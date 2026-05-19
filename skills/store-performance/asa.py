#!/usr/bin/env python3
"""
asa.py — unified store-performance entry point.

Subcommands:
  validate                                    # 0=ok | 60=missing | 61=invalid
  setup [--destination-id X]                 # 0=ok | 51/52/53=disambiguate | 62=creds missing (non-tty)
        [--connection FAM=ID ...]
        [--skip-family FAM ...]              # skip a family (persisted across refreshes)
        [--no-skip]                          # clear all persisted skips
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
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_OK                       = 0
EXIT_DESTINATION_DISAMBIGUATE = 51
EXIT_CONNECTION_DISAMBIGUATE  = 52
EXIT_INSUFFICIENT_CONNECTORS  = 53
EXIT_PROFILE_MISSING          = 60
EXIT_PROFILE_INVALID          = 61
EXIT_CREDS_MISSING            = 62
EXIT_CLI_MISSING              = 70
EXIT_CLI_UNAUTH               = 71

# ---------------------------------------------------------------------------
# Constants (preserved from discover_fivetran.py)
# ---------------------------------------------------------------------------
PROFILE_VERSION = "4.0"

# E-commerce connector pool. Shopify is v1; future: woocommerce, bigcommerce, recharge.
ALL_ECOMMERCE_FAMILIES = {
    "shopify",
}

# No package-to-family aliases for v1. (Add here if Fivetran's package name
# differs from the connector service name — e.g. ad-spend has linkedin → linkedin_ads.)
PACKAGE_TO_FAMILY: Dict[str, str] = {}

ACTIVE_SYNC_STATES = {"scheduled", "syncing", "rescheduled"}

REQUIRED_POOL    = ALL_ECOMMERCE_FAMILIES
RECOMMENDED_POOL: set = set()

# Only min_required_count differs across skills; everything else is identical.
SKILL_MIN_REQUIRED: Dict[str, int] = {
    "store-performance": 1,
}

# ---------------------------------------------------------------------------
# Environment config
# ---------------------------------------------------------------------------
API_BASE     = os.environ.get("FIVETRAN_API_BASE_URL", "https://api.fivetran.com").rstrip("/")
API_KEY      = os.environ.get("FIVETRAN_API_KEY", "")
API_SECRET   = os.environ.get("FIVETRAN_API_SECRET", "")
MOCK_FETCHER = os.environ.get("ASA_FIVETRAN_FETCHER", "")


def _config_dir() -> str:
    if os.environ.get("STORE_PERFORMANCE_CONFIG_DIR"):
        return os.environ["STORE_PERFORMANCE_CONFIG_DIR"]
    local = "./.fivetran/store-performance"
    if os.path.isdir(local):
        return local
    return os.path.join(os.path.expanduser("~"), ".fivetran", "skills", "store-performance")


def _profile_path() -> str:
    if os.environ.get("STORE_PERFORMANCE_PROFILE_PATH"):
        return os.environ["STORE_PERFORMANCE_PROFILE_PATH"]
    return os.path.join(_config_dir(), "profile.json")


def _creds_path() -> str:
    return os.path.join(_config_dir(), "credentials.json")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_header() -> str:
    creds = _get_credentials()
    key, secret = creds if creds else ("", "")
    return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()


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
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if 500 <= exc.code < 600 and attempt == 1:
                last_exc = exc
                time.sleep(0.75)
                continue
            raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
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


def _write_credentials(api_key: str, api_secret: str) -> None:
    config_dir = _config_dir()
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    path = _creds_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"api_key": api_key, "api_secret": api_secret}, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _get_credentials() -> Optional[Tuple[str, str]]:
    """Return (api_key, api_secret) from env vars, module globals, or credentials file."""
    key = os.environ.get("FIVETRAN_API_KEY", "") or API_KEY
    secret = os.environ.get("FIVETRAN_API_SECRET", "") or API_SECRET
    if key and secret:
        return key, secret
    creds = _read_credentials()
    if creds:
        k = creds.get("api_key", "")
        s = creds.get("api_secret", "")
        if k and s:
            return k, s
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
# QDM schema probe — corrected algorithm (plan §QDM schema probe)
# ---------------------------------------------------------------------------

def _probe_schema(
    dest_type: str,
    database: str,
    location: str,
    model_names: List[str],
    last_ended_at: Optional[str],
) -> Optional[str]:
    """Find which schema contains all output_model_names. Returns schema name or None."""
    if MOCK_FETCHER or not model_names or not database:
        return None
    try:
        if dest_type == "bigquery":
            return _probe_schema_bq(database, location, model_names, last_ended_at)
        if dest_type == "snowflake":
            return _probe_schema_snowflake(database, model_names, last_ended_at)
        if dest_type == "databricks":
            return _probe_schema_databricks(database, model_names, last_ended_at)
    except Exception as exc:
        print(f"[asa] warn: schema probe failed: {exc}", file=sys.stderr)
    return None


def _tiebreak(
    candidates: List[str],
    last_ended_at: str,
    get_last_mod_fn,  # callable(schema) -> Optional[str] ISO timestamp
) -> Optional[str]:
    try:
        ended = _parse_iso8601(last_ended_at).timestamp()
    except Exception:
        return None
    best_schema, best_delta = None, float("inf")
    for schema in candidates:
        last_mod_str = get_last_mod_fn(schema)
        if not last_mod_str:
            continue
        try:
            delta = abs(_parse_iso8601(last_mod_str).timestamp() - ended)
            if delta < best_delta:
                best_delta, best_schema = delta, schema
        except Exception:
            continue
    return best_schema


def _probe_schema_bq(
    project: str, location: str, model_names: List[str], last_ended_at: Optional[str]
) -> Optional[str]:
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
        return None
    schemas = [r.get("table_schema") for r in rows if r.get("table_schema")]
    if not schemas:
        return None
    if len(schemas) == 1 or not last_ended_at:
        return schemas[0]

    def get_last_mod(schema: str) -> Optional[str]:
        r2 = _bq_query(
            f"SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', "
            f"TIMESTAMP_MICROS(MAX(last_modified_time))) AS last_mod "
            f"FROM `{project}.{schema}.__TABLES__`",
            timeout=20,
        )
        return (r2[0].get("last_mod") if r2 else None)

    return _tiebreak(schemas, last_ended_at, get_last_mod) or schemas[0]


def _probe_schema_snowflake(
    database: str, model_names: List[str], last_ended_at: Optional[str]
) -> Optional[str]:
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
        return None

    def get_schema(row):
        if isinstance(row, dict):
            return row.get("TABLE_SCHEMA") or row.get("table_schema")
        if isinstance(row, list) and row:
            return str(row[0])
        return None

    schemas = [s for s in (get_schema(r) for r in rows) if s]
    if not schemas:
        return None
    if len(schemas) == 1 or not last_ended_at:
        return schemas[0]

    def get_last_mod(schema: str) -> Optional[str]:
        r2 = _snow_query(
            f"SELECT TO_CHAR(MAX(LAST_ALTERED), 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS last_mod "
            f"FROM {database}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = UPPER('{schema}')",
            timeout=20,
        )
        if not r2:
            return None
        row2 = r2[0]
        if isinstance(row2, dict):
            return row2.get("LAST_MOD") or row2.get("last_mod")
        if isinstance(row2, list) and row2:
            return str(row2[0])
        return None

    return _tiebreak(schemas, last_ended_at, get_last_mod) or schemas[0]


def _probe_schema_databricks(
    catalog: str, model_names: List[str], last_ended_at: Optional[str]
) -> Optional[str]:
    names_sql = ", ".join(f"'{n}'" for n in model_names)
    sql = (
        f"SELECT table_schema, COUNT(*) AS matched "
        f"FROM system.information_schema.tables "
        f"WHERE table_catalog = '{catalog}' AND table_name IN ({names_sql}) "
        f"GROUP BY table_schema HAVING matched = {len(model_names)}"
    )
    rows = _databricks_query(sql)
    if not rows:
        return None
    if isinstance(rows[0], list):
        schemas = [str(r[0]) for r in rows if isinstance(r, list) and r]
    else:
        schemas = [str(rows[0])]
    return schemas[0] if schemas else None


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
    family = PACKAGE_TO_FAMILY.get(pkg) or (pkg if pkg in ALL_ECOMMERCE_FAMILIES else None)
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

    creds = _get_credentials()
    if not creds:
        if sys.stdin.isatty():
            key = getpass.getpass("Fivetran API key: ")
            secret = getpass.getpass("Fivetran API secret: ")
            if not key or not secret:
                print("[asa] credentials cannot be empty", file=sys.stderr)
                return EXIT_CREDS_MISSING
            global API_KEY, API_SECRET
            API_KEY, API_SECRET = key, secret
        else:
            print(
                "[asa] Fivetran credentials not found.\n"
                "Run setup in your own terminal (credentials will be prompted securely):\n"
                "  bash .marketplace/store-performance/scripts/asa.sh setup --skill <skill-id>",
                file=sys.stderr,
            )
            return EXIT_CREDS_MISSING

    # Merge CLI skip flags with persisted state.
    # clear_skip → wipe; explicit skip_families → replace; neither → preserve.
    existing_profile = _read_profile() or {}
    existing_skip = set(existing_profile.get("skipped_families") or [])
    if clear_skip:
        final_skip: set = set()
    elif skip_families:
        final_skip = set(skip_families)
    else:
        final_skip = existing_skip

    min_required = SKILL_MIN_REQUIRED.get(skill_id, 1)

    # Fetch destinations + groups in parallel
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            dest_fut   = pool.submit(fetch_paginated, "/v1/destinations", limit=1000)
            groups_fut = pool.submit(fetch_paginated, "/v1/groups",       limit=1000)
            raw_destinations = dest_fut.result()
            raw_groups       = groups_fut.result()
    except RuntimeError as exc:
        if "HTTP 401" in str(exc):
            print(
                "[asa] Invalid API key or secret — check your credentials at "
                "https://fivetran.com/account/settings/api-config and run this script again.",
                file=sys.stderr,
            )
            return EXIT_CREDS_MISSING
        raise

    # Credentials verified — persist to file now (not before, so bad creds aren't stored)
    resolved_creds = _get_credentials()
    if resolved_creds:
        _write_credentials(*resolved_creds)

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
        if not isinstance(c, dict) or c.get("service") not in ALL_ECOMMERCE_FAMILIES:
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

    # Fetch transformation details for active QUICKSTART transforms
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
    qdm_schemas: Dict[str, Optional[str]] = {}
    needed_list = list(needed_qdm_types)
    if needed_list:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(needed_list))) as pool:
            futures = {
                pool.submit(
                    _probe_schema,
                    dest_type, database, location,
                    qdm_registry[qt]["output_model_names"],
                    qdm_registry[qt]["last_ended_at"],
                ): qt
                for qt in needed_list
            }
            for fut in concurrent.futures.as_completed(futures):
                qt = futures[fut]
                try:
                    qdm_schemas[qt] = fut.result()
                except Exception:
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

    if refresh_on_miss and _get_credentials():
        dest_id  = raw.get("destination", {}).get("destination_id")
        skill_id = raw.get("skill", {}).get("id", "store-performance")
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

# Tables whose date column is `date_month` instead of `date_day`.
_MONTHLY_TABLE_MARKER = "monthly_"  # signals date_month instead of date_day


def _readiness_query_bq(project: str, schema: str, table: str, timeout: int = 30) -> Optional[List[dict]]:
    date_col = "date_month" if _MONTHLY_TABLE_MARKER in table else "date_day"
    sql = (
        f"SELECT platform, MAX({date_col}) AS latest_date, COUNT(*) AS row_count "
        f"FROM `{project}.{schema}.{table}` GROUP BY platform"
    )
    return _bq_query(sql, timeout=timeout)


def _readiness_query_snow(database: str, schema: str, table: str, timeout: int = 30) -> Optional[List]:
    date_col = "date_month" if _MONTHLY_TABLE_MARKER in table else "date_day"
    sql = (
        f"SELECT platform, MAX({date_col}) AS latest_date, COUNT(*) AS rows "
        f"FROM {database}.{schema}.{table} GROUP BY platform"
    )
    return _snow_query(sql, timeout=timeout)


def _readiness_query_databricks(catalog: str, schema: str, table: str, timeout: int = 30) -> Optional[List]:
    date_col = "date_month" if _MONTHLY_TABLE_MARKER in table else "date_day"
    sql = (
        f"SELECT platform, MAX({date_col}) AS latest_date, COUNT(*) AS rows "
        f"FROM {catalog}.{schema}.{table} GROUP BY platform"
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
                    "platform":    str(r.get("platform") or ""),
                    "latest_date": str(r.get("latest_date") or ""),
                    "rows":        int(r.get("row_count") or r.get("rows") or 0),
                })
            elif isinstance(r, list) and len(r) >= 3:
                rows.append({"platform": str(r[0]), "latest_date": str(r[1]), "rows": int(r[2] or 0)})
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

    freshness_rows.sort(key=lambda r: (r["table"], r["platform"]))

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
        skip_families: set = set()
        clear_skip = False
        skill_id = "store-performance"
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
            elif arg == "--skip-family" and i + 1 < len(rest):
                skip_families.add(rest[i + 1].strip()); i += 2
            elif arg == "--no-skip":
                clear_skip = True; i += 1
            elif arg == "--skill" and i + 1 < len(rest):
                skill_id = rest[i + 1]; i += 2
            elif arg == "--refresh":
                refresh = True; i += 1
            else:
                print(f"[asa] unknown argument: {arg!r}", file=sys.stderr)
                return 1
        return cmd_setup(destination_id, connection_overrides, skill_id, refresh, skip_families, clear_skip)

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
