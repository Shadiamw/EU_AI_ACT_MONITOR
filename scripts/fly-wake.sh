#!/usr/bin/env bash
# Start Fly Postgres + API + worker for a demo. Saves free-trial allowance by
# only running when you need the service. Pair with ./scripts/fly-sleep.sh after.
set -euo pipefail
export PATH="${HOME}/.fly/bin:${PATH}"

API_APP="${FLY_APP:-eu-ai-act-monitor-api}"
DB_APP="${FLY_POSTGRES_APP:-eu-ai-act-monitor-db}"
API_ID="${FLY_API_MACHINE:-82d1e65b660558}"
WORKER_ID="${FLY_WORKER_MACHINE:-8535d6f47525e8}"
DB_ID="${FLY_DB_MACHINE:-8edd542f3275e8}"
BASE_URL="${SUITE_BASE_URL:-https://eu-ai-act-monitor-api.fly.dev}"

if ! fly auth whoami >/dev/null 2>&1; then
  echo "Run: fly auth login" >&2
  exit 1
fi

echo "Starting Postgres…"
fly machine start "$DB_ID" -a "$DB_APP" >/dev/null
echo "Starting API…"
fly machine start "$API_ID" -a "$API_APP" >/dev/null
echo "Starting worker…"
fly machine start "$WORKER_ID" -a "$API_APP" >/dev/null

echo "Waiting for /healthz to be healthy…"
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  code=$(curl -sS -m 5 -o /tmp/fly-hz.json -w '%{http_code}' "$BASE_URL/healthz" || true)
  if [[ "$code" == "200" ]]; then
    echo "Healthy:"
    cat /tmp/fly-hz.json
    echo
    echo "UI:  https://eu-ai-act-monitor.vercel.app"
    echo "API: $BASE_URL"
    exit 0
  fi
  sleep 3
done

echo "Timed out waiting for healthy /healthz (last HTTP $code)." >&2
cat /tmp/fly-hz.json 2>/dev/null || true
echo >&2
exit 1
