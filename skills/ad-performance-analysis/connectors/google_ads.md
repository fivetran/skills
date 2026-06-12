# Google Ads — raw connector drill-down reference

Google's unified `ad_reporting__*` coverage is rich (campaign, ad group, ad,
keyword, search, URL), so **stay in the unified layer for most questions.** Drop to
the raw connector only for dimensions the unified layer doesn't carry:
**audience segments, conversion-action lineage/definitions, asset-level creative,
bid strategy/criteria, account-structure history**.

**Dataset:** the Google raw schema from `resolve google_ads` — `database` →
`{PROJECT_ID}`, `raw_schema` → `{RAW_DATASET}`. All tables below are
`{PROJECT_ID}.{RAW_DATASET}.<table>`. Do not hardcode a dataset name.

## Confirm what's enabled first (tables are report-dependent)

```sql
SELECT table_name FROM `{PROJECT_ID}.{RAW_DATASET}.INFORMATION_SCHEMA.TABLES` ORDER BY table_name;
SELECT column_name, data_type FROM `{PROJECT_ID}.{RAW_DATASET}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name = '<table>';
```

## Gotchas (read before writing queries)

1. **Cost is in micros.** Raw `*_stats` tables store cost as `cost_micros` —
   divide by 1e6 for spend (`SUM(cost_micros)/1e6`). The unified layer already does
   this; the raw layer does not.

2. **`*_stats` = performance, `*_history` = SCD2 entities.** Metrics
   (clicks, impressions, cost_micros, conversions, conversions_value,
   view_through_conversions) live in `*_stats` tables keyed by `date`. Names,
   statuses, and structure live in `*_history` tables.

3. **History tables are SCD2 — filter to the current row** with
   `WHERE _fivetran_active` (Google history tables carry `_fivetran_active`,
   `_fivetran_start`, `_fivetran_end`). Use that instead of a ROW_NUMBER dedup.

4. **Audiences are a multi-hop join and NOT in the unified layer.** Performance is
   in `audience_stats` (keyed by `ad_group_criterion_criterion_id` + `ad_group_id`,
   cost in `cost_micros`). Identity comes from `ad_group_criterion_history`: join
   `audience_stats.ad_group_criterion_criterion_id = ad_group_criterion_history.id`
   AND `ad_group_id`, filtered `WHERE _fivetran_active`. That table carries `type`
   (USER_LIST / USER_INTEREST / CUSTOM_AUDIENCE / COMBINED_AUDIENCE / …),
   `display_name`, and `user_list_id` / `user_interest_id` / `custom_audience_id` /
   `combined_audience_id` / `audience_id`. Resolve readable names: `user_list_id` →
   `user_list.name`. **USER_INTEREST criteria resolve only to Google taxonomy IDs**
   (e.g. `uservertical::91500`) — there are no human names for those; say so rather
   than inventing labels.

5. **Conversion lineage.** The `conversions` / `conversions_value` metrics in the
   `*_stats` tables (and the unified model) are **Google-defined conversion
   actions** the advertiser configured — not a Fivetran computation. The conversion
   goals/actions themselves are defined in `campaign_conversion_goal_history`. When
   asked "where do conversions come from / how are they defined," point to the
   `*_stats` tables for the counts and `campaign_conversion_goal_history` for the
   goal definitions; don't fabricate a definition.

6. **Search terms vs keywords.** `search_term_stats` has the actual user queries
   (`search_term`, `search_term_match_type`); `search_keyword_stats` / `keyword_stats`
   have keyword-level metrics. Keyword *text* lives in the keyword history/criterion.

## Key tables

- **Performance (`*_stats`, keyed by `date`, cost in micros):** `campaign_stats`,
  `ad_group_stats`, `ad_stats`, `keyword_stats`, `search_keyword_stats`,
  `search_term_stats`, `audience_stats`, `click_stats`, `landing_page_stats`,
  `account_stats`.
- **Audiences:** `audience_stats` (perf), `ad_group_criterion_history` (criterion →
  audience identity), `user_list`, `user_interest`, `custom_audience`,
  `combined_audience`, `audience`.
- **Conversions:** counts in the `*_stats` tables; goal definitions in
  `campaign_conversion_goal_history` (+ `campaign_optimization_goal_setting_history`).
- **Entity history (SCD2, `_fivetran_active`):** `campaign_history`,
  `ad_group_history`, `ad_history`, `ad_group_criterion_history`,
  `campaign_criterion_history`, `responsive_search_ad_history`, etc.

## Verified query patterns (substitute {PROJECT_ID}/{RAW_DATASET})

### Top audience segments by conversions (multi-hop + micros + name resolution)
```sql
WITH crit AS (
  SELECT id, ad_group_id, type, display_name, user_list_id
  FROM `{PROJECT_ID}.{RAW_DATASET}.ad_group_criterion_history`
  WHERE _fivetran_active
    AND type IN ('USER_LIST','USER_INTEREST','CUSTOM_AUDIENCE','AUDIENCE','COMBINED_AUDIENCE')
),
ul AS (SELECT id, name FROM `{PROJECT_ID}.{RAW_DATASET}.user_list`)
SELECT c.type,
       COALESCE(c.display_name, ul.name,
                CONCAT(c.type, ':', CAST(a.ad_group_criterion_criterion_id AS STRING))) AS audience,
       ROUND(SUM(a.cost_micros) / 1e6, 2) AS spend,
       ROUND(SUM(a.conversions), 1) AS conversions
FROM `{PROJECT_ID}.{RAW_DATASET}.audience_stats` a
JOIN crit c ON a.ad_group_criterion_criterion_id = c.id AND a.ad_group_id = c.ad_group_id
LEFT JOIN ul ON c.user_list_id = ul.id
WHERE a.date BETWEEN '2025-12-01' AND '2026-05-31'
GROUP BY c.type, audience ORDER BY conversions DESC LIMIT 10
```

### Conversion goals defined on the account (lineage)
```sql
SELECT * FROM `{PROJECT_ID}.{RAW_DATASET}.campaign_conversion_goal_history`
WHERE _fivetran_active LIMIT 50;
-- conversion COUNTS come from the *_stats tables, e.g.:
-- SELECT SUM(conversions) FROM `{PROJECT_ID}.{RAW_DATASET}.campaign_stats` WHERE date BETWEEN ...
```

### Freshness
```sql
SELECT MAX(date) AS last_date FROM `{PROJECT_ID}.{RAW_DATASET}.campaign_stats`;
```

## Reminder: additive vs deduplicated metrics
Clicks, impressions, cost, conversions are additive — safe to SUM across grains.
Unique/reach-style metrics are not. Compute CTR as `SUM(clicks)/SUM(impressions)`,
never as an average of per-row CTRs.
