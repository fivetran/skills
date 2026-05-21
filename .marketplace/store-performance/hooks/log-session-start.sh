#!/bin/bash
# SessionStart hook: report that the plugin loaded, and its version.

WEBHOOK_URL="https://webhooks.fivetran.com/webhooks/67c64b0b-1439-4a35-a8af-5ea980d638a3"
CONNECT_TIMEOUT_SECONDS="${CONNECT_TIMEOUT_SECONDS:-2}"
REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS:-3}"

PLUGIN_JSON="${CLAUDE_PLUGIN_ROOT:-}/.claude-plugin/plugin.json"

body=$(PLUGIN_JSON="$PLUGIN_JSON" python3 -c "
import datetime, json, os, sys, uuid

try: event = json.loads(sys.stdin.buffer.read() or b'{}')
except Exception: event = {}

try: manifest = json.load(open(os.environ['PLUGIN_JSON']))
except Exception: manifest = {}

client_id = None
try:
    p = os.path.expanduser('~/.fivetran/client-id')
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f'{p}.{os.getpid()}.tmp'
        open(tmp, 'w').write(str(uuid.uuid4()))
        os.rename(tmp, p)
    client_id = open(p).read().strip() or None
except OSError: pass

print(json.dumps({
    'event': 'Plugin Session Start',
    'plugin': manifest.get('name', 'unknown'),
    'version': manifest.get('version', 'unknown'),
    'source': event.get('source'),
    'model': event.get('model'),
    'session_id': event.get('session_id'),
    'anonymous_id': client_id,
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
