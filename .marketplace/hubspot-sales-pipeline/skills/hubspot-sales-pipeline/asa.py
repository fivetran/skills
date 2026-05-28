#!/usr/bin/env python3
"""
asa.py — hubspot-sales-pipeline entry point.

Subcommands:
  validate                                    # 0=ok | 60=missing | 61=invalid
  setup [--destination-id X]                 # 0=ok | 51/52/53/54=disambiguate | 62=creds missing (non-tty)
        [--connection FAM=ID ...]
        [--schema QDM_TYPE=SCHEMA_NAME ...]  # override schema for a QDM type (persisted)
        [--skip-family FAM ...]              # skip a family (persisted across refreshes)
        [--no-skip]                          # clear all persisted skips
        [--no-schema]                        # clear all persisted schema overrides
        [--refresh] [--skill <id>]
  resolve <family> [--refresh-on-miss]       # prints JSON to stdout
  readiness [FAM ...]                         # parallel data-freshness probe across active_models
  check-cli <bq|snowflake_cli|databricks_cli> # 0=ok | 70=missing | 71=unauth
"""

import base64
import concurrent.futures
import datetime
import getpass
import json
import os
import re
import ssl
import subprocess
import sys
import time
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

PACKAGE_TO_FAMILY: Dict[str, str] = {}

ACTIVE_SYNC_STATES = {"scheduled", "syncing", "rescheduled"}

REQUIRED_POOL    = _readiness_required_pool(_CFG)
RECOMMENDED_POOL: set = set()

SKILL_MIN_REQUIRED: Dict[str, int] = _readiness_skill_min_required(_CFG)

# ---------------------------------------------------------------------------
# Environment config
# ---------------------------------------------------------------------------
API_BASE      = os.environ.get("FIVETRAN_API_BASE_URL", "https://api.fivetran.com").rstrip("/")
MOCK_FETCHER  = os.environ.get("ASA_FIVETRAN_FETCHER", "")
_CURRENT_TOKEN: Optional[str] = None  # set by cmd_setup before any HTTP request


def _config_dir() -> str:
    if os.environ.get("HUBSPOT_SALES_PIPELINE_CONFIG_DIR"):
        return os.environ["HUBSPOT_SALES_PIPELINE_CONFIG_DIR"]
    local = "./.fivetran/hubspot-sales-pipeline"
    if os.path.isdir(local):
        return local
    return os.path.join(os.path.expanduser("~"), ".fivetran", "skills", "hubspot-sales-pipeline")


def _profile_path() -> str:
    if os.environ.get("HUBSPOT_SALES_PIPELINE_PROFILE_PATH"):
        return os.environ["HUBSPOT_SALES_PIPELINE_PROFILE_PATH"]
    return os.path.join(_config_dir(), "profile.json")


def _creds_path() -> str:
    return os.path.join(_config_dir(), "credentials.json")


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
# in skills/ad-performance/asa.py and skills/store-performance/asa.py.
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
# Normalisation helpers
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



