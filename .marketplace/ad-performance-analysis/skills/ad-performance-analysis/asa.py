#!/usr/bin/env python3
"""
asa.py — unified ad-performance-analysis entry point.

Subcommands:
  validate                                    # 0=ok | 60=missing | 61=invalid
  setup [--destination-id X]                 # 0=ok | 51/52/53/54=disambiguate | 62=creds missing (non-tty)
        [--connection FAM=ID ...]
        [--schema QDM_TYPE=SCHEMA_NAME ...]  # override schema for a QDM type (persisted)
        [--skip-family FAM ...]              # skip a family (persisted across refreshes)
        [--no-skip]                          # clear all persisted skips
        [--no-schema]                        # clear all persisted schema overrides
        [--refresh] [--skill <id>]
  discover --warehouse <bq|snowflake_cli|databricks_cli> --database <name>
           [--location <region>] [--schema <name> ...]
           [--schema-override KEY=SCHEMA ...] [--skill <id>]
                                               # warehouse-only setup, no Fivetran API key.
                                               # 0=ok | 54=schema disambiguate | 53=insufficient
                                               # connectors | 70/71=CLI missing/unauth
  resolve <family> [--refresh-on-miss]       # prints JSON to stdout
  readiness [FAM ...]                         # parallel data-freshness probe across active_models
  list-schemas --warehouse <bq|snowflake_cli|databricks_cli> --database <name>
                                               # cheap metadata-only schema name list,
                                               # for offering the user a pick-list before
                                               # the full table scan. 0=ok | 53=none found
  check-cli <bq|snowflake_cli|databricks_cli> # 0=ok | 70=missing | 71=unauth
"""

import base64
import concurrent.futures
import configparser
import datetime
import threading
import getpass
import json
import os
import re
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# readiness.json loader (inlined so the skill is self-contained when installed)
# ---------------------------------------------------------------------------
def _load_readiness_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as _fh:
        return json.load(_fh)

def _readiness_required_pool(cfg: dict) -> set:
    return {opt["service"] for grp in cfg.get("connector_groups", []) for opt in grp.get("options", [])}

def _readiness_skill_min_required(cfg: dict) -> dict:
    skill_id = cfg.get("metadata", {}).get("app_id", "")
    total = sum(grp.get("min_required", 1) for grp in cfg.get("connector_groups", []))
    return {skill_id: total}

try:
    _CFG = _load_readiness_json(os.path.join(os.path.dirname(os.path.abspath(__file__)), "readiness.json"))
except Exception as _e:
    print(f"[asa] failed to load readiness.json: {_e}", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_OK                       = 0
EXIT_DESTINATION_DISAMBIGUATE = 51
EXIT_CONNECTION_DISAMBIGUATE  = 52
EXIT_INSUFFICIENT_CONNECTORS  = 53
EXIT_SCHEMA_DISAMBIGUATE      = 54
EXIT_PROFILE_MISSING          = 60
EXIT_PROFILE_INVALID          = 61
EXIT_CREDS_MISSING            = 62
EXIT_CLI_MISSING              = 70
EXIT_CLI_UNAUTH               = 71

# ---------------------------------------------------------------------------
# Constants — derived from readiness.json
# ---------------------------------------------------------------------------
PROFILE_VERSION = "4.0"

# Package-name aliases where the dbt package name differs from the Fivetran
# connector service name (e.g. fivetran/linkedin → linkedin_ads family).
PACKAGE_TO_FAMILY: Dict[str, str] = {
    "linkedin": "linkedin_ads",
    "pinterest": "pinterest_ads",
    "twitter": "twitter_ads",
    "snapchat": "snapchat_ads",
}

# ---------------------------------------------------------------------------
# `discover` subcommand constants — warehouse-only setup (no Fivetran API key)
# ---------------------------------------------------------------------------
# Identifies which family a unified model's `platform`/`source_relation`
# value refers to. `platform` is a literal per-package slug in the real
# fivetran/dbt_ad_reporting package (see get_query.sql: `cast('{{ platform }}'
# ...)`), so it's expected to already match a service's slug or
# readiness.json display_name once normalized (lowercased, non-alphanumerics
# stripped) — no alias needed for it in practice.
#
# `source_relation`, by contrast, is passed through from each connector's own
# upstream staging package (`select source_relation, ... from {{ relation }}`
# in the same macro), so it reflects that package's own naming rather than
# ad_reporting's slug convention. `linkedin_ad_analytics` and `bing_ads` are
# the actual Fivetran staging package names for LinkedIn Ads and Microsoft
# Ads (the latter predating the "Microsoft Advertising" rebrand) — a real,
# structural divergence, not label drift. This alias table exists for that.
DISCOVERY_VALUE_ALIASES: Dict[str, str] = {
    "linkedinadanalytics": "linkedin_ads",
}


def _normalize_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _build_discovery_identity_map() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for grp in _CFG.get("connector_groups", []):
        for opt in grp.get("options", []):
            svc = opt["service"]
            out[_normalize_label(svc)] = svc
            if opt.get("display_name"):
                out[_normalize_label(opt["display_name"])] = svc
    out.update(DISCOVERY_VALUE_ALIASES)
    return out


_DISCOVERY_IDENTITY_TO_FAMILY: Dict[str, str] = _build_discovery_identity_map()


def _family_for_label(raw_value: str) -> Optional[str]:
    return _DISCOVERY_IDENTITY_TO_FAMILY.get(_normalize_label(raw_value))


# The unified model queried in the linkage check and the degraded fallback
# below, and used to test whether a candidate schema is the unified
# ad_reporting layer.
DISCOVERY_PRIMARY_UNIFIED_MODEL = "ad_reporting__campaign_report"

# Above this many schemas in one unscoped scan, warn that discovery may be
# fingerprinting schemas owned by other teams in a shared project.
BROAD_SCAN_SCHEMA_WARN_THRESHOLD = 15

WAREHOUSE_TOOL_TO_DEST_TYPE: Dict[str, str] = {
    "bq": "bigquery",
    "snowflake_cli": "snowflake",
    "databricks_cli": "databricks",
}

ACTIVE_SYNC_STATES = {"scheduled", "syncing", "rescheduled"}

REQUIRED_POOL    = _readiness_required_pool(_CFG)
RECOMMENDED_POOL: set = set()

# channel-performance and mmm-dashboard share this skill's connector pool
# but require a higher min count. Their min is set directly; ad-performance-analysis's
# comes from the config.
SKILL_MIN_REQUIRED: Dict[str, int] = {
    **_readiness_skill_min_required(_CFG),
    "channel-performance": 1,
    "mmm-dashboard":       2,
}

# ---------------------------------------------------------------------------
# Environment config
# ---------------------------------------------------------------------------
API_BASE      = os.environ.get("FIVETRAN_API_BASE_URL", "https://api.fivetran.com").rstrip("/")
MOCK_FETCHER  = os.environ.get("ASA_FIVETRAN_FETCHER", "")
_CURRENT_TOKEN: Optional[str] = None  # set by cmd_setup before any HTTP request
_DATABRICKS_CLI_LOCK = threading.Lock()  # serializes CLI subprocesses to avoid token-cache write races


def _config_dir() -> str:
    local = "./.fivetran/ad-performance-analysis"
    if os.path.isdir(local):
        return local
    return os.path.join(os.path.expanduser("~"), ".fivetran", "skills", "ad-performance-analysis")


def _profile_path() -> str:
    if os.environ.get("AD_PERFORMANCE_ANALYSIS_PROFILE_PATH"):
        return os.environ["AD_PERFORMANCE_ANALYSIS_PROFILE_PATH"]
    return os.path.join(_config_dir(), "profile.json")


def _creds_path() -> str:
    return os.path.join(_config_dir(), "credentials.json")


def _databricks_profile_context() -> Tuple[Optional[str], Optional[str]]:
    profile = (
        os.environ.get("DATABRICKS_CONFIG_PROFILE")
        or os.environ.get("DATABRICKS_PROFILE")
        or ""
    ).strip() or None
    host = os.environ.get("DATABRICKS_HOST", "").strip() or None

    cfg_path = os.path.expanduser(os.environ.get("DATABRICKS_CONFIG_FILE", "~/.databrickscfg"))
    if not os.path.isfile(cfg_path):
        return profile, host

    parser = configparser.ConfigParser()
    try:
        parser.read(cfg_path)
    except Exception:
        return profile, host

    if not profile:
        try:
            sections = [s for s in parser.sections() if s != "__settings__"]
            profile = sections[0] if len(sections) == 1 else None
        except Exception:
            profile = None

    if not host and profile and parser.has_section(profile):
        try:
            host = (parser.get(profile, "host", fallback="") or "").strip() or None
        except Exception:
            host = None

    return profile, host


def _databricks_credential_access_message(raw_msg: str) -> str:
    r = _databricks_error_remediation(raw_msg)
    if r:
        return r["message"]
    return raw_msg


def _databricks_error_remediation(raw_msg: str) -> Optional[dict]:
    profile, host = _databricks_profile_context()
    login_cmd = "databricks auth login"
    if host:
        login_cmd += f" --host {host}"
    if profile:
        login_cmd += f" --profile {profile}"

    msg = raw_msg or ""
    if "cache: no cached credentials" not in msg:
        return None

    return {
        "code": "databricks_cached_credentials_unavailable",
        "next_action": "verify_shell_auth_then_retry_with_user_consent",
        "verify_shell_auth_command": "databricks auth profiles",
        "fallback_login_command": login_cmd,
        "rerun_policy": (
            "If you are running sandboxed, `verify_shell_auth_command` can fail spuriously for "
            "the same credential-cache reason — treat an in-sandbox failure as inconclusive and "
            "ask the user to run it in their own terminal instead of treating it as invalid auth. "
            "If shell-side Databricks auth is valid, the sandbox is likely blocking access to "
            "the credential cache. Tell the user, then retry the same Databricks-backed command "
            "once with the user's approval to run without sandbox restrictions. If that retry "
            "returns this same remediation code again, stop and surface the error instead of "
            "retrying further."
        ),
        "message": (
            "Databricks CLI could not access cached credentials in this process, which usually "
            "means the sandbox is blocking the credential cache. If `databricks auth profiles` "
            "works in your shell, approve one retry of this command without sandbox restrictions. "
            "If you checked `databricks auth profiles` from inside a sandbox, treat a failure "
            "there as inconclusive and check from your own terminal instead. "
            f"If your shell auth is not valid, run `{login_cmd}`."
        ),
    }


def looks_like_b64_token(s: str) -> bool:
    """Return True if s is a valid base64-encoded 'key:secret' Fivetran token."""
    if not s:
        return False
    try:
        decoded = base64.b64decode(s, validate=True).decode("ascii")
    except Exception:
        return False
    if decoded.count(":") != 1:
        return False
    key, secret = decoded.split(":", 1)
    return bool(key) and bool(secret)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

# NOTE: _SSL_CTX, _build_ssl_context, and _ssl_context are duplicated verbatim
# in skills/store-performance-analysis/asa.py and skills/sales-pipeline-analysis/asa.py.
# Any change here must be applied to all three files.
_SSL_CTX: Optional[ssl.SSLContext] = None


def _build_ssl_context() -> ssl.SSLContext:
    # ssl.create_default_context() already honors SSL_CERT_FILE and SSL_CERT_DIR
    # via OpenSSL's standard env-var handling — those do not need to appear in
    # the explicit loop below.
    ctx = ssl.create_default_context()

    def _empty() -> bool:
        try:
            return ctx.cert_store_stats().get("x509_ca", 0) == 0
        except Exception:
            return False

    # REQUESTS_CA_BUNDLE is intentionally included here because stdlib ignores it
    # (only requests/httpx read it); we bridge the gap explicitly.
    for var in ("CUSTOM_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            try:
                ctx.load_verify_locations(cafile=path)
            except Exception:
                pass

    if _empty():
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass

    if _empty() and sys.platform == "darwin":
        keychains = [
            "/System/Library/Keychains/SystemRootCertificates.keychain",
            "/Library/Keychains/System.keychain",
        ]
        for kc in keychains:
            try:
                pem = subprocess.run(
                    ["/usr/bin/security", "find-certificate", "-a", "-p", kc],
                    capture_output=True, text=True, timeout=15,
                ).stdout
                if pem and pem.strip():
                    ctx.load_verify_locations(cadata=pem)
            except Exception:
                pass

    return ctx


def _ssl_context() -> ssl.SSLContext:
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _build_ssl_context()
    return _SSL_CTX


def _auth_header() -> str:
    return "Basic " + (_CURRENT_TOKEN or base64.b64encode(b":").decode())


def fetch_url(url: str) -> dict:
    if MOCK_FETCHER:
        r = subprocess.run([MOCK_FETCHER, url], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"mock fetcher failed for {url!r}: {r.stderr.strip()}")
        return json.loads(r.stdout)
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        req = urllib.request.Request(url)
        req.add_header("Authorization", _auth_header())
        try:
            with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if 500 <= exc.code < 600 and attempt == 1:
                last_exc = exc
                time.sleep(0.75)
                continue
            raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            # SSL cert failures usually mean the user's Python install is
            # missing root certificates (common on python.org Python on macOS).
            # No point retrying — give them an actionable message and stop.
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ssl.SSLCertVerificationError):
                py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
                print(
                    "[asa] SSL certificate verification failed — your Python install is missing root certificates.\n"
                    "      On macOS with python.org Python, run:\n"
                    f"        /Applications/Python\\ {py_ver}/Install\\ Certificates.command\n"
                    "      Then re-run this command. (If you installed Python a different way, install certifi: "
                    "python3 -m pip install --user certifi)",
                    file=sys.stderr,
                )
                sys.exit(1)
            if attempt == 1:
                last_exc = exc
                time.sleep(0.75)
                continue
            raise RuntimeError(f"network error for {url}: {exc}") from exc
    raise RuntimeError(f"network error for {url}: {last_exc}")


def fetch_paginated(endpoint: str, **params) -> List[dict]:
    items: List[dict] = []
    cursor: Optional[str] = None
    while True:
        qp = {k: str(v) for k, v in params.items()}
        if cursor:
            qp["cursor"] = cursor
        qs = "&".join(f"{k}={v}" for k, v in qp.items())
        url = f"{API_BASE}{endpoint}" + (f"?{qs}" if qs else "")
        payload = fetch_url(url)
        data = payload.get("data") or {}
        page_items = data.get("items")
        if isinstance(page_items, list):
            items.extend(page_items)
        cursor = data.get("next_cursor") or None
        if not cursor:
            break
    return items


# ---------------------------------------------------------------------------
# Normalisation helpers (preserved from discover_fivetran.py)
# ---------------------------------------------------------------------------

def normalize_destination_type(service: str) -> str:
    s = (service or "").lower()
    if s in {"big_query", "big_query_dts", "bigquery"} or s.startswith("bigquery_"):
        return "bigquery"
    if s == "snowflake" or s.startswith("snowflake_"):
        return "snowflake"
    if s == "databricks" or s.startswith("adb_") or s == "azure_databricks":
        return "databricks"
    return service or ""


def destination_database(dest_type: str, raw_config: dict) -> str:
    if dest_type == "bigquery":
        return raw_config.get("project_id") or ""
    if dest_type == "snowflake":
        return raw_config.get("database") or ""
    if dest_type == "databricks":
        return raw_config.get("catalog") or ""
    return ""


# ---------------------------------------------------------------------------
# Date + profile I/O helpers
# ---------------------------------------------------------------------------

def _parse_iso8601(value: str) -> datetime.datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(value)


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_profile() -> Optional[dict]:
    path = _profile_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}  # exists but unreadable/invalid → empty dict signals invalid


