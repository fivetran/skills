# Microsoft Ads — raw connector drill-down reference

Use this when a Microsoft Ads question needs a dimension the unified
`ad_reporting__*` layer doesn't carry: **per-goal conversions, share of voice,
city/region geo, search queries, audience/demographics, ad extensions**. For
cross-channel spend/clicks/impressions/conversions, stay in the unified layer.

**Dataset:** the Microsoft raw schema from `resolve bingads` — `database` →
`{PROJECT_ID}`, `raw_schema` → `{RAW_DATASET}`. All tables below are
`{PROJECT_ID}.{RAW_DATASET}.<table>`. Do not hardcode a dataset name.

## Confirm what's enabled first (tables are report-dependent)

```sql
SELECT table_name FROM `{PROJECT_ID}.{RAW_DATASET}.INFORMATION_SCHEMA.TABLES` ORDER BY table_name;
SELECT column_name, data_type FROM `{PROJECT_ID}.{RAW_DATASET}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name = '<table>';
```

## Gotchas (read before writing queries)

1. **Reports are pre-aggregated daily — query them directly.** Unlike Facebook,
   Microsoft's `*_performance_daily_report` tables are already rolled up; no
   action-type fan-out, no multi-hop joins. The dimension you want is usually a
   built-in column.

2. **The date column is `date`** (not `date_day`). Anchor relative windows on
   `MAX(date)` of the table, never `CURRENT_DATE()` — this connector may be stale.

3. **Per-goal conversions: break down by `goal`, not `goal_type`.**
   `conversion_performance_daily_report` has `goal`, `goal_type`, `goal_id`. In
   practice `goal_type` is often uniformly `Custom`, so the meaningful split is the
   `goal` name (e.g. "Trial Sign Up", "Demo Submission"). If a goal the user asks
   about (e.g. "Purchases") has no rows, report **0 / none** honestly — don't
   invent it.

4. **Geo has built-in `city` — no join.** `geographic_performance_daily_report`
   carries `city`, `region`/`state`, `country`. **Exclude blank-city rows**
   (`city IS NOT NULL AND city != ''`) — a blank-city bucket exists and will
   otherwise top the list.

5. **Share of voice is a dedicated table.** `share_of_voice_daily_report` carries
   `impression_share_percent`, `impression_lost_to_rank_agg_percent`,
   `impression_lost_to_budget_percent`, `click_share_percent` per campaign/keyword.
   Use the reported columns — do not compute a homemade impression share.

6. **`_daily` vs `_hourly`.** Most report families have both; use `_daily` unless
   the question is explicitly intraday.

## Key tables

- **Core performance:** `account_performance_daily_report`,
  `campaign_performance_daily_report`, `ad_group_performance_daily_report`,
  `ad_performance_daily_report`, `keyword_performance_daily_report`.
- **Drill-downs:** `conversion_performance_daily_report` (goal/goal_type),
  `geographic_performance_daily_report` (city/region/country),
  `share_of_voice_daily_report` (impression/click share),
  `search_query_performance_daily_report` (search terms),
  `age_gender_audience_daily_report`, `professional_demographics_audience_daily_report`,
  `audience_performance_daily_report`, `ad_extension_detail_daily_report`,
  `user_location_performance_daily_report`, `destination_url_performance_daily_report`.
- **Entity history:** `campaign_history`, `ad_group_history`, `ad_history`,
  `keyword_history`, `conversion_goal_history`.

## Verified query patterns (substitute {PROJECT_ID}/{RAW_DATASET})

### Conversions by goal (Form Fills/Sign-Ups vs Purchases)
```sql
SELECT goal, goal_type, ROUND(SUM(conversions), 0) AS conversions
FROM `{PROJECT_ID}.{RAW_DATASET}.conversion_performance_daily_report`
WHERE date BETWEEN '2025-01-01' AND '2025-12-31'
GROUP BY goal, goal_type ORDER BY conversions DESC
```

### Top cities by spend (built-in city, exclude blanks, anchor on MAX(date))
```sql
SELECT city, ROUND(SUM(spend), 2) AS spend
FROM `{PROJECT_ID}.{RAW_DATASET}.geographic_performance_daily_report`
WHERE date >= DATE_SUB(
  (SELECT MAX(date) FROM `{PROJECT_ID}.{RAW_DATASET}.geographic_performance_daily_report`),
  INTERVAL 6 MONTH)
  AND city IS NOT NULL AND city != ''
GROUP BY city ORDER BY spend DESC LIMIT 10
```

### Share of voice for top campaigns
```sql
SELECT campaign_name,
       ROUND(AVG(impression_share_percent), 1) AS avg_impression_share,
       ROUND(AVG(impression_lost_to_rank_agg_percent), 1) AS lost_to_rank,
       ROUND(AVG(impression_lost_to_budget_percent), 1) AS lost_to_budget,
       SUM(impressions) AS impressions
FROM `{PROJECT_ID}.{RAW_DATASET}.share_of_voice_daily_report`
WHERE date BETWEEN '2025-01-01' AND '2025-12-31'
GROUP BY campaign_name ORDER BY impressions DESC LIMIT 10
```

### Freshness
```sql
SELECT MAX(date) AS last_date FROM `{PROJECT_ID}.{RAW_DATASET}.campaign_performance_daily_report`;
```
If months stale, say so explicitly.
