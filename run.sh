#!/bin/sh
# Start Housekeeping without Docker at http://localhost:8000
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
# Read .env (the file Docker Compose uses) without overriding variables already set in your shell.
# Handles comments, blank lines, `export KEY=value`, spaces around =, quoted values, Windows line endings
# and a last line with no newline.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(printf '%s' "$line" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/^export[[:space:]]\{1,\}//')
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key=$(printf '%s' "${line%%=*}" | sed 's/[[:space:]]*$//')
    value=$(printf '%s' "${line#*=}" | sed 's/^[[:space:]]*//')
    case "$value" in
      \"*\"*) value=${value#\"}; value=${value%%\"*} ;;   # "quoted"  # comment
      \'*\'*) value=${value#\'}; value=${value%%\'*} ;;
      *) value=$(printf '%s' "$value" | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//') ;;
    esac
    printf '%s' "$key" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]*$' || { echo "run.sh: ignoring line in .env: $key" >&2; continue; }
    eval "current=\${$key:-}"
    if [ -z "$current" ] && [ -n "$value" ]; then export "$key=$value"; fi
  done < .env
fi
exec .venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
