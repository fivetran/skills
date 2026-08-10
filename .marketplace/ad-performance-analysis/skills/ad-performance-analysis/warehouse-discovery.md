# Warehouse-only setup (`discover`)

Full walkthrough for Step 2a in `SKILL.md`. Read this on demand — only after the user has said they know their warehouse.

No secret is involved here — `bq`/`snow`/`databricks` are already in this skill's allowed tools — so run this **in this chat session**, not a separate terminal.

1. Ask for the warehouse type (BigQuery / Snowflake / Databricks) and the database/project/catalog name. Do **not** ask the user to recall schema names: only the warehouse and database are required, and schema names are discoverable (step 3 below lists them). Discovery does **not** match on schema name anyway, because Fivetran schema names are arbitrary (a Facebook Ads connector may land in a schema called `ads_9108233`). Instead, discovery inspects the *table names inside* each schema and fingerprints them against known connector/QDM table sets.
2. Run `check-cli` for the chosen warehouse tool first (same as the Prerequisites step in `SKILL.md`) to confirm the CLI is installed and authenticated.
3. List the schemas and let the user pick from what is shown, rather than asking them to name one from memory:
   ```bash
   bash ${CLAUDE_PLUGIN_ROOT}/skills/ad-performance-analysis/asa.sh list-schemas \
     --warehouse <bq|snowflake_cli|databricks_cli> --database <name> 2>&1; echo "EXIT:$?"
   ```
   This is a metadata-only lookup (`bq ls`, `SHOW SCHEMAS`), so it is cheap on all three warehouses and reads no table inventory. Exit `0` prints `schemas[]`; exit `53` means the database has no schemas, so re-check the database name. Show the names, ask which look relevant, and pass those as `--schema` in the next step. If the user cannot tell, skip `--schema` and let fingerprinting decide. On BigQuery this step also avoids the `--location` requirement, since `bq ls` is not a regional `INFORMATION_SCHEMA` view.
4. Run discovery:
   ```bash
   bash ${CLAUDE_PLUGIN_ROOT}/skills/ad-performance-analysis/asa.sh discover \
     --warehouse <bq|snowflake_cli|databricks_cli> \
     --database <project|database|catalog> \
     --schema <schema_name> [--schema <schema_name> ...] 2>&1; echo "EXIT:$?"
   ```
   Omit `--schema` entirely if the user gave none — discovery scans the whole database. For BigQuery specifically, prefer asking for at least one schema hint when possible: an unscoped scan queries `INFORMATION_SCHEMA.TABLES` across every dataset in the project's region, which is slow and can be costly on projects with many unrelated datasets. On BigQuery, `--location` is required unless a `--schema` hint is given for discovery to read the region from; discovery will not assume `US`, because a scan against the wrong region returns zero rows that look identical to an empty project.
5. **Discovery exit codes:**
   - `0` — profile written. Parse the printed JSON and present the **same Setup Summary** as `SKILL.md`'s "Setup Summary" section (the shape is identical: `connections[]`, `single_source_qdms[]`, `multi_source_qdms[]`). Then continue to the Freshness Check in `SKILL.md`. Three optional keys need handling when present:
     - `raw_schema_placeholders` — these families were found in the unified layer but no raw connector schema was fingerprinted for them, so raw-table drill-down is unavailable. Ask if the user can name the missing raw schema(s) and re-run with `--schema-override <family>=<schema_name>`. The named schema must be one discovery actually scanned, so add it via `--schema` in the same re-run if it was outside the original scope.
     - `linkage_warning` — the connector-to-QDM linkage check did not succeed, so every connector is reported as `raw` tier and `unified_schema` is null. That looks the same as a warehouse with no QDM layer, so do **not** present the tiers as confirmed. Relay the warning and offer to re-run with `--schema-override unified=<name>`.
     - `broad_scan_warning` — the scan covered more schemas than expected with no `--schema` hints given, so the fingerprint match may have picked up a schema belonging to an unrelated team/connector on a shared project. Relay the warning and offer to re-run with `--schema <name>` to narrow the scan.
     - `ignored_schema_override_keys` — one or more `--schema-override` keys matched nothing and were skipped, usually a misspelled family name. Tell the user which keys were ignored and re-run with the corrected spelling.
   - `70` (CLI missing) or `71` (CLI unauthenticated) — same handling as `SKILL.md`'s setup exit codes: surface the printed recipe verbatim and STOP.
   - `54` (schema disambiguate) — multiple schemas matched the same fingerprint. Parse the JSON; `"schemas"` maps a key (either `"multisource_ad_reporting"` for the unified layer, or a connector family like `google_ads` for a raw schema) to a list of candidate schema names, and `"hint"` carries a ready-made re-run flag string. Show the candidate names to the user and ask which to use for each key, then re-run with `--schema-override KEY=<chosen_schema>` (repeatable):
     ```bash
     bash ${CLAUDE_PLUGIN_ROOT}/skills/ad-performance-analysis/asa.sh discover \
       --warehouse <tool> --database <db> --schema <hint> \
       --schema-override google_ads=<chosen_schema> 2>&1; echo "EXIT:$?"
     ```
   - `53` (insufficient connectors / nothing found) — parse the JSON (`required_pool`, `found`, `min_required_count`, or a `"not_found"` message). Tell the user nothing matched, ask them to double-check the database/schema names, or offer to fall back to the Fivetran API key setup.
   - any other non-zero — relay the stderr message; offer to fall back to the Fivetran API key setup.

6. **If you learn more after `discover` has already run, re-run it.** Discovery's fingerprinting is heuristic — it can under-detect. If you later confirm that a schema belongs to a connector beyond what the last run found, write that back by re-running `discover`; do not leave the finding in chat. `profile.json` is what `resolve` and every future session read, so anything not in it is lost at the end of this session.

   **Re-run with the complete schema list, not just the newly found one.** `discover` overwrites the profile wholesale and does not merge with what is already there, so a re-run naming only the new schema silently drops every connector the previous run found. The same applies to `--schema-override`: overrides are not persisted, so any that were needed before must be repeated in the re-run.

   No flag is needed — unlike `setup`, `discover` has no "already configured" guard and will simply rewrite the profile.

   Do **not** hand-edit `profile.json`. A re-run is already safe and produces a profile that passes validation; a hand-edit can silently produce one that does not.

**Acceptable degradations** of a warehouse-discovered profile vs. the API-key path: it cannot know which QDM models are *excluded* from refresh, or when the transformation last ran (`excluded_models` is always empty, `last_ended_at` is always null) — that metadata only the Fivetran API has. This is fine because the Freshness Check (`readiness`) independently probes actual data recency per table at query time, so staleness is still caught.
