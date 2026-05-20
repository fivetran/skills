---
name: lakehouse-explorer
description: >
  Explore and query your Fivetran Managed Data Lake Service (MDLS). As part
  of MDLS, Fivetran manages a Polaris catalog and the underlying Apache
  Iceberg tables where your connector data lands — your fully managed
  lakehouse. This skill gives you an agentic interface to explore that data
  (read-only in the current MDLS release). Use for ANY question about your
  MDLS tables: schemas, namespaces, row counts, sync status, data freshness,
  partitioning, snapshots, or running queries against your Iceberg data.
  Trigger on: "what tables do we have", "explore my lakehouse", "query the
  lakehouse", "check sync status", "how fresh is our data", "what's in MDLS",
  "describe this table", "how many rows", "show me our Iceberg tables".
metadata:
  short-description: "Query Fivetran-managed Iceberg tables via DuckDB and Polaris, cost-efficiently"
user-invocable: true
argument-hint: "<question about your MDLS tables, schemas, or data>"
allowed-tools: "bash(duckdb, curl, python3, pip)"
---

# Lakehouse Explorer Skill

## Setup

Copy `lakehouse_config.json.example` to `.lakehouse_config.json` in this skill's directory and fill in your credentials. All fields must be non-empty strings.

Your MDLS credentials can be found in the Fivetran UI under **Destinations → your MDLS destination → Catalog Integration**.

## Prerequisites

### DuckDB CLI (required)

Requires the DuckDB CLI binary (>=1.2.0). Test with `duckdb --version`. If missing, install from https://duckdb.org/docs/installation/.

> DuckDB 1.2 is the minimum because GCS (GCP-backed MDLS destinations) support was added in that release. AWS/S3-backed destinations work on 1.1.x, but 1.2+ is required for GCP and is the safe floor across all MDLS destinations.

### pyiceberg (optional — Column Bounds Inspection only)

