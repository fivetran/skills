# Fivetran Skills

> **Read-only mirror.** This repo is automatically published from an internal repository. Do not open pull requests here — all development and review happens internally.

The official repository of Fivetran skills and plugins for AI Agents, as a [Claude Plugin Marketplace](https://code.claude.com/docs/en/discover-plugins).

## Installation

### Claude Code CLI
Register the marketplace:

```
/plugin marketplace add fivetran/skills
```
Install a specific plugin from the marketplace (see [Plugins](#plugins) below):
```
/plugin install <plugin>@fivetran-skills   # replace <plugin> with a named from the list plugin 
```
Reload your plugins to load the installed skill in your current session:
```
/reload-plugins
```

#### Recommended: Enable Auto-Update

Use the `/plugin` interactive menu, navigate to **Marketplaces**, and enable auto-update for the "fivetran" marketplace.

```
❯ /plugin
────────────────────────────────────────────────────────────────────
 Plugins  Discover   Installed   Marketplaces  (←/→ or tab to cycle)

 fivetran-skills
 fivetran/skills

6 available plugins

 Installed plugins (1):
  ● ad-performance-analysis

   Browse plugins (6)
   Update marketplace
 ❯ Enable auto-update
   Remove marketplace
```

### Claude Desktop App

1. Click **Customize** in the left nav and click the **+** next to Personal Plugins
2. Click **+ Create Plugin** → **Add Marketplace**
3. Enter `fivetran/skills`
4. Navigate to **+ Browser Plugins** → **Personal** → **skills**
5. Add the specific skill you need by clicking the **+** next to the skill

### Vercel Skills CLI

Skills can also be installed individually by referencing them in the `skills/` subdirectory.

```
npx skills add fivetran/skills
```

See [Vercel's Skills docs](https://github.com/vercel-labs/skills) for flags like `--global`, `--skill`, `--agent`, and `--list`.

## Plugins

<!-- PLUGINS-TABLE-START -->
| Plugin | Description |
|--------|-------------|
| [base](.marketplace/base) | Fivetran MCP and general skills |
| [ad-performance-analysis](.marketplace/ad-performance-analysis) | Cross-channel ad performance analysis via BigQuery, Snowflake, or Databricks |
| [customer-support-analysis](.marketplace/customer-support-analysis) | First reply, resolution time, backlog, SLA, and CSAT analysis for Zendesk |
| [marketing-automation-analysis](.marketplace/marketing-automation-analysis) | Funnel velocity, nurture, and email engagement analysis for Marketo (and other marketing automation tools) |
| [sales-pipeline-analysis](.marketplace/sales-pipeline-analysis) | HubSpot sales pipeline funnel and rep performance analysis |
| [store-performance-analysis](.marketplace/store-performance-analysis) | E-commerce store performance analysis from raw Shopify connector data |
<!-- PLUGINS-TABLE-END -->

<!-- SKILLS-BY-PLUGIN-START -->
### `base` skills

| Skill | Description |
|-------|-------------|
| [fivetran-account-info](.marketplace/base/skills/fivetran-account-info) | Get a quick overview of the connected Fivetran account |
| [lakehouse-explorer](.marketplace/base/skills/lakehouse-explorer) | Query Fivetran-managed Iceberg tables via DuckDB and Polaris, cost-efficiently |
<!-- SKILLS-BY-PLUGIN-END -->

## MCP

The bundled Fivetran MCP server uses the published `uvx` launcher from
[fivetran/fivetran-mcp](https://github.com/fivetran/fivetran-mcp):

```bash
uvx --from git+https://github.com/fivetran/fivetran-mcp fivetran-mcp
```

When the `base` plugin is enabled, Claude Code prompts for the Fivetran
API key and API secret via `userConfig` and injects them into the MCP server
configuration automatically.

## Disclaimer

These skills are provided as-is. Fivetran makes no guarantees about their fitness
for any particular purpose and accepts no liability for issues arising from their use.

## Privacy & Data Collection

When a skill is invoked (whether it succeeds or fails), the plugin sends a usage
event to Fivetran containing: an anonymous per-machine identifier, your Fivetran
account and user ID (once authenticated), the skill name, invocation status
(success or failure), the model in use, and a session identifier.
The receiving server also records standard request metadata, including your IP
address, as part of normal HTTP logging.

Users can learn more about these data practices in
[Fivetran's Privacy Policy](https://www.fivetran.com/legal/privacy-policy).

### Opting out

Set `FIVETRAN_TELEMETRY_DISABLED=1` in the environment your agent runs in.
Values of `0`, `false`, `FALSE`, `no`, or `NO` keep telemetry on; any other value disables it.
