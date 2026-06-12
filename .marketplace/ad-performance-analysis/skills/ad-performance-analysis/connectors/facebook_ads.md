# Facebook Ads — raw connector drill-down reference

Use this when a Facebook question needs a dimension the unified `ad_reporting__*`
layer doesn't carry: **leads/conversions by action type, creatives, UTM detail,
placement/device, demographics, reach/frequency**. For cross-channel spend/clicks/
impressions/conversions, stay in the unified layer.

**Dataset:** the Facebook raw schema from `resolve facebook_ads` — `database` →
`{PROJECT_ID}`, `raw_schema` → `{RAW_DATASET}`. All tables below are
`{PROJECT_ID}.{RAW_DATASET}.<table>`. Do not hardcode a dataset name.

## Confirm what's enabled first (tables are report-dependent)

```sql
SELECT table_name FROM `{PROJECT_ID}.{RAW_DATASET}.INFORMATION_SCHEMA.TABLES` ORDER BY table_name;
-- columns for a specific table:
SELECT column_name, data_type FROM `{PROJECT_ID}.{RAW_DATASET}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name = '<table>';
```

## Gotchas (read before writing queries)

1. **Conversions/actions are split by `action_type`.** `basic_ad_actions`,
   `basic_ad_set_actions`, `basic_campaign_actions` have one row per
   (entity, date, action_type); `value` is the count for that action type. To get
   a metric you SUM(value) filtered to the right action_type(s).

2. **Lead triple-count trap.** `lead`, `onsite_web_lead`, and
   `offsite_conversion.fb_pixel_lead` are typically the **same events tracked three
   ways** — summing them triple-counts. Use **`action_type = 'lead'` only** unless
   you've verified the account tags leads differently. (Confirm with
   `SELECT action_type, SUM(value) ... GROUP BY action_type` — if the three are
   equal, they're duplicates.)

3. **STRING ↔ INT64 join cast.** `basic_*_actions.ad_id` / `campaign_id` are
   **STRING**; `ad_history.id` / `campaign_history.id` are **INT64**. Always
   `CAST(history.id AS STRING)` when joining, or the query errors with a type
   mismatch.

4. **History tables are SCD2 — dedup.** Each entity has multiple history rows.
   Facebook history tables do **not** carry a `_fivetran_active` flag (unlike
   Google Ads), so take the current row with
   `ROW_NUMBER() OVER (PARTITION BY id ORDER BY _fivetran_synced DESC) = 1`.

5. **Conversions ≠ engagement.** The dominant true conversion here is
   `offsite_conversion.fb_pixel_custom`; `page_engagement`, `post_engagement`,
   `video_view`, `link_click` are engagement, **not** conversions. Pick the
   conversion action_types deliberately.

6. **UTMs live in creative JSON, not any insights table.**
   `creative_history.asset_feed_spec_link_urls` is a JSON array of objects, each
   with a `website_url` carrying the UTM query string. The unified `url_report`
   has FB UTMs but is stale and sparse. (There is also a normalized
   `ad_asset_feed_spec_link_url` table, but its `url_tags` is usually empty and it
   covers few rows — prefer the creative JSON.)

7. **Reach / frequency are DEDUPLICATED — never SUM them across campaigns or days.**
   Summing campaign-level reach double-counts users who saw multiple campaigns and
   overstates true reach. Read reach at the grain you need (`reach_frequency`); do
   not roll it up like spend/clicks/impressions (which *are* additive).

8. **Cannot break out per creative-element performance.** Facebook's API returns
   ad/ad-set/campaign-level metrics only — not headline-vs-body-vs-image splits.

## Key tables

- **Performance, action-split:** `basic_ad_actions`, `basic_ad_set_actions`,
  `basic_campaign_actions`, `basic_all_levels_actions` (+ `*_cost_per_action_type`).
- **Performance, aggregate (no action split):** `basic_ad`, `basic_ad_set`,
  `basic_campaign`, `basic_all_levels` — daily clicks/impressions/spend per grain.
- **Entity / SCD2 history:** `ad_history` (`id`, `campaign_id`, `ad_set_id`,
  `creative_id`, `name`), `creative_history` (`id`, `name`, `title`, `body`,
  `image_url`, `asset_feed_spec_link_urls` JSON), `campaign_history`,
  `ad_set_history`.
- **Breakouts:** `demographics_age`/`_gender`/`_age_and_gender` (+ `_actions`),
  `delivery_device`/`delivery_platform` (+ `_actions`), `reach_frequency`.

## Verified query patterns (substitute {PROJECT_ID}/{RAW_DATASET})

### Leads by campaign (use `lead` only — avoids the triple-count)
```sql
WITH cn AS (
  SELECT id, name, ROW_NUMBER() OVER (PARTITION BY id ORDER BY _fivetran_synced DESC) AS rn
  FROM `{PROJECT_ID}.{RAW_DATASET}.campaign_history`
)
SELECT cn.name AS campaign_name, SUM(bca.value) AS leads
FROM `{PROJECT_ID}.{RAW_DATASET}.basic_campaign_actions` bca
JOIN cn ON bca.campaign_id = CAST(cn.id AS STRING) AND cn.rn = 1
WHERE bca.action_type = 'lead'
  AND bca.date BETWEEN '2025-07-01' AND '2025-09-30'
GROUP BY campaign_name ORDER BY leads DESC
```

### Top creatives by conversions (multi-hop + CAST + SCD dedup)
```sql
WITH la AS (
  SELECT id, creative_id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY _fivetran_synced DESC) AS rn
  FROM `{PROJECT_ID}.{RAW_DATASET}.ad_history`
),
lc AS (
  SELECT id, name, ROW_NUMBER() OVER (PARTITION BY id ORDER BY _fivetran_synced DESC) AS rn
  FROM `{PROJECT_ID}.{RAW_DATASET}.creative_history`
)
SELECT c.name AS creative_name, ROUND(SUM(baa.value), 0) AS conversions
FROM `{PROJECT_ID}.{RAW_DATASET}.basic_ad_actions` baa
JOIN la ON baa.ad_id = CAST(la.id AS STRING) AND la.rn = 1
JOIN lc c ON la.creative_id = c.id AND c.rn = 1
WHERE baa.action_type IN ('offsite_conversion.fb_pixel_custom','offsite_conversion.fb_pixel_lead',
                          'onsite_web_lead','lead','complete_registration','subscribe','submit_application')
  AND baa.date BETWEEN '2025-07-01' AND '2025-09-30'
GROUP BY creative_name ORDER BY conversions DESC LIMIT 10
```

### UTM source/medium breakdown (parse the creative JSON)
```sql
WITH urls AS (
  SELECT REGEXP_EXTRACT(JSON_VALUE(u, '$.website_url'), r'[?&]utm_source=([^&]+)') AS utm_source,
         REGEXP_EXTRACT(JSON_VALUE(u, '$.website_url'), r'[?&]utm_medium=([^&]+)') AS utm_medium
  FROM `{PROJECT_ID}.{RAW_DATASET}.creative_history`,
       UNNEST(JSON_QUERY_ARRAY(asset_feed_spec_link_urls)) u
)
SELECT utm_source, utm_medium, COUNT(*) AS link_entries
FROM urls WHERE utm_source IS NOT NULL
GROUP BY utm_source, utm_medium ORDER BY link_entries DESC
```

### Freshness
```sql
SELECT MAX(date) AS last_date FROM `{PROJECT_ID}.{RAW_DATASET}.basic_ad`;
```
If months stale, say so — do not present stale data as current.
