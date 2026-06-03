# Marketo raw connector schema

Reference for the raw Marketo connector tables that Fivetran syncs (the `raw_schema` source for the `fivetran/marketo` dbt package). Use this when:

- `model_tier == 'raw'` — the dbt package isn't deployed and queries must go directly against connector tables.
- A question requires event-level detail the QDM rolls up (e.g. "the exact timestamp lead X opened email Y", or "what device was used to open this send"). The dbt models aggregate; raw tables preserve every event.

Source of truth: [`fivetran/dbt_marketo_source/models/src_marketo.yml`](https://github.com/fivetran/dbt_marketo_source/blob/main/models/src_marketo.yml). Authored by Fivetran. The dbt package consumes these tables; the connector syncs them.

> **Coverage caveat:** The 14 tables below are what the dbt package documents and consumes. The Marketo connector may sync additional tables that the dbt package doesn't use (e.g. `activity_visit_webpage`, `activity_fill_out_form`, `form`, `list`, `smart_list`, `segmentation`, `program_membership_history`). Their schemas aren't enumerated here — use Discovery Mode (`bq ls`, `bq show --schema`) on the destination to inspect them when needed.

## `lead` — one row per Marketo lead

| Column | Notes |
|---|---|
| `id` | INTEGER. Primary key (joins to `activity_*.lead_id`). |
| `created_at` | TIMESTAMP. When the lead was created. |
| `updated_at` | TIMESTAMP. When the lead was last updated. |
| `email` | STRING. Email address. |
| `first_name`, `last_name`, `phone`, `main_phone`, `mobile_phone` | STRING. Lead identity. |
| `company`, `inferred_company` | STRING. Declared vs reverse-IP inferred company. |
| `address`, `address_lead`, `city`, `state`, `state_code`, `country`, `country_code`, `postal_code` | STRING. Lead's stated geo. |
| `billing_street`, `billing_city`, `billing_state`, `billing_state_code`, `billing_country`, `billing_country_code`, `billing_postal_code` | STRING. Billing geo. |
| `inferred_city`, `inferred_state_region`, `inferred_country`, `inferred_postal_code`, `inferred_phone_area_code` | STRING. Reverse-IP inferred geo. |
| `anonymous_ip` | STRING. IP from first recorded web visit. |
| `unsubscribed` | BOOLEAN. Email unsubscribe status. |
| `email_invalid` | BOOLEAN. Hard-bounce / invalid email flag. |
| `do_not_call` | BOOLEAN. DNC preference. |

## `lead_describe` — metadata about lead-object columns

| Column | Notes |
|---|---|
| `id` | INTEGER. Field id. |
| `display_name` | STRING. UI label. |
| `data_type` | STRING. Field's datatype. |
| `length` | INTEGER. Max length for text fields. |
| `restname`, `soapname` | STRING. REST and SOAP API field names. |
| `restread_only`, `soapread_only` | BOOLEAN. Whether the field is read-only via that API. |

## `campaign` — one row per Marketo campaign

| Column | Notes |
|---|---|
| `id` | INTEGER. Primary key. |
| `name` | STRING. Display name. |
| `type` | STRING. `batch` or `trigger`. |
| `status` | STRING. Campaign status. |
| `active` | BOOLEAN. Whether trigger campaign is currently active. |
| `description` | STRING. |
| `program_id` | INTEGER. Joins to `program.id`. |
| `workspace_name` | STRING. Marketo workspace. |
| `created_at`, `updated_at` | TIMESTAMP. |
| `computed_url`, `flow_id` | STRING. UI url and flow reference. |
| `folder_id`, `folder_type` | Folder grouping. |
| `is_communication_limit_enabled`, `is_requestable`, `is_system` | BOOLEAN. Campaign flags. |
| `max_members` | INTEGER. Member cap. |
| `qualification_rule_type`, `qualification_rule_interval`, `qualification_rule_unit` | Qualification frequency rules. |
| `recurrence_start_at`, `recurrence_end_at`, `recurrence_interval_type`, `recurrence_interval`, `recurrence_weekday_only`, `recurrence_day_of_month`, `recurrence_day_of_week`, `recurrence_week_of_month` | Recurrence schedule. |
| `smart_list_id` | INTEGER. Associated smart list. |
| `_fivetran_deleted` | BOOLEAN. Filter `= false` to exclude soft-deleted rows. |

## `program` — one row per Marketo program

| Column | Notes |
|---|---|
| `id` | INTEGER. Primary key. |
| `name` | STRING. Display name. |
| `type` | STRING. dbt-documented allowed values: `program`, `event`, `webinar`, `nurture`. Customer-configured — actual values vary (`Email`, `Engagement`, `Default`, `EventWithWebinar` are common in real instances). |
| `status` | STRING. `locked`, `unlocked`, `on`, `off` (email and engagement programs only). |
| `channel` | STRING. Marketo channel. |
| `description` | STRING. |
| `workspace` | STRING. Marketo workspace. |
| `start_date`, `end_date` | TIMESTAMP. For event/webinar/email programs. |
| `sfdc_id`, `sfdc_name` | STRING. Linked Salesforce campaign, if any. |
| `created_at`, `updated_at` | TIMESTAMP. |
| `url` | STRING. UI url. |
| `_fivetran_deleted` | BOOLEAN. Filter `= false` to exclude soft-deleted rows. |

## `email_template_history` — one row per email template version

| Column | Notes |
|---|---|
| `id` | INTEGER. Template id (not unique per row — see `version`). |
| `version` | STRING. Template version (`1` or `2`). Use the most recent per `id`. |
| `name` | STRING. Display name. |
| `subject` | STRING. Subject line. |
| `from_email`, `from_name` | STRING. Sender identity. |
| `reply_email` | STRING. Reply-to. |
| `program_id` | INTEGER. Joins to `program.id`. |
| `description` | STRING. |
| `template` | INTEGER. Parent template id. |
| `operational` | BOOLEAN. Operational emails bypass unsubscribe status. |
| `publish_to_msi` | BOOLEAN. Published to Marketo Sales Insight. |
| `text_only` | BOOLEAN. Include text-only version. |
| `web_view` | BOOLEAN. "View as Webpage" enabled. |
| `status` | STRING. Draft or approved. |
| `url` | STRING. UI url. |
| `workspace` | STRING. Marketo workspace. |
| `folder_id`, `folder_type`, `folder_value`, `folder_folder_name` | Folder grouping. |
| `created_at`, `updated_at` | TIMESTAMP. |

## Activity tables — one row per event of that type

These eleven `activity_*` tables share a core column set (`id`, `activity_date`, `activity_type_id`, `lead_id`, `primary_attribute_value`, `primary_attribute_value_id`). Email-related activities also share campaign / template / flow attribution columns. The shared columns are documented once below; type-specific columns are listed per table.

**Shared columns across all `activity_*` tables:**

| Column | Notes |
|---|---|
| `id` | STRING / INTEGER. Activity id. |
| `lead_id` | INTEGER. Joins to `lead.id`. |
| `activity_date` | TIMESTAMP. When the event happened. |
| `activity_type_id` | INTEGER. Marketo activity-type id. |
| `primary_attribute_value` | STRING. Activity's primary attribute (varies by type). |
| `primary_attribute_value_id` | INTEGER. Id of the primary attribute. |

**Shared columns across email `activity_*` tables** (`send_email`, `email_delivered`, `email_bounced`, `open_email`, `click_email`, `unsubscribe_email`):

| Column | Notes |
|---|---|
| `campaign_id` | INTEGER. Joins to `campaign.id`. |
| `campaign_run_id` | INTEGER. Specific campaign execution. |
| `email_template_id` | INTEGER. Joins to `email_template_history.id`. |
| `step_id` | INTEGER. Flow step that triggered the activity. |
| `choice_number` | INTEGER. Branch choice within the flow step. |

### `activity_send_email` — email send events

Additional columns:

| Column | Notes |
|---|---|
| `action_result` | STRING. Outcome of the action within Marketo. |

### `activity_email_delivered` — successful delivery events

No type-specific columns beyond the shared set.

### `activity_email_bounced` — bounce events

Additional columns:

| Column | Notes |
|---|---|
| `email` | STRING. The address that bounced. |
| `category` | STRING. Bounce category (e.g. `hard`, `soft`). |
| `subcategory` | STRING. Sub-category detail. |
| `details` | STRING. Why the email bounced. |

### `activity_open_email` — open events

Additional columns:

| Column | Notes |
|---|---|
| `device` | STRING. Device type opened on. |
| `is_mobile_device` | BOOLEAN. |
| `platform` | STRING. Platform (e.g. iOS, Android, Outlook). |
| `user_agent` | STRING. Browser user agent. |

### `activity_click_email` — click events

Additional columns:

| Column | Notes |
|---|---|
| `link` | STRING. URL clicked. |
| `device` | STRING. Device type. |
| `is_mobile_device` | BOOLEAN. |
| `user_agent` | STRING. Browser user agent. |

### `activity_unsubscribe_email` — unsubscribe events

Additional columns:

| Column | Notes |
|---|---|
| `client_ip_address` | STRING. IP of the client. |
| `form_fields`, `query_parameters` | STRING. Form / URL parameters. |
| `referrer_url` | STRING. Page that referred. |
| `user_agent` | STRING. Browser user agent. |
| `webform_id`, `webpage_id` | INTEGER. Unsubscribe form / page ids. |

### `activity_change_data_value` — lead-attribute change events

Additional columns:

| Column | Notes |
|---|---|
| `api_method_name` | STRING. API method that made the change. |
| `modifying_user` | STRING. User who made the change. |
| `new_value`, `old_value` | STRING. Values before and after. |
| `reason` | STRING. Reason for the change. |
| `request_id` | STRING. API request id. |

### `activity_delete_lead` — lead deletion events

Additional columns:

| Column | Notes |
|---|---|
| `campaign` | STRING. Campaign that triggered the deletion, if any. |

### `activity_merge_leads` — lead merge events

Additional columns:

| Column | Notes |
|---|---|
| `merge_ids` | STRING. Lead id this record was merged into. |

## Raw-tier query notes

When `model_tier == 'raw'`, lifetime engagement counts on `marketo__leads` are not available. Compute them yourself:

- **Sends per lead:** `SELECT lead_id, COUNT(*) FROM activity_send_email GROUP BY 1`
- **Opens per lead:** `SELECT lead_id, COUNT(DISTINCT id) FROM activity_open_email GROUP BY 1` (use `COUNT(DISTINCT id)` for unique opens or `COUNT(*)` for total opens)
- **Engagement rates per program:** join `activity_*` to `email_template_history.program_id` (or `campaign.program_id`) via `email_template_id` (or `campaign_id`)
- **Funnel velocity:** not directly available in raw mode — `marketo__lead_history` snapshots are produced by the dbt package. The closest raw signal is `activity_change_data_value` filtered to changes on the lead-status field (look up the field id in `lead_describe`).
