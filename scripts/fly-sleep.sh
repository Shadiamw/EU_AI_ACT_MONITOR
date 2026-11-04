#!/usr/bin/env bash
# Stop Fly API + worker + Postgres to save free-trial allowance after a demo.
set -euo pipefail
export PATH="${HOME}/.fly/bin:${PATH}"

API_APP="${FLY_APP:-eu-ai-act-monitor-api}"
DB_APP="${FLY_POSTGRES_APP:-eu-ai-act-monitor-db}"
API_ID="${FLY_API_MACHINE:-82d1e65b660558}"
WORKER_ID="${FLY_WORKER_MACHINE:-8535d6f47525e8}"
DB_ID="${FLY_DB_MACHINE:-8edd542f3275e8}"

if ! fly auth whoami >/dev/null 2>&1; then
  echo "Run: fly auth login" >&2
  exit 1
fi

echo "Stopping worker…"
fly machine stop "$WORKER_ID" -a "$API_APP" >/dev/null || true
echo "Stopping API…"
fly machine stop "$API_ID" -a "$API_APP" >/dev/null || true
echo "Stopping Postgres…"
fly machine stop "$DB_ID" -a "$DB_APP" >/dev/null || true
echo "All stopped. Run ./scripts/fly-wake.sh before the next demo."
