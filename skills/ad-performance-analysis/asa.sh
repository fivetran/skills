#!/usr/bin/env bash
set -euo pipefail
if ! command -v python3 >/dev/null 2>&1; then
  echo "[asa] python3 is required but not found. Install it via your package manager." >&2
  exit 1
fi
exec python3 "$(dirname "${BASH_SOURCE[0]}")/asa.py" "$@"
