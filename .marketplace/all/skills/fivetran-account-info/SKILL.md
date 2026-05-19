---
name: fivetran-account-info
description: Get a quick overview of the connected Fivetran account.
compatibility:
  tools:
    - fivetran:get_account_info
metadata:
  short-description: Get a quick overview of the connected Fivetran account
---

# Fivetran Account Info

Use the `get_account_info` tool from the `fivetran` MCP server to summarize the
connected Fivetran account.

Include:
- The account name and identifier
- Any account status details returned by the API
- A short note confirming the MCP connection is working