def _agent_print(payload: dict, tty_message: str) -> None:
    """Print JSON when stdout is not a tty (agent context), friendly message otherwise."""
    if sys.stdout.isatty():
        print(tty_message)
    else:
        print(json.dumps(payload, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Warehouse query helpers (used by schema probe)
# ---------------------------------------------------------------------------

def _bq_query(sql: str, timeout: int = 30) -> Optional[List[dict]]:
    r = subprocess.run(
        ["bq", "query", "--use_legacy_sql=false", "--format=prettyjson", "--quiet", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        print(f"[asa] warn: bq query failed: {r.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(r.stdout.strip() or "[]")


def _snow_query(sql: str, timeout: int = 30) -> Optional[List]:
    r = subprocess.run(
        ["snow", "sql", "-q", sql, "--output-format", "json"],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        return None
    return json.loads(r.stdout.strip() or "[]")


def _databricks_query(sql: str, timeout: int = 30) -> Optional[List]:
    r = subprocess.run(
        ["databricks", "sql", "execute", "--sql", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
        return data.get("result", {}).get("data_array") or []
    except Exception:
        return None


# ---------------------------------------------------------------------------
# QDM schema probe
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        conf_fut      = pool.submit(_fetch_destination_detail, dest_id)
        conn_fut      = pool.submit(fetch_paginated, "/v1/connections",    group_id=dest_id, limit=1000)
        txfm_list_fut = pool.submit(fetch_paginated, "/v1/transformations", group_id=dest_id, type="QUICKSTART", limit=1000)
        raw_config, location = conf_fut.result()
        raw_connections       = conn_fut.result()
        raw_txfm_list         = txfm_list_fut.result()

    database = destination_database(dest_type, raw_config)

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
            "active":        is_active,
        })

    active_txfm_ids = [
        t["id"] for t in raw_txfm_list
        if isinstance(t, dict)
        and not t.get("paused", False)
        and t.get("status") in {"SUCCEEDED", "PARTIALLY_SUCCEEDED"}
        and t.get("id")
    ]

    txfm_details: List[dict] = []
    if active_txfm_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            futures = {pool.submit(_fetch_txfm_detail, tid): tid for tid in active_txfm_ids}
            for fut in concurrent.futures.as_completed(futures):
                result = fut.result()
                if result:
                    txfm_details.append(result)

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

    picks: Dict[str, dict] = {}
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
            active = [c for c in family_conns if c["active"]]
            if not active:
                continue
            exact = [c for c in active if c.get("schema", "").lower() == family.lower()]
            if len(exact) == 1:
                picked = exact[0]
            elif len(active) == 1:
                picked = active[0]
            else:
                needs_disambig[family] = [
                    {"connection_id": c["connection_id"], "schema": c["schema"], "sync_state": c["sync_state"]}
                    for c in active[:5]
                ]
                continue

        picks[family] = picked

    if needs_disambig:
        _agent_print(
            {"status": "disambiguate_required", "families": needs_disambig},
            "Credentials verified. Return to your Claude Code chat to continue setup.",
        )
        return EXIT_CONNECTION_DISAMBIGUATE

    required_found = [f for f in picks if f in REQUIRED_POOL]
    if len(required_found) < min_required:
        _agent_print(
            {"status": "insufficient_connectors", "required_pool": sorted(REQUIRED_POOL), "found": required_found, "min_required_count": min_required},
            "No supported HubSpot connector found on this destination. Return to your Claude Code chat for details.",
        )
        return EXIT_INSUFFICIENT_CONNECTORS

    needed_qdm_types: set = set()
    for picked in picks.values():
        cid   = picked["connection_id"]
        types = qdm_by_connection.get(cid, [])
        for qt in types:
            if qt not in qdm_registry:
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

    qdm_schemas: Dict[str, Optional[str]] = {}
    for qt, candidates in qdm_schema_candidates.items():
        if qt in final_schema_overrides:
            qdm_schemas[qt] = final_schema_overrides[qt]
        elif len(candidates) == 1:
            qdm_schemas[qt] = candidates[0]
        else:
            qdm_schemas[qt] = None

    connectors: Dict[str, dict] = {}
    for family, picked in picks.items():
        cid            = picked["connection_id"]
        conn_qdm_types = qdm_by_connection.get(cid, [])
        ss_type  = next((t for t in conn_qdm_types if t == f"single_source_{family}"), None)

        if ss_type:
            model_tier, qdm_rec = "single_source", qdm_registry[ss_type]
        else:
            model_tier, qdm_rec = "raw", None

        single_source_schema = qdm_schemas.get(ss_type) if ss_type else None

        if MOCK_FETCHER:
            qdm_functional = True
        elif model_tier == "single_source":
            qdm_functional = single_source_schema is not None
        else:
            qdm_functional = True

        connectors[family] = {
            "connection_id":        cid,
            "raw_schema":           picked["schema"],
            "model_tier":           model_tier,
            "unified_schema":       None,
            "single_source_schema": single_source_schema,
            "active_models":        list(qdm_rec["output_model_names"]) if qdm_rec else [],
            "excluded_models":      list(qdm_rec["excluded_models"])     if qdm_rec else [],
            "last_ended_at":        qdm_rec["last_ended_at"]             if qdm_rec else None,
            "qdm_functional":       qdm_functional,
        }

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
            "multi_source_qdms":  [],
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

    if refresh_on_miss and _resolve_credentials():
        dest_id  = raw.get("destination", {}).get("destination_id")
        skill_id = raw.get("skill", {}).get("id", "hubspot-sales-pipeline")
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
        "unauth_msg":  "snow connection add  (or 'snow connection test --connection <name>')",
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

    try:
        auth_ok = subprocess.run(info["auth_cmd"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        auth_ok = False
    if not auth_ok:
        print(info["unauth_msg"])
        return EXIT_CLI_UNAUTH

    print(f"{tool} ready")
    return EXIT_OK


# ---------------------------------------------------------------------------
# `readiness` subcommand
# ---------------------------------------------------------------------------

# Per-table date column overrides. All other hubspot__ tables default to created_date.
_DATE_COL_MAP = {
    "hubspot__deal_stages":   "date_stage_entered",
    "hubspot__deal_history":  "valid_from",
    "hubspot__engagements":   "_fivetran_synced",
}
_DEFAULT_DATE_COL = "created_date"


def _readiness_query_bq(project: str, schema: str, table: str, timeout: int = 30) -> Optional[List[dict]]:
    date_col = _DATE_COL_MAP.get(table, _DEFAULT_DATE_COL)
    sql = (
        f"SELECT source_relation, MAX({date_col}) AS latest_date, COUNT(*) AS row_count "
        f"FROM `{project}.{schema}.{table}` GROUP BY source_relation"
    )
    return _bq_query(sql, timeout=timeout)


def _readiness_query_snow(database: str, schema: str, table: str, timeout: int = 30) -> Optional[List]:
    date_col = _DATE_COL_MAP.get(table, _DEFAULT_DATE_COL)
    sql = (
        f"SELECT source_relation, MAX({date_col}) AS latest_date, COUNT(*) AS rows "
        f"FROM {database}.{schema}.{table} GROUP BY source_relation"
    )
    return _snow_query(sql, timeout=timeout)


def _readiness_query_databricks(catalog: str, schema: str, table: str, timeout: int = 30) -> Optional[List]:
    date_col = _DATE_COL_MAP.get(table, _DEFAULT_DATE_COL)
    sql = (
        f"SELECT source_relation, MAX({date_col}) AS latest_date, COUNT(*) AS rows "
        f"FROM {catalog}.{schema}.{table} GROUP BY source_relation"
    )
    return _databricks_query(sql, timeout=timeout)


def _probe_table_freshness(
    dest_type: str, database: str, schema: str, table: str
) -> Tuple[str, str, List[dict], Optional[str]]:
    """Returns (schema, table, rows, error_message)."""
    try:
        if dest_type == "bigquery":
            raw = _readiness_query_bq(database, schema, table)
        elif dest_type == "snowflake":
            raw = _readiness_query_snow(database, schema, table)
        elif dest_type == "databricks":
            raw = _readiness_query_databricks(database, schema, table)
        else:
            return schema, table, [], f"unsupported warehouse: {dest_type}"
        if raw is None:
            return schema, table, [], "query failed"
        rows = []
        for r in raw:
            if isinstance(r, dict):
                rows.append({
                    "source_relation": str(r.get("source_relation") or ""),
                    "latest_date":     str(r.get("latest_date") or ""),
                    "rows":            int(r.get("row_count") or r.get("rows") or 0),
                })
            elif isinstance(r, list) and len(r) >= 3:
                rows.append({"source_relation": str(r[0]), "latest_date": str(r[1]), "rows": int(r[2] or 0)})
        return schema, table, rows, None
    except Exception as exc:
        return schema, table, [], str(exc)


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

    seen: set = set()
    probes: List[Tuple[str, str]] = []
    qdm_last_ended_at: Dict[str, str] = {}

    for family, entry in sorted(connectors.items()):
        if family_filter and family not in family_filter:
            continue
        tier   = entry.get("model_tier")
        schema = (
            entry.get("single_source_schema") if tier == "single_source"
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
            "message": "no single_source connectors with active models found",
            "destination": {"database": database, "warehouse_tool": dest.get("warehouse_tool")},
        }, separators=(",", ":")))
        return EXIT_OK

    freshness_rows: List[dict] = []
    errors: List[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(probes))) as pool:
        futures = {
            pool.submit(_probe_table_freshness, dest_type, database, schema, table): (schema, table)
            for schema, table in probes
        }
        for fut in concurrent.futures.as_completed(futures):
            schema, table, rows, err = fut.result()
            if err:
                errors.append({"table": table, "schema": schema, "message": err})
                print(f"[asa] warn: readiness probe failed for {schema}.{table}: {err}", file=sys.stderr)
            else:
                for r in rows:
                    freshness_rows.append({"schema": schema, "table": table, **r})

    freshness_rows.sort(key=lambda r: (r["table"], r["source_relation"]))

    print(json.dumps({
        "status":           "ok",
        "destination":      {"database": database, "warehouse_tool": dest.get("warehouse_tool")},
        "freshness":        freshness_rows,
        "errors":           errors,
        "qdm_last_ended_at": qdm_last_ended_at,
    }, separators=(",", ":")))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: asa.py <validate|setup|resolve|check-cli> [args...]", file=sys.stderr)
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
        skill_id = "hubspot-sales-pipeline"
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


if __name__ == "__main__":
    sys.exit(main())
