#!/bin/sh
# Start Housekeeping without Docker at http://localhost:8000
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
# Read .env (the file Docker Compose uses) without overriding variables already set in your shell.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    eval "current=\${$key:-}"
    if [ -z "$current" ] && [ -n "$value" ]; then export "$key=$value"; fi
  done < .env
fi
exec .venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
