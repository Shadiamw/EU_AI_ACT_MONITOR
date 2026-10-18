#!/usr/bin/env bash
# Pod-meeting walkthrough against a running stack on localhost:8000.
# Prerequisites: docker compose up --build -d
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8000}"
INGEST_KEY="${INGEST_KEY_SYS_42:-test-ingest-key-sys-42-0d6f1c2e9b8a4f37}"
REVIEWER_KEY="${REVIEWER_KEY:-test-reviewer-key-rev-1-5a7e3b9c1d2f4086}"
VIEWER_KEY="${VIEWER_KEY:-test-viewer-key-viewer-1-8c4d2e6f0a1b3957}"

auth() { printf 'Authorization: Bearer %s' "$1"; }

echo "== health =="
curl -fsS "$BASE/healthz"; echo

echo "== reset =="
curl -fsS -X POST "$BASE/v1/test/reset"; echo

echo "== verify after seed =="
curl -fsS -H "$(auth "$VIEWER_KEY")" "$BASE/v1/ledger/verify"; echo

# A local webhook receiver for the demo (prints each POST body).
RECEIVER_PORT="${RECEIVER_PORT:-8765}"
python3 - <<'PY' &
from http.server import BaseHTTPRequestHandler, HTTPServer
import os, json
port = int(os.environ.get("RECEIVER_PORT", "8765"))
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        print("WEBHOOK:", body.decode(), flush=True)
        self.send_response(200); self.end_headers()
    def log_message(self, *args): pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
RECEIVER_PID=$!
trap 'kill $RECEIVER_PID 2>/dev/null || true' EXIT
sleep 0.3

# From inside Docker the host receiver is host.docker.internal on Desktop.
WEBHOOK_URL="${WEBHOOK_URL:-http://host.docker.internal:${RECEIVER_PORT}/hook}"
echo "== register webhook $WEBHOOK_URL =="
curl -fsS -X POST "$BASE/v1/webhooks" \
  -H "$(auth "$REVIEWER_KEY")" -H 'Content-Type: application/json' \
  -d "{\"url\":\"$WEBHOOK_URL\",\"event_types\":[\"flag_notification\"]}"; echo

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "== ingest accuracy_drift 0.34 at $TS =="
FLAG=$(curl -fsS -X POST "$BASE/v1/metrics" \
  -H "$(auth "$INGEST_KEY")" -H 'Content-Type: application/json' \
  -d "{\"system_id\":\"sys-42\",\"metric_name\":\"accuracy_drift\",\"metric_type\":\"drift\",\"value\":0.34,\"timestamp\":\"$TS\"}")
echo "$FLAG"
EVENT_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['flagged_event_id'])" <<<"$FLAG")

echo "== wait briefly for delivery =="
sleep 6

echo "== event detail =="
curl -fsS -H "$(auth "$VIEWER_KEY")" "$BASE/v1/events/$EVENT_ID"; echo

echo "== advance 900s (three missed intervals) =="
curl -fsS -X POST "$BASE/v1/test/clock/advance" \
  -H 'Content-Type: application/json' -d '{"seconds":900}'; echo
sleep 2

echo "== monitoring gaps =="
curl -fsS -H "$(auth "$VIEWER_KEY")" "$BASE/v1/events?type=monitoring_gap"; echo

echo "== ledger verify =="
curl -fsS -H "$(auth "$VIEWER_KEY")" "$BASE/v1/ledger/verify"; echo

echo "== export entry types =="
curl -fsS -H "$(auth "$VIEWER_KEY")" "$BASE/v1/ledger/export" \
  | python3 -c "import json,sys; print([e['entry_type'] for e in json.load(sys.stdin)])"

echo "demo ok"
