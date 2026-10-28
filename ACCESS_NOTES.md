# Access / run notes (course staff)

## Endpoint registry bases

Staff append `/test`, `/test/setup`, `/test/run`, or `/test/report`.

| Role | Base URL |
|------|----------|
| **System** | `https://eu-ai-act-monitor-api.fly.dev/system` |
| **Tester** | `https://eu-ai-act-monitor-api.fly.dev/tester` |

No authentication on the testing interface (`/system/test/*`, `/tester/test/*`).

Always on (Fly.io, region `iad`). `auto_stop_machines = off`.

## Flow

1. `POST …/test/setup` (JSON body optional) → `{ "session_id": "…" }`
   - Tester may include `{ "target": "https://…", "target_url": "https://…" }` for the peer system.
2. `POST …/test/run` with `{ "session_id": "…" }`
3. `GET …/test/report?session_id=…`

System setup resets fixtures. One suite / session at a time; shared DB and test clock.

## Product API keys (cross-team /v1 only; not required for harness)

| Role | Header |
|------|--------|
| Ingest (`sys-42`) | `Authorization: Bearer test-ingest-key-sys-42-0d6f1c2e9b8a4f37` |
| Reviewer | `Authorization: Bearer test-reviewer-key-rev-1-5a7e3b9c1d2f4086` |
| Viewer | `Authorization: Bearer test-viewer-key-viewer-1-8c4d2e6f0a1b3957` |

## Pod spec

Also mirrored publicly at: `https://eu-ai-act-monitor-api.fly.dev/pod_spec.txt`