Required only for the [Column Bounds Inspection](#column-bounds-inspection) section. Install with:

```bash
pip install pyiceberg
```

---

## Start of Every Session

**Step 1 — Verify DuckDB.**

```bash
duckdb --version
```

If the command fails, inform the user DuckDB is not installed and link to https://duckdb.org/docs/installation/.

**Step 2 — Read `.lakehouse_config.json` from the skill's base directory and validate that all fields are populated.**

The skill base directory is injected at load time (shown at the top of this prompt). Read the config from `<skill_base_dir>/.lakehouse_config.json`. Every field must have a non-empty string value before this skill can proceed.

If any field is an empty string `""`, stop immediately and ask the user to populate it:

> "Your `.lakehouse_config.json` is missing values for: `<list of blank fields>`. Please fill them in and let me know when it's ready."

Do not proceed until all fields are confirmed populated. Once validated, extract and use these values throughout the session:

| Config key | Used for |
|---|---|
| `polaris.polaris_client_id` | Polaris OAuth client ID |
| `polaris.polaris_client_secret` | Polaris OAuth client secret |
| `polaris.polaris_endpoint` | Polaris REST catalog base URL (e.g. `https://pack-dictate.us-west-2.aws.polaris.fivetran.com/api/catalog`) |
| `polaris.polaris_warehouse` | Warehouse name used in Polaris REST URL paths and DuckDB ATTACH |

Three values are derived automatically — do not ask the user for them:

- **`polaris_oauth_uri`** = `polaris_endpoint` + `/v1/oauth/tokens`
  - e.g. `https://pack-dictate.us-west-2.aws.polaris.fivetran.com/api/catalog/v1/oauth/tokens`
- **`cloud_region`** = second label of the `polaris_endpoint` hostname
  - e.g. `us-west-2` from `pack-dictate.us-west-2.aws.polaris.fivetran.com`
  - e.g. `us-east4` from `nibble-assessing.us-east4.gcp.polaris.fivetran.com`
- **`cloud_provider`** = third label of the `polaris_endpoint` hostname
  - e.g. `aws` from `pack-dictate.us-west-2.aws.polaris.fivetran.com`
  - e.g. `gcp` from `nibble-assessing.us-east4.gcp.polaris.fivetran.com`
  - e.g. `azure` from `something.eastus.azure.polaris.fivetran.com`

Never hardcode these values. Always source them from `.lakehouse_config.json`.

**Step 2b — Validate cloud provider support.**

After deriving `cloud_provider`, check it against the list of backends supported by DuckDB's Iceberg extension before proceeding:

| Provider | Supported? |
|---|---|
| `aws` | Yes — S3 and S3 Tables |
| `gcp` | Yes — GCS (added in DuckDB v1.2+) |
| `azure` | Unknown — check DuckDB docs |

If `cloud_provider` is `azure`, fetch the current DuckDB Iceberg REST catalog docs to check whether ADLS support has been added:

```
https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_rest_catalogs
```

- If the docs confirm Azure/ADLS is **not yet supported**, inform the user and do not attempt any DuckDB queries:
  > "This lakehouse is backed by Azure Data Lake Storage (ADLS), which is not yet supported by DuckDB's Iceberg extension. DuckDB currently supports S3 (AWS) and GCS (GCP) only. See: https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_rest_catalogs"

- If the docs confirm Azure/ADLS **is now supported**, proceed with the session and note to the user that Azure support has been added since this skill was last updated.

**Step 3 — Verify Polaris REST connectivity.**

Using the [Standard Call Pattern](#polaris-rest-api--standard-call-pattern) below, acquire a token and list namespaces:

```
GET {polaris_endpoint}/v1/{polaris_warehouse}/namespaces
```

If this fails, check `polaris_endpoint`, `polaris_client_id`, and `polaris_client_secret` in `.lakehouse_config.json`.

> **Token lifetime**: Polaris tokens expire (typically 1 hour). If a REST call returns `401 Unauthorized`, re-acquire using the same pattern and retry.

---

## Polaris REST API — Standard Call Pattern

Polaris tokens are valid for ~1 hour. Rather than re-acquiring on every call, cache to `/tmp` and reuse. The snippet below checks the cache age (Python `os.path.getmtime` is cross-platform) and only hits the token endpoint when the cached token is stale. Include this at the top of every bash script that makes REST calls, then use `$POLARIS_TOKEN` freely for all calls in that script.

```bash
POLARIS_TOKEN=$(python3 - << 'PYEOF'
import os, time, json, urllib.request, urllib.parse
cache = "/tmp/polaris_token_{polaris_warehouse}"
if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3000:
    print(open(cache).read().strip())
else:
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": "{polaris_client_id}",
        "client_secret": "{polaris_client_secret}",
        "scope": "PRINCIPAL_ROLE:ALL"
    }).encode()
    req = urllib.request.Request("{polaris_oauth_uri}", data=data)
    token = json.loads(urllib.request.urlopen(req).read())["access_token"]
    open(cache, "w").write(token)
    print(token)
PYEOF
)

curl -s -H "Authorization: Bearer $POLARIS_TOKEN" \
  "<endpoint URL>" | python3 -m json.tool
```

> **Token cache file** is named `/tmp/polaris_token_{polaris_warehouse}` so multiple warehouses on the same machine don't collide. TTL is 3000 seconds (50 min) — conservative against the 1-hour Polaris expiry.

---

## DuckDB Session Setup

Every DuckDB invocation is a fresh in-memory database. The `CREATE SECRET` step triggers an OAuth token acquisition on first catalog access. To avoid repeating that cost across many queries, use a **persistent secret** (stored to `~/.duckdb/stored_secrets/`): DuckDB auto-loads it in every subsequent session so you can drop `CREATE SECRET` from routine query scripts.

### First-time setup — create the persistent secret once

Run this once per machine (or whenever credentials rotate). It stores the OAuth2 config to disk so all future sessions auto-load it.

```bash
cat > /tmp/lakehouse_setup.sql << 'TEMPLATE'
INSTALL httpfs FROM core; LOAD httpfs;
INSTALL iceberg FROM core; LOAD iceberg;
CREATE OR REPLACE PERSISTENT SECRET polaris_secret (
    TYPE iceberg,
    CLIENT_ID '<polaris_client_id>',
    CLIENT_SECRET '<polaris_client_secret>',
    OAUTH2_SCOPE 'PRINCIPAL_ROLE:ALL',
    OAUTH2_SERVER_URI '<polaris_oauth_uri>'
);
TEMPLATE
duckdb < /tmp/lakehouse_setup.sql
```

> The persistent secret stores the OAuth2 configuration, not the bearer token itself. DuckDB still acquires a fresh bearer token per session automatically — you just don't need to re-declare the secret in every script.

### Bash invocation pattern (after persistent secret is set up)

```bash
cat > /tmp/lakehouse_query.sql << 'TEMPLATE'
INSTALL httpfs FROM core; LOAD httpfs;
INSTALL iceberg FROM core; LOAD iceberg;
ATTACH '<polaris_warehouse>' AS fivetran_lakehouse (
    TYPE ICEBERG,
    ENDPOINT '<polaris_endpoint>',
    SECRET polaris_secret,
    DEFAULT_REGION '<cloud_region>'
);

<your SQL here>
TEMPLATE

duckdb < /tmp/lakehouse_query.sql
```

**If the persistent secret hasn't been set up yet**, add the `CREATE OR REPLACE PERSISTENT SECRET` block before the `ATTACH` — it will create it and persist it for all future sessions in the same invocation.

### DuckDB catalog name

The catalog is always attached as **`fivetran_lakehouse`** (the `AS fivetran_lakehouse` alias in the ATTACH statement). Use this as the catalog prefix in all SQL queries:

```sql
DESCRIBE fivetran_lakehouse.<namespace>.<table>;
SELECT COUNT(*) FROM fivetran_lakehouse.<namespace>.<table>;
```

Do not use `{polaris_warehouse}` as the catalog prefix in SQL — that value is only used in the ATTACH call and in Polaris REST URL paths.

---

## Core Principle

**Polaris REST catalog first. Always. No exceptions.**

Every DuckDB query reads Parquet files from cloud storage via vended credentials — that costs money and can crash the machine if the dataset is large. The Polaris REST API returns catalog metadata with zero data egress. Most questions can be answered entirely from the catalog. Only fall through to DuckDB when you genuinely need row-level data that the catalog cannot provide.

---

## Catalog Name Reference — Critical

There are two different catalog identifiers in use. Using the wrong one causes 400/404 errors. Never confuse them.

| Context | Value | Source |
|---|---|---|
| Polaris REST API calls (URL path) | `{polaris_warehouse}` from config | `polaris_warehouse` in `.lakehouse_config.json` |
| DuckDB queries | `fivetran_lakehouse` | Hardcoded alias from the `ATTACH ... AS fivetran_lakehouse` statement — never changes |

---

## Catalog Discovery — Always Do This First

**Never assume the catalog structure.** Namespaces, tables, and schemas vary by environment and change over time. Always discover live state from Polaris before answering any question about tables or writing any query. Acquire a single token and make all required calls in one script:

```bash
# Acquire or reuse cached token (see Standard Call Pattern)
POLARIS_TOKEN=$(python3 - << 'PYEOF'
import os, time, json, urllib.request, urllib.parse
cache = "/tmp/polaris_token_{polaris_warehouse}"
if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3000:
    print(open(cache).read().strip())
else:
    data = urllib.parse.urlencode({"grant_type":"client_credentials","client_id":"{polaris_client_id}","client_secret":"{polaris_client_secret}","scope":"PRINCIPAL_ROLE:ALL"}).encode()
    req = urllib.request.Request("{polaris_oauth_uri}", data=data)
    token = json.loads(urllib.request.urlopen(req).read())["access_token"]
    open(cache,"w").write(token)
    print(token)
PYEOF
)

# 1. List namespaces
curl -s -H "Authorization: Bearer $POLARIS_TOKEN" \
  "{polaris_endpoint}/v1/{polaris_warehouse}/namespaces" | python3 -m json.tool

# 2. List tables in a namespace
curl -s -H "Authorization: Bearer $POLARIS_TOKEN" \
  "{polaris_endpoint}/v1/{polaris_warehouse}/namespaces/<namespace>/tables" | python3 -m json.tool

# 3. Get full table metadata (schema, partitions, snapshots) — do this before writing any query
curl -s -H "Authorization: Bearer $POLARIS_TOKEN" \
  "{polaris_endpoint}/v1/{polaris_warehouse}/namespaces/<namespace>/tables/<table>" | python3 -m json.tool
```

Never guess column names, partition columns, or row counts — fetch them first.

---

## Three-Tier Decision Process

Work through these tiers in order. Stop at the first tier that can answer.

---

### Tier 1 — Polaris REST API (zero cloud storage egress)

**Use for:** namespaces, table lists, schemas, column types, partition specs, sort orders, snapshot history, table properties, file counts, table-level statistics, catalog structure, roles, principals.

All calls follow the [standard call pattern](#polaris-rest-api--standard-call-pattern) above.

#### Endpoint reference

**List namespaces**
```
GET {polaris_endpoint}/v1/{polaris_warehouse}/namespaces
```

**Get namespace properties**
```
GET {polaris_endpoint}/v1/{polaris_warehouse}/namespaces/<namespace>
```

**List tables in a namespace**
```
GET {polaris_endpoint}/v1/{polaris_warehouse}/namespaces/<namespace>/tables
```

**Get full table metadata** (schema, partitions, snapshots, stats, storage location)
```
GET {polaris_endpoint}/v1/{polaris_warehouse}/namespaces/<namespace>/tables/<table>
```

**List catalog roles**
```
GET {polaris_endpoint}/management/v1/catalogs/{polaris_warehouse}/catalogRoles
```

**List principals**
```
GET {polaris_endpoint}/management/v1/principals
```

#### What table metadata contains (no cloud storage read required)

When you call `GET …/tables/<table>`, Polaris returns the full Iceberg table metadata JSON, which includes:

- **Schema**: every column name, type, nullability, field ID
- **Partition spec**: which columns partition the table and how
- **Sort order**: clustering keys if any
- **Snapshots**: full history of every commit — timestamp, operation (append/overwrite/delete), added/deleted file counts, added/deleted record counts, total records, total files, total size in bytes
- **Current snapshot summary**: `total-records`, `total-files-size`, `total-data-files`, `total-delete-files` — all available without touching cloud storage
- **Table properties**: custom key/value metadata
- **Storage location**: the base path of the table in cloud storage

**Row counts, file sizes, and record counts are available directly from the catalog — never run a `COUNT(*)` if the catalog already has it.**

#### Per-file column bounds (in manifest files, not the table metadata JSON)

Fivetran writes per-file `lower_bounds` and `upper_bounds` for every column into the Iceberg manifest Avro files at sync time. These are **not** in the table metadata JSON returned by the Polaris REST API — they live inside the manifest files referenced by the snapshot's `manifest-list`.

- For tables with ≤200 columns, Fivetran writes bounds for all columns
- For larger tables: `_fivetran_synced`, all primary key columns, and history mode columns (`_fivetran_active`, `_fivetran_start`, `_fivetran_end`)

**DuckDB reads these bounds automatically when planning a query** — it skips entire Parquet files whose column bounds fall outside your filter predicate. This is called *column bound pruning* and happens even on unpartitioned tables. You will see it in the EXPLAIN ANALYZE output as `optional: Dynamic Filter (<column>)` in the TABLE_SCAN node.

**Practical implication**: filtering on `_fivetran_synced`, primary key columns, or any column with a wide value spread can skip many files even without partitioning. Always filter on these columns when possible to reduce `#GET` count.

To inspect the actual bound values for a table, use the `pyiceberg` approach in the [Column Bounds Inspection](#column-bounds-inspection) section below.

---

### Tier 2 — DuckDB via Bash with EXPLAIN ANALYZE gate (cloud storage egress incurred)

Only reach for this when the question requires actual row data that the Polaris catalog cannot provide.

#### Mandatory three-step process — no exceptions

**Step 1: Run plain `EXPLAIN` first — free, zero egress.**

Before touching storage, run `EXPLAIN` (no ANALYZE) to inspect the logical query plan. This reads no Parquet data. Use it to check:
- Are your filters present in the TABLE_SCAN node? If not, DuckDB will full-scan — rewrite before going further.
- Does the plan structure look right (correct joins, aggregations, projections)?
- Are only the needed columns projected?

If the plan looks bad (missing filters, unnecessary columns, wrong structure), rewrite the query and re-run `EXPLAIN` until the plan is clean. Only proceed to Step 2 once the plan looks correct.

**Step 2: Run `EXPLAIN ANALYZE` to get real execution stats.**

Build the full session SQL script, prefix your query with `EXPLAIN ANALYZE`, write to a temp file, and run via the Bash tool.

> **`EXPLAIN ANALYZE` actually executes the query.** The stats below are real — not estimates. The storage cost has already been incurred. Running the real query afterward will cost the same again.

**Step 3: Present the plan report to the user and wait for explicit confirmation before running the real query.**

Extract and display these key metrics from the `analyzed_plan` output:

```
📊 Query Plan Report
─────────────────────────────────────
⚠️  Note: EXPLAIN ANALYZE executes the query — these are real numbers, not estimates.
    The storage cost below has already been incurred. Running the real query will cost the same again.

Query:           <the SQL you intend to run>

HTTPFS Stats:
  Data read:     <in: X KiB/MiB/GiB>
  HTTP GETs:     <#GET count>  ← number of cloud storage file reads

Execution:
  Total time:    <Xs>
  Files scanned: <Total Files Read: N from TABLE_SCAN node>
  Rows scanned:  <rows at TABLE_SCAN node>
  Rows returned: <rows at top node>

Plan quality:
  Filters pushed down:    <list filters shown in TABLE_SCAN node, or "none">
  Projections only:       <columns listed in TABLE_SCAN Projections>
  Partition pruning:      <yes/no — infer from files scanned vs total files>
  Column bound pruning:   <yes/no — look for "Dynamic Filter (<col>)" in TABLE_SCAN>
  Small files warning:    <flag if total-data-files / total-records ratio > 1 file per 100 rows>

Shall I run this query? Running it will incur the cost above a second time. (yes / no / suggest alternative)
```

Only execute the real query after explicit user confirmation ("yes", "run it", "go ahead", or equivalent). If the user says no or wants an alternative, revise the query, re-run EXPLAIN ANALYZE, and present a new report before asking again.

#### Reading the EXPLAIN ANALYZE output

The output is a box-drawing ASCII tree. Key things to extract:

- **HTTPFS HTTP Stats block** — `in: X bytes` is actual cloud storage data transferred. `#GET` is number of file reads. Zero bytes = no storage read (metadata only).
- **TABLE_SCAN node** — shows `Total Files Read: N`, applied `Filters:`, and `Projections:`. Filters here means pushdown worked. Missing filters = full scan.
- **`Dynamic Filter (<col>)` in TABLE_SCAN** — DuckDB is using per-file column bounds to skip files whose min/max range falls outside the filter. This is *column bound pruning* — distinct from partition pruning and effective even on unpartitioned tables. `optional:` prefix means DuckDB will apply it opportunistically.
- **Row counts at each node** — traces how many rows flow through each step.
- **Total Time** — wall clock including cloud storage latency.

#### What good vs bad plans look like

| Signal | Good | Bad |
|---|---|---|
| Files Read | Low (1-3) | High (>10) |
| Filters in TABLE_SCAN | Present | Absent (full scan) |
| Dynamic Filter in TABLE_SCAN | Present (column bound pruning active) | Absent (no file skipping) |
| Projections | Only needed columns | All columns (`SELECT *`) |
| Data in (HTTPFS) | KiB range | MiB/GiB range |
| #GET | 1-10 | >50 (even if data volume is small — indicates small files problem) |
| files / rows ratio | <1 file per 100 rows | >1 file per 100 rows (small files — high GET overhead per byte) |

**High `#GET` with small data volume** is a distinct problem from high data volume. It means the table has many small Parquet files (file fragmentation). Recommend table compaction/OPTIMIZE rather than rewriting the query.

If a plan looks bad, rewrite the query to add better filters or reduce projections, then EXPLAIN ANALYZE again before presenting to the user.

#### Mandatory query rules — apply before writing any query

1. **Always use fully qualified table names**: `fivetran_lakehouse.<namespace>.<table>`
2. **Always confirm schema from Tier 1 first** — use Polaris table metadata before writing any query so you know column names and partition columns
3. **Always include `LIMIT`** on any `SELECT` that returns rows — hard cap 100
4. **Prefer aggregations** (`COUNT`, `GROUP BY`, `MIN`, `MAX`, `AVG`) over fetching rows
5. **Always filter on partition or bound columns** — check the partition spec from Tier 1 first; if no partition exists, filter on `_fivetran_synced` or primary key columns to trigger column bound pruning (DuckDB skips files whose per-file min/max falls outside the predicate, even on unpartitioned tables)
6. **Filter soft-deleted rows** when a `_fivetran_deleted` column is present: `WHERE _fivetran_deleted IS NOT TRUE`
7. **Never scan large tables without a partition or time filter** — check row counts and partition specs from Tier 1 before querying
8. **Select only needed columns** — never `SELECT *` unless the user explicitly asks; always list columns explicitly

#### Safe query patterns

```sql
-- Aggregation (safe)
SELECT <group_col>, COUNT(*) as n
FROM fivetran_lakehouse.<namespace>.<table>
WHERE _fivetran_deleted IS NOT TRUE
GROUP BY <group_col> ORDER BY n DESC;

-- Filtered sample (safe — select only needed columns)
SELECT <col1>, <col2>, <col3>
FROM fivetran_lakehouse.<namespace>.<table>
WHERE <partition_col> = '<value>' AND _fivetran_deleted IS NOT TRUE
LIMIT 20;

-- Last sync check (safe)
SELECT MAX(_fivetran_synced) as last_sync
FROM fivetran_lakehouse.<namespace>.<table>;
```

#### DuckDB quirks for this catalog

- Use `DESCRIBE fivetran_lakehouse.<ns>.<table>` — not `information_schema.columns` (returns UNKNOWN types for Iceberg tables)
- Use `information_schema.tables` — not `SHOW TABLES` or `SHOW SCHEMAS`
- `duckdb_tables()` does not return `estimated_size` for Iceberg tables
- Always prefix `fivetran_lakehouse.` — unqualified names fail
- `EXPLAIN ANALYZE` prints the full ASCII box-drawing tree directly to stdout — parse it as plain text output from the Bash tool
- `iceberg_metadata(fivetran_lakehouse.<ns>.<table>)` works on attached catalog tables and returns per-file info (manifest paths, file paths, record counts). It does **not** expose per-column bounds — use the pyiceberg approach for that
- `parquet_metadata('<cloud://path>')` does **not** work for Iceberg files — it bypasses the Iceberg OAuth secret and hits cloud storage directly with no credentials. You will get a 403 regardless of cloud provider. Use `iceberg_metadata()` or pyiceberg instead

---

### Tier 3 — Rewrite or refuse (query is fundamentally unsafe)

If EXPLAIN ANALYZE reveals a plan that is unsafe even after rewriting — e.g. no filters available, table has millions of rows, data read would be GiB-range — do not present it for confirmation. Instead:

1. Explain why the query is unsafe (files scanned, data volume)
2. Offer a safe alternative (aggregation, filtered subset, catalog metadata)
3. Ask whether the user wants to proceed with the alternative

Examples of fundamentally unsafe patterns:
- Full scan of a large table with no partition filter
- Log tables with no time filter
- Multi-table joins with no selective filters on either side

---

## Decision Flowchart

```
Question received
       │
       ▼
Read .lakehouse_config.json → validate all fields populated → extract config values
       │
       ▼
Can Polaris REST catalog answer this?
(schema, columns, partitions, snapshots,
 row counts, file sizes, table properties)
       │
      YES ──► GET {polaris_endpoint}/v1/{polaris_warehouse}/... → Answer
       │      (zero cloud storage egress)
       NO
       │
       ▼
Write the best possible DuckDB query
(partition filters, column pruning, aggregations, LIMIT)
       │
       ▼
Run: EXPLAIN <query> via Bash  ← free, zero egress
       │
       ▼
Plan looks clean?
(filters in TABLE_SCAN, correct projections, right structure)
       │
      NO ──► Rewrite query → EXPLAIN again → Repeat
       │
      YES
       │
       ▼
Run: EXPLAIN ANALYZE <query> via Bash  ← executes query, real cost incurred
       │
       ▼
Present Query Plan Report to user
(data read, #GETs, files scanned, filters pushed down)
       │
       ▼
User confirms?
       │
      YES ──► Run the real query → Present results
       │
       NO ──► Revise query → EXPLAIN → EXPLAIN ANALYZE again → Repeat
       │
   Plan is fundamentally unsafe even after rewrite
       │
       └──► Explain why → Offer safe alternative → Ask to proceed
```

---

## Response Format

- **Lead with the answer**, not the tool call or SQL
- State which tier was used:
  - *"From Polaris catalog — no cloud storage read."*
  - *"Ran aggregation query on [table] ([N] rows scanned)."*
- Show SQL or curl commands in a code block after the answer, not before
- For tabular results, render as a markdown table (cap display at 20 rows)
- For snapshot stats, format bytes as human-readable (GB/MB)
- For schema results, format as a clean column/type table

---

## Column Bounds Inspection

> **Diagnostic tool only.** Use this to understand what filter values will enable file skipping, or to verify column bound pruning effectiveness for a given query. Do not run this routinely — it reads manifest Avro files from cloud storage and has real latency cost for large tables.

### Why this is non-trivial

The per-file column bounds are stored as binary-encoded bytes in Iceberg manifest Avro files. They are **not** in the Polaris REST API JSON response. Three approaches fail before one works:

1. **DuckDB `parquet_metadata('<cloud://path>')`** — bypasses the Iceberg OAuth secret, hits cloud storage directly with no credentials. Always 403 regardless of cloud provider.
2. **DuckDB `iceberg_metadata(fivetran_lakehouse.<ns>.<table>)`** — works on attached catalogs and returns file paths and record counts, but does not surface `lower_bounds`/`upper_bounds` from the manifest Avro.
3. **`pyiceberg` straight** — `table.scan().plan_files()` would give `DataFile.lower_bounds` / `.upper_bounds` directly, but Fivetran writes a custom puffin blob type (`fivetran-synced-distribution`) that pyiceberg 0.10.x's Pydantic V2 model rejects. The entire table load fails before you can read anything.

### Known caveats of the working approach

Before using this, understand the tradeoffs:

- **Strips Fivetran's custom statistics.** The `fivetran-synced-distribution` blob is discarded to work around validation. Its contents (likely sync-time value distributions) are not read. This is fine for column bound inspection but means you are not seeing everything Fivetran wrote.
- **`resp._content` is a private requests attribute.** The patch mutates an internal field. If `requests` changes its response model, the patch breaks silently — pyiceberg will either fail validation again or behave unexpectedly with no clear error.
- **Library-coupled.** If pyiceberg migrates from `requests` to `httpx` (a common direction), the patch does nothing. Check pyiceberg release notes when upgrading.
- **Not zero-egress.** `plan_files()` reads the manifest list and manifest Avro files from cloud storage. For tables with many snapshots and manifests this is non-trivial latency. Not as cheap as a Polaris REST call.
- **Output is per-file, not per-table.** For a table with N files, the raw output is N rows per column. For large tables this is unreadable and unhelpful. Use the aggregated pattern below instead.

### Working approach — pyiceberg with HTTP intercept

Shared decode helper and intercept setup used by both patterns below:

```python
import json, struct, requests
from datetime import datetime, timezone
from unittest.mock import patch

def _strip_unknown_blobs(resp):
    """Strip Fivetran's custom puffin blob type before pyiceberg validates it."""
    if "tables" in resp.url and resp.status_code == 200:
        try:
            data = resp.json()
            for s in data.get("metadata", {}).get("statistics", []):
                s["blob-metadata"] = [
                    b for b in s.get("blob-metadata", [])
                    if b.get("type") in ("apache-datasketches-theta-v1", "deletion-vector-v1")
                ]
            resp._content = json.dumps(data).encode()
        except Exception:
            pass
    return resp

original_get = requests.Session.get
def patched_get(self, url, **kwargs):
    return _strip_unknown_blobs(original_get(self, url, **kwargs))

def decode_bound(typ, b):
    if not b: return "(null)"
    if typ in ("timestamptz", "timestamp"):
        micros = struct.unpack_from("<q", b)[0]
        return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if typ == "boolean": return "true" if b == b'\x01' else "false"
    if typ in ("int",):  return str(struct.unpack_from("<i", b)[0])
    if typ == "long":    return str(struct.unpack_from("<q", b)[0])
    if typ == "double":  return str(struct.unpack_from("<d", b)[0])
    try: return b.decode("utf-8").rstrip("\x00")
    except: return repr(b)

def load_catalog_patched(polaris_endpoint, polaris_client_id, polaris_client_secret, polaris_warehouse):
    from pyiceberg.catalog import load_catalog
    return load_catalog("polaris", **{
        "type": "rest",
        "uri": polaris_endpoint,
        "credential": f"{polaris_client_id}:{polaris_client_secret}",
        "warehouse": polaris_warehouse,
        "scope": "PRINCIPAL_ROLE:ALL",
    })
```

### Pattern A — single-file tables (or when per-file detail is needed)

For tables with one Parquet file (e.g. small dimension tables freshly synced), raw per-file output is readable:

```python
with patch.object(requests.Session, "get", patched_get):
    catalog = load_catalog_patched("{polaris_endpoint}", "{polaris_client_id}",
                                   "{polaris_client_secret}", "{polaris_warehouse}")
    table = catalog.load_table("<namespace>.<table>")
    schema = table.schema()
    field_map = {f.field_id: f.name for f in schema.fields}
    type_map  = {f.field_id: str(f.field_type) for f in schema.fields}

    print(f"{'column':<25} {'type':<15} {'min':<40} {'max':<40}")
    print("-" * 120)
    for task in table.scan().plan_files():
        df = task.file
        lower, upper = df.lower_bounds or {}, df.upper_bounds or {}
        for fid in sorted(set(list(lower) + list(upper))):
            typ = type_map.get(fid, "?")
            print(f"{field_map.get(fid, fid):<25} {typ:<15} "
                  f"{decode_bound(typ, lower.get(fid, b'')):<40} "
                  f"{decode_bound(typ, upper.get(fid, b'')):<40}")
```

### Pattern B — multi-file tables (aggregated, skippability-focused)

For tables with many files (e.g. `log` with 410 files), aggregate across all files to show global min/max and — for a given filter value — how many files DuckDB can skip. This is the meaningful output for query planning.

```python
# Set this to the filter value you are planning to use
FILTER_COLUMN = "_fivetran_synced"
FILTER_VALUE_STR = "2026-05-12 00:00:00 UTC"   # must be a decoded string matching decode_bound output

with patch.object(requests.Session, "get", patched_get):
    catalog = load_catalog_patched("{polaris_endpoint}", "{polaris_client_id}",
                                   "{polaris_client_secret}", "{polaris_warehouse}")
    table = catalog.load_table("<namespace>.<table>")
    schema = table.schema()
    field_map = {f.field_id: f.name for f in schema.fields}
    type_map  = {f.field_id: str(f.field_type) for f in schema.fields}

    # Collect per-file bounds across all files
    from collections import defaultdict
    col_mins, col_maxs = defaultdict(list), defaultdict(list)
    total_files = 0

    for task in table.scan().plan_files():
        df = task.file
        total_files += 1
        lower, upper = df.lower_bounds or {}, df.upper_bounds or {}
        for fid in set(list(lower) + list(upper)):
            typ = type_map.get(fid, "?")
            col_mins[fid].append(decode_bound(typ, lower.get(fid, b"")))
            col_maxs[fid].append(decode_bound(typ, upper.get(fid, b"")))

    print(f"Total files: {total_files}\n")
    print(f"{'column':<25} {'type':<15} {'global_min':<35} {'global_max':<35} {'skippable_files':>16}")
    print("-" * 130)

    filter_fid = next((fid for fid, name in field_map.items() if name == FILTER_COLUMN), None)

    for fid in sorted(col_mins.keys()):
        name = field_map.get(fid, f"field_{fid}")
        typ  = type_map.get(fid, "?")
        mins = col_mins[fid]
        maxs = col_maxs[fid]
        g_min = min(mins)
        g_max = max(maxs)

        # Count files where filter_value > file_max (file can be skipped for >= filter)
        skippable = "-"
        if fid == filter_fid:
            skippable = str(sum(1 for mx in maxs if mx < FILTER_VALUE_STR))
            skippable = f"{skippable}/{total_files}"

        print(f"{name:<25} {typ:<15} {g_min:<35} {g_max:<35} {skippable:>16}")

    print(f"\nInterpretation: for WHERE {FILTER_COLUMN} >= '{FILTER_VALUE_STR}', "
          f"DuckDB can skip files where file_max < filter value.")
```

### What the output tells you

- **Wide spread between global min and max** — the column has good value distribution across files; filtering on it will skip many files
- **Narrow spread (min ≈ max)** — values are clustered; filtering won't skip much regardless of the predicate
- **`_fivetran_deleted` max = `false` everywhere** — DuckDB can skip any file for a `WHERE _fivetran_deleted = true` filter instantly (and vice versa)
- **Skippable files count** — the core metric. If a filter value can skip 350 of 410 files, the real query will only read 60 files instead of 410, dramatically reducing `#GET` and data read

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `duckdb: command not found` | Install from https://duckdb.org/docs/installation/ |
| `401 Unauthorized` on Polaris REST calls | Token expired — re-acquire using the OAuth token step |
| `400 Bad Request` on Polaris REST calls | Check that `polaris_warehouse` in `.lakehouse_config.json` matches the catalog name exactly |
| `404` on table metadata | Confirm namespace and table name via the list endpoints first — never guess names |
| DuckDB `iceberg` extension not found | Run `INSTALL iceberg FROM core;` manually in DuckDB to pre-install |
| DuckDB `Secret with name "polaris_secret" already exists` | Persistent secret already loaded from disk. Drop it with `DROP PERSISTENT SECRET polaris_secret` if credentials have changed, then re-run first-time setup. |
| Cloud storage credential or region errors | Confirm `cloud_region` is parsed correctly from the second hostname label of `polaris_endpoint` |
| Azure / ADLS errors or hangs | ADLS support is unconfirmed — check DuckDB docs per Step 2b before proceeding. |
| GCS errors despite correct credentials | Ensure DuckDB >= v1.2 is installed (`duckdb --version`). GCS support was added in v1.2 and is not available in LTS builds. |
| `parquet_metadata()` returns 403 | Expected — `parquet_metadata()` bypasses the Iceberg OAuth secret and hits cloud storage directly with no credentials. Use `iceberg_metadata()` or the pyiceberg approach instead. |
| pyiceberg `ValidationError` on `fivetran-synced-distribution` | Fivetran writes a custom puffin blob type that pyiceberg 0.10.x does not recognise. Use the HTTP intercept pattern in the Column Bounds Inspection section to strip the unknown type before validation. |
| High `#GET` count with small data volume | Small files problem — the table has many tiny Parquet files. This is a table maintenance issue, not a query issue. Recommend compaction. Queries will be slow regardless of filters. |
| REST token returns 401 mid-session | Cached token in `/tmp/polaris_token_{polaris_warehouse}` is stale or corrupt. Delete the file and retry — the Standard Call Pattern will re-acquire. |

## Reference Files

- `.lakehouse_config.json` — user-populated Polaris credentials. **Read from the skill base directory. Validate all fields are non-empty before proceeding. Never overwrite this file — it contains the user's credentials.**
