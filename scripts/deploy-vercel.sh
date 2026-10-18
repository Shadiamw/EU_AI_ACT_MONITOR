#!/usr/bin/env bash
# Deploy the console UI to Vercel. The API stays on Docker (optionally behind a tunnel).
#
# Usage:
#   vercel login
#   API_BASE_URL=https://your-api.example.com ./scripts/deploy-vercel.sh
#
# Or with a token:
#   VERCEL_TOKEN=... API_BASE_URL=... ./scripts/deploy-vercel.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -z "${API_BASE_URL:-}" ]]; then
  echo "Set API_BASE_URL to the public API origin (no trailing slash)." >&2
  echo "Example: API_BASE_URL=https://abc.trycloudflare.com $0" >&2
  exit 2
fi

export API_BASE_URL="${API_BASE_URL%/}"
node scripts/vercel-build.mjs

# Folder name EU_AI_ACT_MONITOR becomes an invalid Vercel slug (---); pin a clean name.
PROJECT="${VERCEL_PROJECT:-eu-ai-act-monitor}"
ARGS=(--yes --prod --project "$PROJECT")
if [[ -n "${VERCEL_TOKEN:-}" ]]; then
  ARGS+=(--token "$VERCEL_TOKEN")
fi

echo "Deploying UI project=$PROJECT API_BASE_URL=$API_BASE_URL"
exec vercel deploy "${ARGS[@]}"
