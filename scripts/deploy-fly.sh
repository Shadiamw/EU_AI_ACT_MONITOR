#!/usr/bin/env bash
# Deploy API + worker + Postgres to Fly.io, then print the public API URL.
# Prerequisites: fly auth login
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="${HOME}/.fly/bin:${PATH}"

APP="${FLY_APP:-eu-ai-act-monitor-api}"
PG_APP="${FLY_POSTGRES_APP:-eu-ai-act-monitor-db}"
REGION="${FLY_REGION:-iad}"

if ! fly auth whoami >/dev/null 2>&1; then
  echo "Run: fly auth login" >&2
  exit 1
fi

if ! fly apps list --json 2>/dev/null | python3 -c "import sys,json; print(any(a.get('Name')=='$APP' for a in json.load(sys.stdin)))" | grep -q True; then
  fly apps create "$APP" --org personal 2>/dev/null || fly apps create "$APP"
fi

if ! fly apps list --json 2>/dev/null | python3 -c "import sys,json; print(any(a.get('Name')=='$PG_APP' for a in json.load(sys.stdin)))" | grep -q True; then
  echo "Creating Fly Postgres ($PG_APP)…"
  fly postgres create --name "$PG_APP" --region "$REGION" --initial-cluster-size 1 --vm-size shared-cpu-1x --volume-size 1 --yes
fi

echo "Attaching Postgres to $APP…"
fly postgres attach "$PG_APP" --app "$APP" --yes 2>/dev/null || true

# DATABASE_URL from attach is the Fly superuser-ish URL. Bootstrap app roles, then set app URLs.
PROXY_PORT=15432
fly proxy ${PROXY_PORT}:5432 -a "$PG_APP" &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT
sleep 3

# Resolve credentials from the app secrets / postgres status
DB_URL="$(fly ssh console -a "$APP" -C 'printenv DATABASE_URL' 2>/dev/null || true)"
if [[ -z "$DB_URL" ]]; then
  # Fresh attach stores DATABASE_URL on the app; fetch via secrets list is redacted — use postgres connect.
  echo "Waiting for DATABASE_URL on app…"
  sleep 2
fi

# Use fly postgres connect to run bootstrap (runs as DB owner).
echo "Bootstrapping monitor_owner / monitor_app…"
fly postgres connect -a "$PG_APP" --database postgres < scripts/cloud-bootstrap-db.sql

# Internal host for Fly Postgres is typically <pg-app>.internal
# Prefer rewriting from attached DATABASE_URL host if we can read it via release machine later.
# Set explicit URLs used by the app (same passwords as local compose fixtures).
PG_HOST="${PG_APP}.internal"
fly secrets set -a "$APP" \
  DATABASE_URL="postgresql+psycopg://monitor_app:monitor_app@${PG_HOST}:5432/monitor_test" \
  DATABASE_ADMIN_URL="postgresql+psycopg://monitor_owner:monitor_owner@${PG_HOST}:5432/monitor_test" \
  SUITE_BASE_URL="https://${APP}.fly.dev"

echo "Deploying $APP…"
fly deploy -a "$APP" --ha=false

API_URL="https://${APP}.fly.dev"
echo ""
echo "API:  $API_URL"
echo "UI:   set Vercel API_BASE_URL=$API_URL then redeploy the frontend"
echo "Try:  curl -sS $API_URL/healthz"
