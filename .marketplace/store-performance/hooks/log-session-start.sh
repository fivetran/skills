#!/bin/bash
# SessionStart hook: report that the plugin loaded, and its version.

WEBHOOK_URL="https://www.postb.in/1776621867867-1216837756801"
CONNECT_TIMEOUT_SECONDS="${CONNECT_TIMEOUT_SECONDS:-2}"
REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS:-3}"

PLUGIN_JSON="${CLAUDE_PLUGIN_ROOT:-}/.claude-plugin/plugin.json"

body=$(PLUGIN_JSON="$PLUGIN_JSON" python3 -c "
import datetime
import json
import os
import sys

payload = sys.stdin.buffer.read()
try:
    event = json.loads(payload.decode('utf-8')) if payload else {}
except (UnicodeDecodeError, json.JSONDecodeError):
    event = {}

plugin_name = 'unknown'
plugin_version = 'unknown'
try:
    with open(os.environ['PLUGIN_JSON'], 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    plugin_name = manifest.get('name') or plugin_name
    plugin_version = manifest.get('version') or plugin_version
except (OSError, json.JSONDecodeError):
    pass

print(json.dumps({
    'event': 'Plugin Session Start',
    'plugin': plugin_name,
    'version': plugin_version,
    'source': event.get('source'),
    'model': event.get('model'),
    'session_id': event.get('session_id'),
    'timestamp': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
}))
")

if [[ -z "$body" ]]; then
  exit 0
fi

curl -s -o /dev/null -X POST "$WEBHOOK_URL" \
  --connect-timeout "$CONNECT_TIMEOUT_SECONDS" \
  --max-time "$REQUEST_TIMEOUT_SECONDS" \
  -H "Content-Type: application/json" \
  -d "$body" >/dev/null 2>&1 &

exit 0
