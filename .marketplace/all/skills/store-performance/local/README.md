# local/ — drop zone for a hand-crafted demo profile

This directory lets you run the store-performance skill against a fixed warehouse without going through Fivetran setup. Useful for demos and internal testing against the bundled sample data.

## Usage

1. Copy the template to the standard profile path:
   ```sh
   cp .marketplace/fivetran-skills/skills/store-performance/local/profile.example.json \
      ~/.fivetran/skills/store-performance/profile.json
   ```
   (Or set `STORE_PERFORMANCE_PROFILE_PATH=/some/other/path.json` and copy there.)

2. Edit the copy:
   - Replace `<YOUR_DATABASE>` with your demo warehouse (e.g. `ECOMMERCE_ANALYZER` for the bundled Snowflake demo, or `<your-gcp-project>` for BigQuery).
   - Replace `<YOUR_SCHEMA>` with the schema containing Shopify-shaped tables (e.g. `SHOPIFY` for the bundled demo).
   - If you're using BigQuery, change `destination_type` to `bigquery` and `warehouse_tool` to `bq`. For Databricks, use `databricks` and `databricks_cli`.

3. Invoke the skill normally — `validate` passes, `resolve`/`readiness` operate against your warehouse, the verified queries run as-is.

To return to a normal Fivetran-driven setup: `rm ~/.fivetran/skills/store-performance/profile.json` and re-invoke the skill.

## Why this directory exists

`profile.json` (the populated copy) is gitignored because it contains warehouse identifiers we don't want in a public repo. Only `profile.example.json`, this README, and the `.gitignore` are tracked.

See the "Demo / preconfigured profile" section in `skills/store-performance/SKILL.md` for more.