def _write_profile(obj: dict) -> None:
    config_dir = _config_dir()
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    path = _profile_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_credentials() -> Optional[dict]:
    path = _creds_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_credentials(token: str) -> None:
    config_dir = _config_dir()
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    path = _creds_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"token": token}, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _resolve_credentials() -> Optional[Tuple[str, str]]:
    """Return (token_b64, source) or None. credentials.json wins over env vars."""
    # 1. credentials.json — represents a previously-verified token
    creds = _read_credentials()
    if creds:
        token = (creds.get("token") or "").strip()
        if looks_like_b64_token(token):
            return token, "credentials.json"
        # legacy {api_key, api_secret} — migrate transparently
        key    = (creds.get("api_key")    or "").strip()
        secret = (creds.get("api_secret") or "").strip()
        if key and secret:
            token = base64.b64encode(f"{key}:{secret}".encode()).decode()
            _write_credentials(token)
            return token, "credentials.json"

    # 2. env vars
    env_key    = os.environ.get("FIVETRAN_API_KEY",    "").strip()
    env_secret = os.environ.get("FIVETRAN_API_SECRET", "").strip()

    if env_key and looks_like_b64_token(env_key):
        if env_secret:
            print(
                "[asa] FIVETRAN_API_SECRET is set but ignored — "
                "FIVETRAN_API_KEY is already a base64 token",
                file=sys.stderr,
            )
        return env_key, "env FIVETRAN_API_KEY"

    if env_key and env_secret:
        token = base64.b64encode(f"{env_key}:{env_secret}".encode()).decode()
        return token, "env FIVETRAN_API_KEY + FIVETRAN_API_SECRET"

    if env_key and not env_secret:
        print(
            "[asa] FIVETRAN_API_KEY is set but FIVETRAN_API_SECRET is not — "
            "incomplete credential pair ignored.\n"
            "      Set FIVETRAN_API_KEY to the base64-encoded token from "
            "https://fivetran.com/dashboard/user/api-config",
            file=sys.stderr,
        )
        return None

    return None


def _prompt_for_token() -> Optional[str]:
    """Interactively prompt for a base64 API token. Returns the token or None on failure."""
    for attempt in range(3):
        raw = getpass.getpass("Fivetran API token (base64): ").strip()
        if not raw:
            print("[asa] token cannot be empty", file=sys.stderr)
            continue
        if not looks_like_b64_token(raw):
            print(
                "[asa] that doesn't look like a Fivetran base64 token "
                "(expected the base64-encoded value from "
                "https://fivetran.com/dashboard/user/api-config)",
                file=sys.stderr,
            )
            continue
        return raw
    print("[asa] too many invalid attempts", file=sys.stderr)
    return None


def _write_auth_state(account_id: Optional[str], user_id: Optional[str]) -> None:
    path = os.path.expanduser("~/.fivetran/auth-state")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"account_id": account_id, "user_id": user_id}, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_error_log(tb: str) -> Optional[str]:
    try:
        path = os.path.join(_config_dir(), "asa-error.log")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tb)
        os.chmod(path, 0o600)
        return path
    except Exception:
        return None


