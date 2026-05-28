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

| Plugin | Description |
|--------|-------------|
| [base](.marketplace/base) | Fivetran MCP and general skills |
| [ad-performance](.marketplace/ad-performance) | Cross-channel ad performance analysis via BigQuery, Snowflake, or Databricks |
| [store-performance](.marketplace/store-performance) | E-commerce store performance analysis from raw Shopify connector data |

### `base` skills

| Skill | Description |
|-------|-------------|
| [fivetran-account-info](.marketplace/base/skills/fivetran-account-info) | Get a quick overview of the connected Fivetran account |
| [lakehouse-explorer](.marketplace/base/skills/lakehouse-explorer) | Query Fivetran-managed Iceberg tables via DuckDB and Polaris, cost-efficiently |

## MCP

The bundled Fivetran MCP server uses the published `uvx` launcher from
[fivetran/fivetran-mcp](https://github.com/fivetran/fivetran-mcp):

```bash
uvx --from git+https://github.com/fivetran/fivetran-mcp fivetran-mcp
```

When the `base` plugin is enabled, Claude Code prompts for the Fivetran
API key and API secret via `userConfig` and injects them into the MCP server
configuration automatically.