def _agent_print(payload: dict, tty_message: str) -> None:
    """Print JSON when stdout is not a tty (agent context), friendly message otherwise."""
    if sys.stdout.isatty():
        print(tty_message)
    else:
        print(json.dumps(payload, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Warehouse query helpers (used by schema probe)
# ---------------------------------------------------------------------------

def _bq_query(sql: str, timeout: int = 30, raise_on_error: bool = False, max_rows: Optional[int] = None) -> Optional[List[dict]]:
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=prettyjson", "--quiet"]
    if max_rows is not None:
        # bq's own default is 100 rows — silently truncates wide pulls (e.g. an
        # INFORMATION_SCHEMA.TABLES scan across several schemas easily exceeds
        # it). Callers doing bulk listing must pass an explicit ceiling.
        cmd.append(f"--max_rows={max_rows}")
    cmd.append(sql)
    r = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        msg = r.stderr.strip()
        print(f"[asa] warn: bq query failed: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg)
        return None
    return json.loads(r.stdout.strip() or "[]")


def _snow_query(sql: str, timeout: int = 30, raise_on_error: bool = False) -> Optional[List]:
    r = subprocess.run(
        ["snow", "sql", "-q", sql, "--output-format", "json"],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        msg = r.stderr.strip()
        print(f"[asa] warn: snow query failed: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg)
        return None
    return json.loads(r.stdout.strip() or "[]")


def _databricks_query(sql: str, timeout: int = 30, raise_on_error: bool = False) -> Optional[List]:
    # Modern Databricks CLI (v1.x) has no `databricks sql execute` subcommand;
    # the SQL Statement Execution REST API is the supported path. Route through
    # `databricks api post` so we keep using the CLI's existing auth profile.
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
    if not warehouse_id:
        msg = ("DATABRICKS_WAREHOUSE_ID not set. Set it to the id of a SQL "
               "warehouse in your Databricks workspace (visible under SQL "
               "Warehouses) and re-run.")
        print(f"[asa] {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg)
        return None

    body = json.dumps({
        "warehouse_id": warehouse_id,
        "statement":    sql,
        "wait_timeout": f"{min(max(timeout, 5), 50)}s",
    })
    with _DATABRICKS_CLI_LOCK:
        r = subprocess.run(
            ["databricks", "api", "post", "/api/2.0/sql/statements/", "--json", body],
            capture_output=True, text=True, timeout=timeout + 10,
        )
    if r.returncode != 0:
        msg = r.stderr.strip()
        print(f"[asa] warn: databricks query failed: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg)
        return None
    try:
        payload = json.loads(r.stdout)
    except Exception:
        return None
    state = (payload.get("status") or {}).get("state")
    if state != "SUCCEEDED":
        err = (payload.get("status") or {}).get("error", {}).get("message", "")
        msg = f"databricks statement state={state} {err}".strip()
        print(f"[asa] warn: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg)
        return None
    return (payload.get("result") or {}).get("data_array") or []


# ---------------------------------------------------------------------------
# QDM schema probe — corrected algorithm (plan §QDM schema probe)
# ---------------------------------------------------------------------------

def _probe_schema(
    dest_type: str,
    database: str,
    location: str,
    model_names: List[str],
) -> List[str]:
    """Find all schemas containing every output_model_name. Returns list of matching schema names."""
    if MOCK_FETCHER or not model_names or not database:
        return []
    try:
        if dest_type == "bigquery":
            return _probe_schema_bq(database, location, model_names)
        if dest_type == "snowflake":
            return _probe_schema_snowflake(database, model_names)
        if dest_type == "databricks":
            return _probe_schema_databricks(database, model_names)
    except Exception as exc:
        print(f"[asa] warn: schema probe failed: {exc}", file=sys.stderr)
    return []


def _probe_schema_bq(
    project: str, location: str, model_names: List[str],
) -> List[str]:
    region = f"region-{location.lower()}" if location else "region-us"
    names_sql = ", ".join(f"'{n}'" for n in model_names)
    sql = (
        f"SELECT table_schema, COUNT(*) AS matched "
        f"FROM `{project}.{region}.INFORMATION_SCHEMA.TABLES` "
        f"WHERE table_name IN ({names_sql}) "
        f"GROUP BY table_schema "
        f"HAVING matched = {len(model_names)}"
    )
    rows = _bq_query(sql)
    if not rows:
        return []
    return [r.get("table_schema") for r in rows if r.get("table_schema")]


def _probe_schema_snowflake(
    database: str, model_names: List[str],
) -> List[str]:
    names_sql = ", ".join(f"'{n.upper()}'" for n in model_names)
    sql = (
        f"SELECT TABLE_SCHEMA, COUNT(*) AS matched "
        f"FROM {database}.INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_NAME IN ({names_sql}) "
        f"GROUP BY TABLE_SCHEMA "
        f"HAVING matched = {len(model_names)}"
    )
    rows = _snow_query(sql)
    if not rows:
        return []

    def get_schema(row):
        if isinstance(row, dict):
            return row.get("TABLE_SCHEMA") or row.get("table_schema")
        if isinstance(row, list) and row:
            return str(row[0])
        return None

    return [s for s in (get_schema(r) for r in rows) if s]


def _probe_schema_databricks(
    catalog: str, model_names: List[str],
) -> List[str]:
    names_sql = ", ".join(f"'{n}'" for n in model_names)
    sql = (
        f"SELECT table_schema, COUNT(*) AS matched "
        f"FROM system.information_schema.tables "
        f"WHERE table_catalog = '{catalog}' AND table_name IN ({names_sql}) "
        f"GROUP BY table_schema HAVING matched = {len(model_names)}"
    )
    rows = _databricks_query(sql)
    if not rows:
        return []
    if isinstance(rows[0], list):
        return [str(r[0]) for r in rows if isinstance(r, list) and r]
    return [str(rows[0])]


# ---------------------------------------------------------------------------
# `discover` subcommand — warehouse-only setup, no Fivetran API key.
#
# Schema/dataset names in Fivetran are arbitrary user-chosen strings and do NOT
# match the connector name (e.g. a Facebook Ads connector may land in a schema
# called "ads_9108233"). So discovery never matches on schema name. Instead it
# pulls the full (schema, table) inventory once via INFORMATION_SCHEMA and
# fingerprints each schema's *table names* against readiness.json's declared
# required_tables (raw connectors) and required_models (QDM), since Fivetran
# does standardize table names even though it doesn't standardize schema names.
# ---------------------------------------------------------------------------

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _validate_identifier(name: str, kind: str) -> None:
    """Discovery interpolates database/schema names directly into SQL (they're
    identifiers, not values, so they can't be bind-parameterized). Reject
    anything outside a safe identifier charset up front instead of letting it
    reach the warehouse as a broken or misinterpreted query."""
    if not name or not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(
            f"invalid {kind} {name!r}: only letters, digits, underscore, hyphen, "
            "and dot are allowed"
        )


def _list_schema_names(dest_type: str, database: str) -> List[str]:
    """Return just the schema names in a database, without reading table inventory.

    This is deliberately separate from `_list_tables`. Listing schema names is a
    metadata lookup on all three warehouses, whereas the table scan reads
    INFORMATION_SCHEMA across every schema and is the expensive part. Discovery
    uses this to offer the user a pick-list instead of requiring them to recall a
    schema name up front, and on BigQuery it also sidesteps `--location`, because
    `bq ls` is not a regional INFORMATION_SCHEMA view.
    """
    _validate_identifier(database, "--database")
    if dest_type == "bigquery":
        r = subprocess.run(
            ["bq", "ls", "--max_results=10000", "--format=json", f"--project_id={database}"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        out = []
        for entry in json.loads(r.stdout.strip() or "[]"):
            ref = (entry or {}).get("datasetReference") or {}
            name = ref.get("datasetId") or (entry or {}).get("id", "").split(":")[-1]
            if name:
                out.append(name)
        return sorted(set(out))

    if dest_type == "snowflake":
        rows = _snow_query(f"SHOW SCHEMAS IN DATABASE {database}", raise_on_error=True) or []
    elif dest_type == "databricks":
        # Backticks are required, not cosmetic: a catalog name containing a
        # hyphen (e.g. `luke-test`) is a valid Databricks identifier but is
        # rejected as INVALID_IDENTIFIER when interpolated bare.
        rows = _databricks_query(f"SHOW SCHEMAS IN `{database}`", raise_on_error=True) or []
    else:
        raise ValueError(f"unsupported destination_type for schema listing: {dest_type!r}")

    out = []
    for r_ in rows:
        if isinstance(r_, dict):
            # SHOW SCHEMAS labels the column differently per warehouse.
            val = (r_.get("name") or r_.get("NAME")
                   or r_.get("schema_name") or r_.get("databaseName"))
        elif isinstance(r_, list) and r_:
            val = r_[0]
        else:
            continue
        if val:
            out.append(str(val))
    return sorted(set(out))


def cmd_list_schemas(warehouse_tool: str, database: str) -> int:
    if warehouse_tool not in WAREHOUSE_TOOL_TO_DEST_TYPE:
        print(f"[asa] unknown --warehouse {warehouse_tool!r}; expected one of "
              f"{sorted(WAREHOUSE_TOOL_TO_DEST_TYPE)}", file=sys.stderr)
        return 1
    dest_type = WAREHOUSE_TOOL_TO_DEST_TYPE[warehouse_tool]

    cli_status = cmd_check_cli(warehouse_tool)
    if cli_status != EXIT_OK:
        return cli_status

    try:
        schemas = _list_schema_names(dest_type, database)
    except ValueError as exc:
        print(f"[asa] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[asa] could not list schemas in {database!r}: {exc}", file=sys.stderr)
        return 1

    if not schemas:
        _agent_print(
            {"status": "not_found", "database": database, "schemas": [],
             "message": f"No schemas found in {database!r}. Check the database/project/catalog name."},
            f"No schemas found in {database}.",
        )
        return EXIT_INSUFFICIENT_CONNECTORS

    _agent_print(
        {"status": "ok", "database": database, "schemas": schemas, "count": len(schemas)},
        f"{len(schemas)} schemas in {database}: {', '.join(schemas)}",
    )
    return EXIT_OK


def _resolve_scanned_schema(name: str, schema_tables: Dict[str, set]) -> Optional[str]:
    """Map an operator-supplied schema name onto an actually-scanned schema key.

    Snowflake reports identifiers uppercased, so `--schema-override unified=ad_reporting`
    would never match the scanned key `AD_REPORTING` under an exact comparison, and the
    override was rejected even though the schema was plainly present. Try the exact name
    first, then fall back to a case-insensitive match, and return the scanned spelling so
    callers index `schema_tables` with a key that exists.
    """
    if name in schema_tables:
        return name
    lowered = name.lower()
    for scanned in schema_tables:
        if scanned.lower() == lowered:
            return scanned
    return None


def _list_tables_bq(project: str, location: str, schemas: Optional[List[str]]) -> List[Tuple[str, str]]:
    _validate_identifier(project, "--database")
    region = f"region-{(location or 'us').lower()}"
    sql = f"SELECT table_schema, table_name FROM `{project}.{region}.INFORMATION_SCHEMA.TABLES`"
    if schemas:
        for s in schemas:
            _validate_identifier(s, "--schema")
        names_sql = ", ".join(f"'{s}'" for s in schemas)
        sql += f" WHERE table_schema IN ({names_sql})"
    # A handful of schemas can easily hold hundreds of tables combined (e.g. a
    # single ad-connector raw schema commonly has 50-100+ report tables) —
    # bq's un-overridden default row cap is 100, well below that.
    rows = _bq_query(sql, raise_on_error=True, max_rows=100000) or []
    return [(r.get("table_schema"), r.get("table_name")) for r in rows if r.get("table_schema") and r.get("table_name")]


def _list_tables_snowflake(database: str, schemas: Optional[List[str]]) -> List[Tuple[str, str]]:
    _validate_identifier(database, "--database")
    sql = f"SELECT TABLE_SCHEMA, TABLE_NAME FROM {database}.INFORMATION_SCHEMA.TABLES"
    if schemas:
        for s in schemas:
            _validate_identifier(s, "--schema")
        names_sql = ", ".join(f"'{s.upper()}'" for s in schemas)
        sql += f" WHERE TABLE_SCHEMA IN ({names_sql})"
    rows = _snow_query(sql, raise_on_error=True) or []
    out = []
    for r in rows:
        if isinstance(r, dict):
            schema, table = r.get("TABLE_SCHEMA") or r.get("table_schema"), r.get("TABLE_NAME") or r.get("table_name")
        elif isinstance(r, list) and len(r) >= 2:
            schema, table = str(r[0]), str(r[1])
        else:
            continue
        if schema and table:
            out.append((schema, table))
    return out


def _list_tables_databricks(catalog: str, schemas: Optional[List[str]]) -> List[Tuple[str, str]]:
    _validate_identifier(catalog, "--database")
    sql = f"SELECT table_schema, table_name FROM system.information_schema.tables WHERE table_catalog = '{catalog}'"
    if schemas:
        for s in schemas:
            _validate_identifier(s, "--schema")
        names_sql = ", ".join(f"'{s}'" for s in schemas)
        sql += f" AND table_schema IN ({names_sql})"
    rows = _databricks_query(sql, raise_on_error=True) or []
    out = []
    for r in rows:
        if isinstance(r, list) and len(r) >= 2:
            out.append((str(r[0]), str(r[1])))
    return out


def _list_tables(dest_type: str, database: str, location: str, schemas: Optional[List[str]]) -> List[Tuple[str, str]]:
    if dest_type == "bigquery":
        return _list_tables_bq(database, location, schemas)
    if dest_type == "snowflake":
        return _list_tables_snowflake(database, schemas)
    if dest_type == "databricks":
        return _list_tables_databricks(database, schemas)
    raise ValueError(f"unsupported destination_type for discovery: {dest_type!r}")


def _schema_table_map(pairs: List[Tuple[str, str]], dest_type: str) -> Dict[str, set]:
    """Build {schema_name: {table_name, ...}}. Snowflake uppercases identifiers by
    default (mirrors _probe_schema_snowflake's existing uppercasing behavior)."""
    out: Dict[str, set] = {}
    for schema, table in pairs:
        out.setdefault(schema, set()).add(table.upper() if dest_type == "snowflake" else table)
    return out


def _names_present(table_set: set, names: List[str], dest_type: str) -> bool:
    check = {n.upper() for n in names} if dest_type == "snowflake" else set(names)
    return check.issubset(table_set)


def _names_matched(table_set: set, names: List[str], dest_type: str) -> List[str]:
    check = [n.upper() if dest_type == "snowflake" else n for n in names]
    matched_upper = {n for n in check if n in table_set}
    # Return the caller's original casing for whichever names matched.
    return [orig for orig, chk in zip(names, check) if chk in matched_upper]


def _bq_dataset_location(project: str, dataset: str) -> Optional[str]:
    """Read a BigQuery dataset's true region, so `_list_tables_bq` queries the
    right INFORMATION_SCHEMA region view. Probing the wrong region silently
    returns zero rows, which would otherwise be misread as "nothing found."""
    try:
        r = subprocess.run(
            ["bq", "show", "--format=prettyjson", f"{project}:{dataset}"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return None
        info = json.loads(r.stdout)
        loc = info.get("location")
        return str(loc) if loc else None
    except Exception:
        return None


def _linked_families_for_unified(dest_type: str, database: str, unified_schema: str, model: str) -> set:
    """Return the family keys that actually have data in the unified model —
    used both to confirm a raw-fingerprinted connector is genuinely linked to
    the unified layer (a family's raw schema and an unrelated unified schema
    can both exist in the same database without that family feeding it), and
    (when no raw schema is found at all) to identify families in the degraded
    fallback.

    Tries `source_relation` first — its value comes from each connector's own
    upstream staging package rather than ad_reporting's slug convention, so it
    can diverge from a service's slug (see DISCOVERY_VALUE_ALIASES) — then
    `platform`, the ad_reporting-level slug, as a second signal. Unions
    whatever either column yields; a column that doesn't exist on a given QDM
    version is skipped rather than treated as fatal. Values from both columns
    are matched via `_family_for_label`, so a family is recognized regardless
    of which label style this particular QDM version happens to store."""
    families: set = set()
    columns = ("source_relation", "platform")
    errors: List[Exception] = []
    for column in columns:
        sql = (f"SELECT DISTINCT {column} FROM `{database}.{unified_schema}.{model}`"
               if dest_type == "bigquery" else
               # Databricks needs each identifier backticked separately so that
               # hyphenated catalog names survive (see _list_schema_names).
               f"SELECT DISTINCT {column} FROM `{database}`.`{unified_schema}`.`{model}`"
               if dest_type == "databricks" else
               f"SELECT DISTINCT {column} FROM {database}.{unified_schema}.{model}")
        try:
            rows = (_bq_query(sql, raise_on_error=True) if dest_type == "bigquery" else
                    _snow_query(sql, raise_on_error=True) if dest_type == "snowflake" else
                    _databricks_query(sql, raise_on_error=True)) or []
        except Exception as exc:
            # Tolerated only because the *other* column may still work: a given
            # QDM version may not have both. Every helper raises a bare
            # RuntimeError carrying raw stderr, so the failure reason can't be
            # classified here — but a version missing BOTH columns isn't a real
            # scenario, so both failing means the query itself is broken
            # (bad identifier, auth, permissions). Swallowing that returns an
            # empty set, which the caller reports as "no recognizable
            # platforms" — implying the query ran and returned unrecognized
            # data when it never ran at all. Propagate instead, so the caller's
            # accurate "linkage lookup failed (<error>)" branch is reachable.
            errors.append(exc)
            continue
        for r in rows:
            if isinstance(r, dict):
                value = r.get(column) or r.get(column.upper())
            elif isinstance(r, list) and r:
                value = r[0]
            else:
                continue
            if not value:
                continue
            family = _family_for_label(str(value))
            if family and family in REQUIRED_POOL:
                families.add(family)
    if len(errors) == len(columns):
        raise errors[-1]
    return families


def cmd_discover(
    warehouse_tool: str,
    database: str,
    location: Optional[str],
    schema_hints: List[str],
    schema_overrides: Dict[str, str],
    skill_id: str,
) -> int:
    if warehouse_tool not in WAREHOUSE_TOOL_TO_DEST_TYPE:
        print(f"[asa] unknown --warehouse {warehouse_tool!r}; expected one of "
              f"{sorted(WAREHOUSE_TOOL_TO_DEST_TYPE)}", file=sys.stderr)
        return 1
    dest_type = WAREHOUSE_TOOL_TO_DEST_TYPE[warehouse_tool]

    cli_status = cmd_check_cli(warehouse_tool)
    if cli_status != EXIT_OK:
        return cli_status

    # BigQuery region must be known, never assumed. INFORMATION_SCHEMA is a
    # per-region view, so scanning the wrong region returns zero rows that look
    # exactly like "this project has no tables". Silently defaulting to US made
    # every non-US project report nothing found with no usable error.
    if dest_type == "bigquery" and not location:
        if schema_hints:
            location = _bq_dataset_location(database, schema_hints[0])
        if not location:
            print(
                "[asa] BigQuery region could not be determined. "
                "Pass --location <region> (e.g. US, EU, us-east1) and retry. "
                "A scan against the wrong region returns zero rows that are "
                "indistinguishable from an empty project, so discovery will not guess.",
                file=sys.stderr,
            )
            return 1
    resolved_location = location or "US"

    try:
        pairs = _list_tables(dest_type, database, resolved_location, schema_hints or None)
    except RuntimeError as exc:
        print(f"[asa] discovery query failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"[asa] {exc}", file=sys.stderr)
        return 1

    schema_tables = _schema_table_map(pairs, dest_type)
    if not schema_tables:
        _agent_print(
            {
                "status": "not_found",
                "message": "No tables found in the given database/schema(s). Check the "
                            "database name and schema hints, or fall back to the "
                            "API-key setup.",
            },
            "No tables found. Return to your Claude Code chat to continue.",
        )
        return EXIT_INSUFFICIENT_CONNECTORS

    # An unscoped scan on a shared project sees every team's schemas, so a
    # fingerprint can match a connector that belongs to somebody else. A
    # stderr-only warning is invisible to an agent that only inspects the
    # JSON payload and exit code, so it is also carried into the result
    # payload as broad_scan_warning.
    broad_scan_warning: Optional[str] = None
    if not schema_hints and len(schema_tables) > BROAD_SCAN_SCHEMA_WARN_THRESHOLD:
        broad_scan_warning = (
            f"Found {len(schema_tables)} schemas in {database!r} and no --schema "
            "hints were given. On a shared project this can fingerprint schemas "
            "belonging to unrelated teams. Pass --schema <name> to narrow the scan "
            "if the result below looks wrong."
        )
        print(f"[asa] warn: {broad_scan_warning}", file=sys.stderr)

    # An override key that is neither "unified" nor a known family matches nothing
    # and does nothing, so a mistyped family name used to be accepted in silence.
    unknown_override_keys = sorted(
        k for k in schema_overrides if k != "unified" and k not in REQUIRED_POOL
    )
    if unknown_override_keys:
        print(
            f"[asa] warn: ignoring unrecognized --schema-override key(s) "
            f"{', '.join(repr(k) for k in unknown_override_keys)}. Expected 'unified' or "
            f"one of: {', '.join(sorted(REQUIRED_POOL))}.",
            file=sys.stderr,
        )

    # --- 1. Unified/multisource QDM schema -----------------------------------
    unified_candidates: List[str] = []
    quickstart = _CFG.get("quickstart_models") or []
    unified_required: List[str] = []
    unified_recommended: List[str] = []
    if quickstart:
        # ad-performance-analysis declares one multisource quickstart package.
        qm = quickstart[0]
        unified_required    = [m["name"] for m in qm.get("required_models", [])]
        unified_recommended = [m["name"] for m in qm.get("recommended_models", [])]
        # Mirrors the raw-connector loop below: an empty required list would
        # make _names_present trivially true for every schema, matching all
        # of them rather than none.
        if unified_required:
            for schema, tables in schema_tables.items():
                if _names_present(tables, unified_required, dest_type):
                    unified_candidates.append(schema)

    if "unified" in schema_overrides:
        unified_schema = schema_overrides["unified"]
        try:
            _validate_identifier(unified_schema, "--schema-override unified")
        except ValueError as exc:
            print(f"[asa] {exc}", file=sys.stderr)
            return 1
        resolved = _resolve_scanned_schema(unified_schema, schema_tables)
        if resolved is None:
            print(
                f"[asa] --schema-override unified={unified_schema!r} was not among the "
                f"scanned schemas ({', '.join(sorted(schema_tables)) or 'none'}). "
                "Add it via --schema and re-run.",
                file=sys.stderr,
            )
            return 1
        unified_schema = resolved
    elif len(unified_candidates) > 1:
        candidates = sorted(unified_candidates)
        _agent_print(
            {
                "status": "disambiguate_required",
                "schemas": {"multisource_ad_reporting": candidates},
                "hint": f"Re-run with: --schema-override unified={candidates[0]}",
            },
            f"Multiple unified schemas found: {', '.join(candidates)}. "
            "Re-run with --schema-override unified=<name> to pick one.",
        )
        return EXIT_SCHEMA_DISAMBIGUATE
    elif len(unified_candidates) == 1:
        unified_schema = unified_candidates[0]
    else:
        unified_schema = None

    active_models: List[str] = []
    if unified_schema:
        active_models = _names_matched(schema_tables[unified_schema], unified_required + unified_recommended, dest_type)

    # Which families the unified schema actually contains data for — a family's
    # raw schema and an unrelated unified schema can both exist in the same
    # database without that family feeding it, so presence of *a* unified
    # schema is not by itself evidence that a given connector is linked to it.
    # When this lookup fails, linked_families stays empty and every connector
    # silently downgrades to model_tier "raw" with unified_schema null, which
    # looks identical to a warehouse that genuinely has no QDM layer. A stderr
    # warning alone was invisible to the agent, so the failure is also carried
    # into the result payload as linkage_warning.
    linked_families: set = set()
    linkage_warning: Optional[str] = None
    if unified_schema:
        model = DISCOVERY_PRIMARY_UNIFIED_MODEL if DISCOVERY_PRIMARY_UNIFIED_MODEL in active_models else (active_models[0] if active_models else None)
        if not model:
            linkage_warning = (
                f"Unified schema {unified_schema!r} matched, but none of its expected models "
                "were found, so connector-to-QDM linkage could not be checked. Every connector "
                "below is reported as raw tier even if a QDM layer exists."
            )
        else:
            try:
                linked_families = _linked_families_for_unified(dest_type, database, unified_schema, model)
            except Exception as exc:
                linkage_warning = (
                    f"Unified linkage lookup against {unified_schema}.{model} failed ({exc}). "
                    "Every connector below is reported as raw tier and unified_schema is null, "
                    "which is indistinguishable from having no QDM layer at all. Re-run with "
                    "--schema-override unified=<name> or check read access on that model before "
                    "trusting the tiers below."
                )
            else:
                if not linked_families:
                    linkage_warning = (
                        f"Unified schema {unified_schema!r} was found, but {model} reported no "
                        "recognizable platforms, so no connector could be linked to it. Every "
                        "connector below is reported as raw tier."
                    )
        if linkage_warning:
            print(f"[asa] warn: {linkage_warning}", file=sys.stderr)

    # --- 2. Raw connector identity (source of truth for the family key) ------
    # A schema whose tables contain a service's required_tables IS that
    # connector — the matching `service` value becomes the profile family key
    # directly (the same key cmd_setup writes, keyed off connection.service).
    raw_matches: Dict[str, List[str]] = {}  # family -> candidate schemas
    for grp in _CFG.get("connector_groups", []):
        for opt in grp.get("options", []):
            service = opt["service"]
            required = [t["name"] for t in opt.get("required_tables", [])]
            if not required:
                continue
            matches = [s for s, tables in schema_tables.items() if _names_present(tables, required, dest_type)]
            if matches:
                raw_matches[service] = matches

    needs_raw_disambig = {
        fam: sorted(schemas) for fam, schemas in raw_matches.items()
        if len(schemas) > 1 and fam not in schema_overrides
    }
    if needs_raw_disambig:
        _agent_print(
            {
                "status": "disambiguate_required",
                "schemas": needs_raw_disambig,
                "hint": "Re-run with: " + " ".join(
                    f"--schema-override {fam}={cands[0]}"
                    for fam, cands in sorted(needs_raw_disambig.items())
                ),
            },
            "Multiple schemas found: " + "; ".join(
                f"{fam}: {', '.join(cands)}" for fam, cands in sorted(needs_raw_disambig.items())
            ) + ". Re-run with --schema-override <family>=<name> for each.",
        )
        return EXIT_SCHEMA_DISAMBIGUATE

    # A per-family override used to be charset-checked only, unlike the unified
    # override which was also confirmed against the scan. A typo was therefore
    # accepted here and only surfaced much later as an opaque warehouse error.
    def _checked_override(family: str, raw_value: str) -> Optional[str]:
        try:
            _validate_identifier(raw_value, f"--schema-override {family}")
        except ValueError as exc:
            print(f"[asa] {exc}", file=sys.stderr)
            raise
        resolved = _resolve_scanned_schema(raw_value, schema_tables)
        if resolved is None:
            print(
                f"[asa] --schema-override {family}={raw_value!r} was not among the "
                f"scanned schemas ({', '.join(sorted(schema_tables)) or 'none'}). "
                "Add it via --schema and re-run.",
                file=sys.stderr,
            )
            raise ValueError(f"unscanned schema for {family}")
        return resolved

    connectors: Dict[str, dict] = {}
    for family, schemas in raw_matches.items():
        override = schema_overrides.get(family)
        if override:
            try:
                override = _checked_override(family, override)
            except ValueError:
                return 1
        raw_schema = override or schemas[0]
        tier = "multisource" if family in linked_families else "raw"
        connectors[family] = {
            "connection_id":        f"manual:{family}",
            "raw_schema":           raw_schema,
            "model_tier":           tier,
            "unified_schema":       unified_schema if tier == "multisource" else None,
            "single_source_schema": None,
            "active_models":        active_models if tier == "multisource" else [],
            "excluded_models":      [],
            "last_ended_at":        None,
            "qdm_functional":       True,
        }

    # --- 3. Degraded fallback: QDM linked, no raw schema fingerprinted -------
    # Family identity here comes from the unified model's platform values rather
    # than a raw fingerprint, for any family the unified layer feeds but whose
    # raw tables were not found.
    #
    # This used to be gated on `not connectors`, i.e. it only ran when zero raw
    # schemas matched anywhere. On a warehouse where some families kept their raw
    # schemas and others did not, the ones without were dropped from the profile
    # entirely: absent from connectors, absent from raw_schema_placeholders, and
    # not counted toward min_required, with nothing printed. Now every linked
    # family that raw fingerprinting missed is filled in, whatever the others did.
    #
    # An operator-supplied --schema-override for such a family is also honoured
    # here. warehouse-discovery.md documents exactly that recovery step, but the
    # override was only ever read in the raw_matches loop above, and a family
    # reaching this block is by definition not in raw_matches, so the value the
    # operator typed was silently discarded.
    if unified_schema:
        for family in sorted(linked_families):
            if family not in REQUIRED_POOL or family in connectors:
                continue
            override = schema_overrides.get(family)
            resolved_override: Optional[str] = None
            if override:
                try:
                    resolved_override = _checked_override(family, override)
                except ValueError:
                    return 1
            connectors[family] = {
                "connection_id":        f"manual:{family}",
                "raw_schema":           resolved_override or unified_schema,
                "model_tier":           "multisource",
                "unified_schema":       unified_schema,
                "single_source_schema": None,
                "active_models":        active_models,
                "excluded_models":      [],
                "last_ended_at":        None,
                "qdm_functional":       True,
                "raw_schema_is_placeholder": resolved_override is None,
            }

    min_required = SKILL_MIN_REQUIRED.get(skill_id, 1)
    found = sorted(connectors.keys())
    if len(found) < min_required:
        _agent_print(
            {
                "status": "insufficient_connectors",
                "required_pool": sorted(REQUIRED_POOL),
                "found": found,
                "min_required_count": min_required,
            },
            "No supported ad connectors found. Return to your Claude Code chat for details.",
        )
        return EXIT_INSUFFICIENT_CONNECTORS

    script_dir  = os.path.dirname(os.path.abspath(__file__))
    plugin_path = os.path.join(script_dir, "..", ".claude-plugin", "plugin.json")
    skill_version = "0.1.0"
    try:
        with open(plugin_path, "r", encoding="utf-8") as f:
            pdata = json.load(f)
        v = pdata.get("version")
        if isinstance(v, str) and v:
            skill_version = v
    except Exception:
        pass

    profile = {
        "config_version": PROFILE_VERSION,
        "install_id":    uuid.uuid4().hex,
        "discovered_at": _now_utc(),
        "skill":         {"id": skill_id, "version": skill_version},
        "destination":   {
            "destination_id":   "manual",
            "destination_type": dest_type,
            "warehouse_tool":   warehouse_tool,
            "database":         database,
            "location":         resolved_location,
        },
        "skipped_families": [],
        "schema_overrides": dict(schema_overrides),
        "connectors": connectors,
        "setup_method": "warehouse_discovery",
        "discovery_inputs": {
            "warehouse_tool": warehouse_tool,
            "database":       database,
            "location":       resolved_location,
            "schema_hints":   list(schema_hints),
        },
    }
    _write_profile(profile)

    placeholder_families = sorted(f for f, e in connectors.items() if e.get("raw_schema_is_placeholder"))
    payload = {
        "status":       "ok",
        "profile_path": _profile_path(),
        "destination":  profile["destination"],
        "connections": [
            {"family": f, "connection_id": e["connection_id"], "schema": e["raw_schema"], "model_tier": e["model_tier"]}
            for f, e in sorted(connectors.items())
        ],
        "multi_source_qdms": (
            [{
                "package":         "ad_reporting",
                "schema":          unified_schema,
                "linked_families": sorted(f for f, e in connectors.items() if e["model_tier"] == "multisource"),
                "active_models":   active_models,
                "excluded_models": [],
                "last_ended_at":   None,
                "qdm_functional":  True,
            }] if unified_schema else []
        ),
        "single_source_qdms": [],
        "raw_schema_placeholders": placeholder_families,
    }
    # Carried in the payload, not just stderr: a failed linkage check makes every
    # connector look raw-tier, which the agent cannot otherwise distinguish from a
    # warehouse that genuinely has no QDM layer.
    if linkage_warning:
        payload["linkage_warning"] = linkage_warning
    if broad_scan_warning:
        payload["broad_scan_warning"] = broad_scan_warning
    if unknown_override_keys:
        payload["ignored_schema_override_keys"] = unknown_override_keys
    _agent_print(
        payload,
        "Warehouse discovery complete. Return to your Claude Code chat to continue.",
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# `validate` subcommand
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(r"password|secret|token|api[_-]?key|authorization", re.IGNORECASE)


def _is_valid_profile(p: dict) -> bool:
    if p.get("config_version") != PROFILE_VERSION:
        return False
    dest = p.get("destination")
    if not isinstance(dest, dict):
        return False
    if not all(isinstance(dest.get(k), str) and dest.get(k) for k in ("destination_id", "destination_type", "warehouse_tool")):
        return False
    if "database" not in dest:
        return False
    skipped = p.get("skipped_families")
    if skipped is not None and not isinstance(skipped, list):
        return False
    schema_overrides = p.get("schema_overrides")
    if schema_overrides is not None and not isinstance(schema_overrides, dict):
        return False
    connectors = p.get("connectors")
    if not isinstance(connectors, dict):
        return False
    valid_tiers = {"multisource", "single_source", "raw"}
    for entry in connectors.values():
        if not isinstance(entry, dict):
            return False
        if not (isinstance(entry.get("connection_id"), str)
                and isinstance(entry.get("raw_schema"), str)
                and entry.get("model_tier") in valid_tiers
                and "unified_schema" in entry
                and "single_source_schema" in entry):
            return False
    return True


def _scan_secrets(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            cur = path + (str(k),)
            if _SECRET_RE.search(str(k)):
                raise ValueError(f"secret-like key '{k}' found in profile")
            _scan_secrets(v, cur)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _scan_secrets(item, path + (str(i),))


def cmd_validate() -> int:
    raw = _read_profile()
    if raw is None:
        return EXIT_PROFILE_MISSING
    if not raw or not _is_valid_profile(raw):
        print("[asa] profile is invalid or wrong version — re-run setup", file=sys.stderr)
        return EXIT_PROFILE_INVALID
    try:
        _scan_secrets(raw)
    except ValueError as exc:
        print(f"[asa] {exc}", file=sys.stderr)
        return EXIT_PROFILE_INVALID
    return EXIT_OK


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _classify_transformation(package_name: str) -> Optional[str]:
    pkg = (package_name or "").lower().strip()
    if not pkg:
        return None
    if pkg == "ad_reporting":
        return "multisource_ad_reporting"
    family = PACKAGE_TO_FAMILY.get(pkg) or (pkg if pkg in REQUIRED_POOL else None)
    return f"single_source_{family}" if family else None


def _fetch_destination_detail(dest_id: str) -> Tuple[dict, str]:
    """Returns (raw_config, location) for the destination."""
    try:
        payload = fetch_url(f"{API_BASE}/v1/destinations/{dest_id}")
        data = payload.get("data") or {}
        config = data.get("config") or {}
        if isinstance(config, dict):
            loc = config.get("location") or config.get("data_set_location") or "US"
        else:
            loc = "US"
        return (config if isinstance(config, dict) else {}), str(loc)
    except Exception:
        return {}, "US"


def _fetch_txfm_detail(txfm_id: str) -> Optional[dict]:
    try:
        payload = fetch_url(f"{API_BASE}/v1/transformations/{txfm_id}")
        return payload.get("data") or {}
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc) and "HTTP 410" not in str(exc):
            print(f"[asa] warn: transformation {txfm_id!r}: {exc}", file=sys.stderr)
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# `setup` subcommand
# ---------------------------------------------------------------------------

def cmd_setup(
    destination_id_override: Optional[str],
    connection_overrides: Dict[str, str],
    skill_id: str,
    refresh: bool,
    skip_families: Optional[set] = None,
    clear_skip: bool = False,
    schema_overrides: Optional[Dict[str, str]] = None,
    clear_schema_overrides: bool = False,
) -> int:
    # Idempotent: skip if profile already valid and not refreshing
    if not refresh:
        raw = _read_profile()
        if raw and _is_valid_profile(raw):
            print(json.dumps({
                "status": "ok",
                "profile_path": _profile_path(),
                "message": "profile already exists; pass --refresh to rediscover",
            }, separators=(",", ":")))
            return EXIT_OK

    global _CURRENT_TOKEN
    result = _resolve_credentials()
    if result:
        _CURRENT_TOKEN, source = result
        print(f"[asa] using Fivetran credentials from {source}", file=sys.stderr)
    elif sys.stdin.isatty():
        _CURRENT_TOKEN = _prompt_for_token()
        if not _CURRENT_TOKEN:
            return EXIT_CREDS_MISSING
        source = "prompt"
    else:
        _script_path = os.path.abspath(__file__).replace(".py", ".sh")
        print(
            "[asa] Fivetran credentials not found.\n"
            "Run setup in your own terminal (credentials will be prompted securely):\n"
            f"  bash {_script_path} setup --skill <skill-id>",
            file=sys.stderr,
        )
        return EXIT_CREDS_MISSING

    # Merge CLI skip/schema flags with persisted state.
    existing_profile = _read_profile() or {}

    existing_skip = set(existing_profile.get("skipped_families") or [])
    if clear_skip:
        final_skip: set = set()
    elif skip_families:
        final_skip = set(skip_families)
    else:
        final_skip = existing_skip

    existing_schema_overrides: Dict[str, str] = existing_profile.get("schema_overrides") or {}
    if clear_schema_overrides:
        final_schema_overrides: Dict[str, str] = {}
    elif schema_overrides:
        final_schema_overrides = {**existing_schema_overrides, **schema_overrides}
    else:
        final_schema_overrides = dict(existing_schema_overrides)

    min_required = SKILL_MIN_REQUIRED.get(skill_id, 1)

    # Fetch destinations + groups in parallel
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            dest_fut   = pool.submit(fetch_paginated, "/v1/destinations", limit=1000)
            groups_fut = pool.submit(fetch_paginated, "/v1/groups",       limit=1000)
            raw_destinations = dest_fut.result()
            raw_groups       = groups_fut.result()
    except RuntimeError as exc:
        if "HTTP 401" not in str(exc):
            raise
        if not sys.stdin.isatty():
            print(
                f"[asa] Invalid Fivetran credentials (source: {source}).\n"
                "      Check https://fivetran.com/dashboard/user/api-config and re-run.",
                file=sys.stderr,
            )
            return EXIT_CREDS_MISSING
        print(
            f"\n[asa] Invalid Fivetran credentials.\n"
            f"      Source: {source}\n\n"
            f"      Options:\n"
            f"        [1] Update {source} and re-run this script\n"
            f"            (recommended if you want this token to work across other Fivetran tools)\n"
            f"        [2] Paste a fresh API token now (will be saved for this skill only)\n"
            f"        [q] Quit\n",
            file=sys.stderr,
        )
        choice = input("      Choice [1/2/q]: ").strip().lower()
        if choice != "2":
            return EXIT_CREDS_MISSING
        _CURRENT_TOKEN = _prompt_for_token()
        if not _CURRENT_TOKEN:
            return EXIT_CREDS_MISSING
        source = "prompt"
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                dest_fut   = pool.submit(fetch_paginated, "/v1/destinations", limit=1000)
                groups_fut = pool.submit(fetch_paginated, "/v1/groups",       limit=1000)
                raw_destinations = dest_fut.result()
                raw_groups       = groups_fut.result()
        except RuntimeError as exc2:
            if "HTTP 401" in str(exc2):
                print("[asa] Invalid credentials on retry — giving up.", file=sys.stderr)
                return EXIT_CREDS_MISSING
            raise

    # Credentials verified — persist to file now (not before, so bad creds aren't stored)
    _write_credentials(_CURRENT_TOKEN)

    try:
        acct = fetch_url(f"{API_BASE}/v1/account/info").get("data") or {}
        _write_auth_state(acct.get("account_id") or None, acct.get("user_id") or None)
    except Exception as exc:
        print(f"[asa] warn: could not write auth-state: {exc}", file=sys.stderr)

    group_names: Dict[str, str] = {
        g["id"]: (g.get("name") or g["id"])
        for g in raw_groups if isinstance(g, dict) and g.get("id")
    }

    destinations = []
    for d in raw_destinations:
        if not isinstance(d, dict):
            continue
        status = d.get("setup_status", "")
        if status and status != "connected":
            continue
        dest_id = d.get("id", "")
        if not dest_id:
            continue
        destinations.append({
            "destination_id":   dest_id,
            "destination_type": normalize_destination_type(d.get("service", "")),
            "display_name":     group_names.get(dest_id) or dest_id,
        })

    if not destinations:
        print(json.dumps({"status": "error", "message": "no connected destinations found"}, separators=(",", ":")))
        sys.exit(1)

    # Pick destination
    if destination_id_override:
        chosen = next((d for d in destinations if d["destination_id"] == destination_id_override), None)
        if not chosen:
            print(f"[asa] destination '{destination_id_override}' not found", file=sys.stderr)
            sys.exit(1)
    elif len(destinations) == 1:
        chosen = destinations[0]
    else:
        _agent_print(
            {"status": "disambiguate_required", "suggested": destinations[0], "destinations": destinations},
            "Credentials verified. Return to your Claude Code chat to continue setup.",
        )
        return EXIT_DESTINATION_DISAMBIGUATE

    dest_id   = chosen["destination_id"]
    dest_type = chosen["destination_type"]
    WAREHOUSE_TOOL = {"bigquery": "bq", "snowflake": "snowflake_cli", "databricks": "databricks_cli"}
    warehouse_tool = WAREHOUSE_TOOL.get(dest_type, dest_type)

    # Fetch destination config, connections, and transformations in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        conf_fut      = pool.submit(_fetch_destination_detail, dest_id)
        conn_fut      = pool.submit(fetch_paginated, "/v1/connections",    group_id=dest_id, limit=1000)
        txfm_list_fut = pool.submit(fetch_paginated, "/v1/transformations", group_id=dest_id, type="QUICKSTART", limit=1000)
        raw_config, location = conf_fut.result()
        raw_connections       = conn_fut.result()
        raw_txfm_list         = txfm_list_fut.result()

    database = destination_database(dest_type, raw_config)

    # Filter to active ad-family connections
    all_connections = []
    for c in raw_connections:
        if not isinstance(c, dict) or c.get("service") not in REQUIRED_POOL:
            continue
        status = c.get("status") if isinstance(c.get("status"), dict) else {}
        is_active = (
            not c.get("paused", False)
            and status.get("setup_state") == "connected"
            and status.get("sync_state") in ACTIVE_SYNC_STATES
        )
        all_connections.append({
            "connection_id": c.get("id", ""),
            "service":       c.get("service", ""),
            "schema":        c.get("schema", "") or "",
            "sync_state":    status.get("sync_state", ""),
            "succeeded_at":  c.get("succeeded_at") or "",
            "active":        is_active,
        })

    # Fetch transformation details for active QUICKSTART transforms
    active_txfm_ids = [
        t["id"] for t in raw_txfm_list
        if isinstance(t, dict)
        and not t.get("paused", False)
        and t.get("status") in {"SUCCEEDED", "PARTIALLY_SUCCEEDED"}
        and t.get("id")
    ]

    # Fetch transformation details in small parallel batches to avoid rate-limiting the
    # Fivetran API while still being faster than pure sequential.  The list endpoint does
    # not expose package_name or connection_ids, so per-transformation detail calls are
    # required.  A batch size of 4 keeps concurrency bounded.
    _TXFM_BATCH = 4

    txfm_details: List[dict] = []
    for i in range(0, len(active_txfm_ids), _TXFM_BATCH):
        batch = active_txfm_ids[i:i + _TXFM_BATCH]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as pool:
            for detail in pool.map(_fetch_txfm_detail, batch):
                if not detail:
                    continue
                txfm_details.append(detail)

    # Build QDM registry: qdm_type -> detail dict
    qdm_registry: Dict[str, dict] = {}
    qdm_by_connection: Dict[str, List[str]] = {}

    for detail in txfm_details:
        cfg          = detail.get("transformation_config") or {}
        package_name = cfg.get("package_name") or ""
        conn_ids     = cfg.get("connection_ids") or []
        qdm_type     = _classify_transformation(package_name)
        if qdm_type is None:
            continue
        if qdm_type not in qdm_registry:
            qdm_registry[qdm_type] = {
                "qdm_type":          qdm_type,
                "package_name":      package_name,
                "output_model_names": list(detail.get("output_model_names") or []),
                "excluded_models":   list(cfg.get("excluded_models") or []),
                "last_ended_at":     detail.get("last_ended_at"),
                "connection_ids":    list(conn_ids),
            }
        for cid in conn_ids:
            if cid:
                qdm_by_connection.setdefault(cid, [])
                if qdm_type not in qdm_by_connection[cid]:
                    qdm_by_connection[cid].append(qdm_type)

    # Pick one connection per family — metadata only, no warehouse queries yet.
    # Disambig and insufficient-connectors checks must happen before any probes
    # so that fast early-exit paths don't pay warehouse latency.
    picks: Dict[str, dict] = {}       # family -> picked conn dict
    needs_disambig: Dict[str, list] = {}

    for family in sorted(REQUIRED_POOL | RECOMMENDED_POOL):
        if family in final_skip:
            continue
        family_conns = [c for c in all_connections if c["service"] == family]
        if not family_conns:
            continue

        if family in connection_overrides:
            override_id = connection_overrides[family]
            picked = next((c for c in family_conns if c["connection_id"] == override_id), None)
            if picked is None:
                print(json.dumps({"status": "error", "message": f"--connection {family}={override_id} not found"}, separators=(",", ":")))
                sys.exit(1)
        else:
            active = sorted(
                [c for c in family_conns if c["active"]],
                key=lambda c: c.get("succeeded_at") or "",
                reverse=True,
            )
            if not active:
                continue
            if len(active) == 1:
                picked = active[0]
            else:
                # Multiple active connections of the same service — require explicit selection
                # so customers running parallel QDMs aren't silently routed to the wrong one.
                # Sorted by succeeded_at so the most recent is first.
                needs_disambig[family] = [
                    {"connection_id": c["connection_id"], "schema": c["schema"],
                     "sync_state": c["sync_state"], "succeeded_at": c["succeeded_at"]}
                    for c in active
                ]
                continue

        picks[family] = picked

    if needs_disambig:
        _agent_print(
            {"status": "disambiguate_required", "families": needs_disambig},
            "Multiple active connections found for the same source. Return to your Claude Code chat to continue setup.",
        )
        return EXIT_CONNECTION_DISAMBIGUATE

    required_found = [f for f in picks if f in REQUIRED_POOL]
    if len(required_found) < min_required:
        _agent_print(
            {"status": "insufficient_connectors", "required_pool": sorted(REQUIRED_POOL), "found": required_found, "min_required_count": min_required},
            "No supported ad connectors found on this destination. Return to your Claude Code chat for details.",
        )
        return EXIT_INSUFFICIENT_CONNECTORS

    # Collect the QDM types needed for picked connections only, then probe in parallel.
    # Skip single-source probes for any connection already covered by the multisource
    # QDM: the resolve tier logic prefers multisource, so `single_source_schema` would
    # be populated but never read. This typically collapses 5+ probes down to 1.
    needed_qdm_types: set = set()
    for picked in picks.values():
        cid   = picked["connection_id"]
        types = qdm_by_connection.get(cid, [])
        has_ms = "multisource_ad_reporting" in types and "multisource_ad_reporting" in qdm_registry
        for qt in types:
            if qt not in qdm_registry:
                continue
            if has_ms and qt.startswith("single_source_"):
                continue
            needed_qdm_types.add(qt)
    qdm_schema_candidates: Dict[str, List[str]] = {}
    needed_list = list(needed_qdm_types)
    if needed_list:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(needed_list))) as pool:
            futures = {
                pool.submit(
                    _probe_schema,
                    dest_type, database, location,
                    qdm_registry[qt]["output_model_names"],
                ): qt
                for qt in needed_list
            }
            for fut in concurrent.futures.as_completed(futures):
                qt = futures[fut]
                try:
                    qdm_schema_candidates[qt] = fut.result()
                except Exception:
                    qdm_schema_candidates[qt] = []

    # If any QDM type has multiple matching schemas and no override, ask the user to pick.
    # This must happen before outputting connections/QDMs so setup exits cleanly.
    needs_schema_disambig: Dict[str, List[str]] = {
        qt: candidates
        for qt, candidates in qdm_schema_candidates.items()
        if len(candidates) > 1 and qt not in final_schema_overrides
    }
    if needs_schema_disambig:
        _agent_print(
            {"status": "disambiguate_required", "schemas": needs_schema_disambig},
            "Multiple schemas found. Return to your Claude Code chat to continue setup.",
        )
        return EXIT_SCHEMA_DISAMBIGUATE

    # Resolve candidates to a single value per QDM type, applying any overrides.
    qdm_schemas: Dict[str, Optional[str]] = {}
    for qt, candidates in qdm_schema_candidates.items():
        if qt in final_schema_overrides:
            qdm_schemas[qt] = final_schema_overrides[qt]
        elif len(candidates) == 1:
            qdm_schemas[qt] = candidates[0]
        else:
            qdm_schemas[qt] = None

    # Build connectors dict from picks + probed schemas.
    connectors: Dict[str, dict] = {}
    for family, picked in picks.items():
        cid            = picked["connection_id"]
        conn_qdm_types = qdm_by_connection.get(cid, [])
        ms_type  = next((t for t in conn_qdm_types if t == "multisource_ad_reporting"),  None)
        ss_type  = next((t for t in conn_qdm_types if t == f"single_source_{family}"),   None)

        if ms_type:
            model_tier, qdm_rec = "multisource",   qdm_registry[ms_type]
        elif ss_type:
            model_tier, qdm_rec = "single_source", qdm_registry[ss_type]
        else:
            model_tier, qdm_rec = "raw",           None

        unified_schema       = qdm_schemas.get(ms_type) if ms_type else None
        single_source_schema = qdm_schemas.get(ss_type) if ss_type else None

        # qdm_functional: True when the expected schema was found (or can't probe)
        if MOCK_FETCHER:
            qdm_functional = True
        elif model_tier == "multisource":
            qdm_functional = unified_schema is not None
        elif model_tier == "single_source":
            qdm_functional = single_source_schema is not None
        else:
            qdm_functional = True

        connectors[family] = {
            "connection_id":        cid,
            "raw_schema":           picked["schema"],
            "model_tier":           model_tier,
            "unified_schema":       unified_schema,
            "single_source_schema": single_source_schema,
            "active_models":        list(qdm_rec["output_model_names"]) if qdm_rec else [],
            "excluded_models":      list(qdm_rec["excluded_models"])     if qdm_rec else [],
            "last_ended_at":        qdm_rec["last_ended_at"]             if qdm_rec else None,
            "qdm_functional":       qdm_functional,
        }

    # Read skill version from plugin.json if present
    script_dir  = os.path.dirname(os.path.abspath(__file__))
    plugin_path = os.path.join(script_dir, "..", ".claude-plugin", "plugin.json")
    skill_version = "0.1.0"
    try:
        with open(plugin_path, "r", encoding="utf-8") as f:
            pdata = json.load(f)
        v = pdata.get("version")
        if isinstance(v, str) and v:
            skill_version = v
    except Exception:
        pass

    profile = {
        "config_version": PROFILE_VERSION,
        "install_id":    uuid.uuid4().hex,
        "discovered_at": _now_utc(),
        "skill":         {"id": skill_id, "version": skill_version},
        "destination":   {
            "destination_id":   dest_id,
            "destination_type": dest_type,
            "warehouse_tool":   warehouse_tool,
            "database":         database,
            "location":         location,
        },
        "skipped_families": sorted(final_skip),
        "schema_overrides": final_schema_overrides,
        "connectors": connectors,
    }
    _write_profile(profile)

    # Print structured summary to stdout
    ms_qdm = qdm_registry.get("multisource_ad_reporting")
    multi_source_qdms = []
    if ms_qdm and qdm_schemas.get("multisource_ad_reporting"):
        linked = sorted(f for f, e in connectors.items() if e["model_tier"] == "multisource")
        multi_source_qdms.append({
            "package":         "ad_reporting",
            "schema":          qdm_schemas["multisource_ad_reporting"],
            "linked_families": linked,
            "active_models":   ms_qdm["output_model_names"],
            "excluded_models": ms_qdm["excluded_models"],
            "last_ended_at":   ms_qdm["last_ended_at"],
            "qdm_functional":  True,
        })

    single_source_qdms = []
    for qdm_type, qdm in qdm_registry.items():
        if not qdm_type.startswith("single_source_"):
            continue
        family = qdm_type.removeprefix("single_source_")
        if any(e["model_tier"] == "single_source" and f == family for f, e in connectors.items()):
            single_source_qdms.append({
                "family":        family,
                "schema":        qdm_schemas.get(qdm_type),
                "active_models": qdm["output_model_names"],
                "last_ended_at": qdm["last_ended_at"],
                "qdm_functional": qdm_schemas.get(qdm_type) is not None,
            })

    _agent_print(
        {
            "status":            "ok",
            "profile_path":      _profile_path(),
            "destination":       profile["destination"],
            "connections":       [
                {"family": f, "connection_id": e["connection_id"], "schema": e["raw_schema"], "model_tier": e["model_tier"]}
                for f, e in sorted(connectors.items())
            ],
            "single_source_qdms": single_source_qdms,
            "multi_source_qdms":  multi_source_qdms,
        },
        "Fivetran profile saved. Return to your Claude Code chat to continue.",
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# `resolve` subcommand
# ---------------------------------------------------------------------------

def cmd_resolve(family: str, refresh_on_miss: bool) -> int:
    raw = _read_profile()
    if raw is None:
        print("[asa] profile missing — run setup first", file=sys.stderr)
        return EXIT_PROFILE_MISSING
    if not raw or not _is_valid_profile(raw):
        print("[asa] profile invalid — re-run setup", file=sys.stderr)
        return EXIT_PROFILE_INVALID

    if refresh_on_miss:
        skill_id = raw.get("skill", {}).get("id", "ad-performance-analysis")
        if raw.get("setup_method") == "warehouse_discovery":
            # Manual profiles have no Fivetran API credentials — never prompt for
            # one on refresh. Re-run the same warehouse probe that built the
            # profile, using its stored discovery inputs.
            inputs = raw.get("discovery_inputs") or {}
            if inputs.get("warehouse_tool") and inputs.get("database"):
                cmd_discover(
                    warehouse_tool=inputs["warehouse_tool"],
                    database=inputs["database"],
                    location=inputs.get("location"),
                    schema_hints=inputs.get("schema_hints") or [],
                    schema_overrides=raw.get("schema_overrides") or {},
                    skill_id=skill_id,
                )
                raw = _read_profile() or raw
        elif _resolve_credentials():
            dest_id = raw.get("destination", {}).get("destination_id")
            cmd_setup(
                destination_id_override=dest_id,
                connection_overrides={},
                skill_id=skill_id,
                refresh=True,
            )
            raw = _read_profile() or raw

    skipped = raw.get("skipped_families") or []
    if family in skipped:
        print(f"[asa] connector family '{family}' was skipped during setup — re-run setup without --skip-family to include it", file=sys.stderr)
        return EXIT_PROFILE_INVALID

    connectors = raw.get("connectors", {})
    if family not in connectors:
        print(f"[asa] connector family '{family}' not configured — re-run setup", file=sys.stderr)
        return EXIT_PROFILE_INVALID

    entry = connectors[family]
    dest  = raw.get("destination", {})

    tier           = entry.get("model_tier") or "raw"
    qdm_functional = bool(entry.get("qdm_functional", True))
    declared_tier  = tier

    if not qdm_functional and tier in ("multisource", "single_source"):
        tier = "raw"

    print(json.dumps({
        "connector_family":    family,
        "connection_id":       entry.get("connection_id"),
        "destination_type":    dest.get("destination_type"),
        "warehouse_tool":      dest.get("warehouse_tool"),
        "database":            dest.get("database"),
        "location":            dest.get("location", "US"),
        "raw_schema":          entry.get("raw_schema"),
        "model_tier":          tier,
        "unified_schema":      entry.get("unified_schema"),
        "single_source_schema": entry.get("single_source_schema"),
        "active_models":       list(entry.get("active_models") or []),
        "excluded_models":     list(entry.get("excluded_models") or []),
        "qdm_last_ended_at":   entry.get("last_ended_at"),
        "qdm_functional":      qdm_functional,
        "qdm_degraded":        (not qdm_functional and declared_tier in ("multisource", "single_source")),
        "qdm_declared_tier":   declared_tier,
    }, separators=(",", ":")))
    return EXIT_OK


# ---------------------------------------------------------------------------
# `check-cli` subcommand
# ---------------------------------------------------------------------------

_CLI_INFO = {
    "bq": {
        "binary":      "bq",
        "missing_msg": "install Google Cloud SDK: https://cloud.google.com/sdk/docs/install   (macOS Homebrew: brew install --cask google-cloud-sdk)",
        "unauth_msg":  "gcloud auth login && gcloud auth application-default login",
        "auth_cmd":    ["bq", "query", "--use_legacy_sql=false", "--max_rows=1", "SELECT 1"],
    },
    "snowflake_cli": {
        "binary":      "snow",
        "missing_msg": "install Snowflake CLI: https://docs.snowflake.com/en/developer-guide/snowflake-cli/installation/installation   (macOS Homebrew: brew install snowflake-cli)",
        "unauth_msg":  "snow connection add  # or update your existing connection's credentials",
        "auth_cmd":    ["snow", "connection", "test"],
    },
    "databricks_cli": {
        "binary":      "databricks",
        "missing_msg": "install Databricks CLI: https://docs.databricks.com/en/dev-tools/cli/install.html   (macOS Homebrew: brew install databricks)",
        "unauth_msg":  "databricks auth login",
        "auth_cmd":    ["databricks", "current-user", "me"],
    },
}


def cmd_check_cli(tool: str) -> int:
    info = _CLI_INFO.get(tool)
    if not info:
        print(f"[asa] unknown tool: {tool!r}", file=sys.stderr)
        sys.exit(1)

    if subprocess.run(["which", info["binary"]], capture_output=True).returncode != 0:
        print(info["missing_msg"])
        return EXIT_CLI_MISSING

    probe = None
    try:
        probe = subprocess.run(info["auth_cmd"], capture_output=True, text=True, timeout=20)
        auth_ok = probe.returncode == 0
    except Exception:
        auth_ok = False
    if not auth_ok:
        if tool == "databricks_cli":
            stderr = (probe.stderr or "").strip() if probe is not None else ""
            msg = _databricks_credential_access_message(stderr) or info["unauth_msg"]
            print(msg)
        else:
            print(info["unauth_msg"])
        return EXIT_CLI_UNAUTH

    print(f"{tool} ready")
    return EXIT_OK


# ---------------------------------------------------------------------------
# `readiness` subcommand
# ---------------------------------------------------------------------------

# Tables whose date column is `date_month` instead of `date_day`.
_MONTHLY_TABLE_MARKER = "monthly_"  # signals date_month instead of date_day


def _readiness_query_bq(project: str, schema: str, table: str, timeout: int = 30) -> Optional[List[dict]]:
    date_col = "date_month" if _MONTHLY_TABLE_MARKER in table else "date_day"
    sql = (
        f"SELECT platform, MAX({date_col}) AS latest_date, COUNT(*) AS row_count "
        f"FROM `{project}.{schema}.{table}` GROUP BY platform"
    )
    return _bq_query(sql, timeout=timeout, raise_on_error=True)


def _readiness_query_snow(database: str, schema: str, table: str, timeout: int = 30) -> Optional[List]:
    date_col = "date_month" if _MONTHLY_TABLE_MARKER in table else "date_day"
    sql = (
        f"SELECT platform, MAX({date_col}) AS latest_date, COUNT(*) AS rows "
        f"FROM {database}.{schema}.{table} GROUP BY platform"
    )
    return _snow_query(sql, timeout=timeout, raise_on_error=True)


def _readiness_query_databricks(catalog: str, schema: str, table: str, timeout: int = 30) -> Optional[List]:
    date_col = "date_month" if _MONTHLY_TABLE_MARKER in table else "date_day"
    sql = (
        f"SELECT platform, MAX({date_col}) AS latest_date, COUNT(*) AS rows "
        f"FROM `{catalog}`.`{schema}`.`{table}` GROUP BY platform"
    )
    return _databricks_query(sql, timeout=timeout, raise_on_error=True)


def _probe_table_freshness(
    dest_type: str, database: str, schema: str, table: str
) -> Tuple[str, str, List[dict], Optional[str], Optional[dict]]:
    """Returns (schema, table, rows, error_message, remediation)."""
    try:
        if dest_type == "bigquery":
            raw = _readiness_query_bq(database, schema, table)
        elif dest_type == "snowflake":
            raw = _readiness_query_snow(database, schema, table)
        elif dest_type == "databricks":
            raw = _readiness_query_databricks(database, schema, table)
        else:
            return schema, table, [], f"unsupported warehouse: {dest_type}", None
        if raw is None:
            return schema, table, [], "query failed", None
        rows = []
        for r in raw:
            if isinstance(r, dict):
                rows.append({
                    "platform":    str(r.get("platform") or ""),
                    "latest_date": str(r.get("latest_date") or ""),
                    "rows":        int(r.get("row_count") or r.get("rows") or 0),
                })
            elif isinstance(r, list) and len(r) >= 3:
                rows.append({"platform": str(r[0]), "latest_date": str(r[1]), "rows": int(r[2] or 0)})
        return schema, table, rows, None, None
    except Exception as exc:
        msg = str(exc)
        remediation = _databricks_error_remediation(msg) if dest_type == "databricks" else None
        if remediation:
            msg = remediation["message"]
        return schema, table, [], msg, remediation


def cmd_readiness(family_filter: Optional[List[str]] = None) -> int:
    raw = _read_profile()
    if raw is None:
        print("[asa] profile missing — run setup first", file=sys.stderr)
        return EXIT_PROFILE_MISSING
    if not raw or not _is_valid_profile(raw):
        print("[asa] profile invalid — re-run setup", file=sys.stderr)
        return EXIT_PROFILE_INVALID

    dest     = raw.get("destination", {})
    dest_type = dest.get("destination_type", "")
    database  = dest.get("database", "")
    connectors = raw.get("connectors", {})

    # Collect (schema, table) pairs to probe — multisource and single_source only.
    # Build a set to deduplicate: multisource schema is shared across families.
    seen: set = set()
    probes: List[Tuple[str, str]] = []
    qdm_last_ended_at: Dict[str, str] = {}

    for family, entry in sorted(connectors.items()):
        if family_filter and family not in family_filter:
            continue
        tier   = entry.get("model_tier")
        schema = (
            entry.get("unified_schema") if tier == "multisource"
            else entry.get("single_source_schema") if tier == "single_source"
            else None
        )
        if not schema:
            continue
        for model in (entry.get("active_models") or []):
            key = (schema, model)
            if key not in seen:
                seen.add(key)
                probes.append(key)
        if entry.get("last_ended_at"):
            qdm_last_ended_at[family] = entry["last_ended_at"]

    if not probes:
        print(json.dumps({
            "status": "no_qdm",
            "message": "no multisource or single_source connectors with active models found",
            "destination": {"database": database, "warehouse_tool": dest.get("warehouse_tool")},
        }, separators=(",", ":")))
        return EXIT_OK

    freshness_rows: List[dict] = []
    errors: List[dict] = []
    remediations: List[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(probes))) as pool:
        futures = {
            pool.submit(_probe_table_freshness, dest_type, database, schema, table): (schema, table)
            for schema, table in probes
        }
        for fut in concurrent.futures.as_completed(futures):
            schema, table, rows, err, remediation = fut.result()
            if err:
                errors.append({"table": table, "schema": schema, "message": err})
                if remediation:
                    remediations.append(remediation)
                print(f"[asa] warn: readiness probe failed for {schema}.{table}: {err}", file=sys.stderr)
            else:
                for r in rows:
                    freshness_rows.append({"schema": schema, "table": table, **r})

    freshness_rows.sort(key=lambda r: (r["table"], r["platform"]))
    remediation: Optional[dict] = None
    if remediations:
        codes = {r.get("code") for r in remediations}
        if len(codes) == 1:
            remediation = remediations[0]

    print(json.dumps({
        "status":           "ok",
        "destination":      {"database": database, "warehouse_tool": dest.get("warehouse_tool")},
        "freshness":        freshness_rows,
        "errors":           errors,
        "remediation":      remediation,
        "qdm_last_ended_at": qdm_last_ended_at,
    }, separators=(",", ":")))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: asa.py <validate|setup|discover|list-schemas|resolve|check-cli> [args...]", file=sys.stderr)
        return 1

    subcmd, rest = args[0], args[1:]

    if subcmd == "validate":
        return cmd_validate()

    if subcmd == "setup":
        destination_id: Optional[str]     = None
        connection_overrides: Dict[str, str] = {}
        schema_overrides_arg: Dict[str, str] = {}
        skip_families: set = set()
        clear_skip = False
        clear_schema_overrides = False
        skill_id = "ad-performance-analysis"
        refresh  = False
        i = 0
        while i < len(rest):
            arg = rest[i]
            if arg == "--destination-id" and i + 1 < len(rest):
                destination_id = rest[i + 1]; i += 2
            elif arg == "--connection" and i + 1 < len(rest):
                pair = rest[i + 1]
                if "=" not in pair:
                    print(f"[asa] --connection requires FAM=ID, got: {pair!r}", file=sys.stderr)
                    return 1
                fam, cid = pair.split("=", 1)
                connection_overrides[fam.strip()] = cid.strip(); i += 2
            elif arg == "--schema" and i + 1 < len(rest):
                pair = rest[i + 1]
                if "=" not in pair:
                    print(f"[asa] --schema requires QDM_TYPE=SCHEMA_NAME, got: {pair!r}", file=sys.stderr)
                    return 1
                qt, sname = pair.split("=", 1)
                schema_overrides_arg[qt.strip()] = sname.strip(); i += 2
            elif arg == "--skip-family" and i + 1 < len(rest):
                skip_families.add(rest[i + 1].strip()); i += 2
            elif arg == "--no-skip":
                clear_skip = True; i += 1
            elif arg == "--no-schema":
                clear_schema_overrides = True; i += 1
            elif arg == "--skill" and i + 1 < len(rest):
                skill_id = rest[i + 1]; i += 2
            elif arg == "--refresh":
                refresh = True; i += 1
            else:
                print(f"[asa] unknown argument: {arg!r}", file=sys.stderr)
                return 1
        return cmd_setup(destination_id, connection_overrides, skill_id, refresh, skip_families, clear_skip, schema_overrides_arg or None, clear_schema_overrides)

    if subcmd == "list-schemas":
        ls_warehouse: Optional[str] = None
        ls_database:  Optional[str] = None
        i = 0
        while i < len(rest):
            if rest[i] == "--warehouse" and i + 1 < len(rest):
                ls_warehouse = rest[i + 1]; i += 2
            elif rest[i] == "--database" and i + 1 < len(rest):
                ls_database = rest[i + 1]; i += 2
            else:
                print(f"[asa] unknown argument: {rest[i]!r}", file=sys.stderr)
                return 1
        if not ls_warehouse or not ls_database:
            print("usage: asa.py list-schemas --warehouse <bq|snowflake_cli|databricks_cli> "
                  "--database <project|database|catalog>", file=sys.stderr)
            return 1
        return cmd_list_schemas(ls_warehouse, ls_database)

    if subcmd == "discover":
        warehouse_tool: Optional[str] = None
        database: Optional[str] = None
        location: Optional[str] = None
        schema_hints: List[str] = []
        schema_overrides_disc: Dict[str, str] = {}
        skill_id = "ad-performance-analysis"
        i = 0
        while i < len(rest):
            arg = rest[i]
            if arg == "--warehouse" and i + 1 < len(rest):
                warehouse_tool = rest[i + 1]; i += 2
            elif arg == "--database" and i + 1 < len(rest):
                database = rest[i + 1]; i += 2
            elif arg == "--location" and i + 1 < len(rest):
                location = rest[i + 1]; i += 2
            elif arg == "--schema" and i + 1 < len(rest):
                schema_hints.append(rest[i + 1]); i += 2
            elif arg == "--schema-override" and i + 1 < len(rest):
                pair = rest[i + 1]
                if "=" not in pair:
                    print(f"[asa] --schema-override requires KEY=SCHEMA_NAME, got: {pair!r}", file=sys.stderr)
                    return 1
                key, sname = pair.split("=", 1)
                schema_overrides_disc[key.strip()] = sname.strip(); i += 2
            elif arg == "--skill" and i + 1 < len(rest):
                skill_id = rest[i + 1]; i += 2
            else:
                print(f"[asa] unknown argument: {arg!r}", file=sys.stderr)
                return 1
        if not warehouse_tool or not database:
            print("usage: asa.py discover --warehouse <bq|snowflake_cli|databricks_cli> --database <name> "
                  "[--location <region>] [--schema <name> ...] [--schema-override KEY=SCHEMA ...] [--skill <id>]",
                  file=sys.stderr)
            return 1
        return cmd_discover(warehouse_tool, database, location, schema_hints, schema_overrides_disc, skill_id)

    if subcmd == "resolve":
        if not rest:
            print("usage: asa.py resolve <family> [--refresh-on-miss]", file=sys.stderr)
            return 1
        return cmd_resolve(rest[0], "--refresh-on-miss" in rest[1:])

    if subcmd == "check-cli":
        if not rest:
            print("usage: asa.py check-cli <bq|snowflake_cli|databricks_cli>", file=sys.stderr)
            return 1
        return cmd_check_cli(rest[0])

    if subcmd == "readiness":
        family_filter = [a for a in rest if not a.startswith("--")] or None
        return cmd_readiness(family_filter)

    print(f"[asa] unknown subcommand: {subcmd!r}", file=sys.stderr)
    return 1


def _entrypoint() -> int:
    try:
        return main()
    except (KeyboardInterrupt, EOFError):
        print("\n[asa] cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    try:
        sys.exit(_entrypoint())
    except SystemExit:
        raise
    except Exception as exc:
        tb = traceback.format_exc()
        log_path = _write_error_log(tb)
        print(f"[asa] unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if log_path:
            print(
                f"[asa] full traceback written to {log_path}\n"
                f"      (set ASA_DEBUG=1 to print it here)",
                file=sys.stderr,
            )
        if os.environ.get("ASA_DEBUG"):
            print(tb, file=sys.stderr)
        sys.exit(1)
