# Access / run notes (course staff)

## Deployed bases (endpoint registry)

Always-on Fly.io (no laptop required). Staff appends `/test`, `/test/setup`, `/test/run`, or `/test/report`.

| Role | Base URL |
|------|----------|
| **System** | `https://eu-ai-act-monitor-api.fly.dev/system` |
| **Tester** | `https://eu-ai-act-monitor-api.fly.dev/tester` |

Examples:

- `POST https://eu-ai-act-monitor-api.fly.dev/system/test/setup`
- `GET  https://eu-ai-act-monitor-api.fly.dev/system/test/report?session_id=...`
- `POST https://eu-ai-act-monitor-api.fly.dev/tester/test/setup`
- `GET  https://eu-ai-act-monitor-api.fly.dev/tester/test/report?session_id=...`

Product UI: `https://eu-ai-act-monitor.vercel.app`  
Product API: `https://eu-ai-act-monitor-api.fly.dev`

## Pod specification

`pod_spec.txt` on branch `development-deploy`.

Raw (after push):  
`https://raw.githubusercontent.com/Shadiamw/EU_AI_ACT_MONITOR/development-deploy/pod_spec.txt`

## Authentication

Harness (`/system/test/*`, `/tester/test/*`): **no auth**.

Product API Bearer keys (test fixtures):

| Role | Header |
|------|--------|
| System ingest (`sys-42`) | `Authorization: Bearer test-ingest-key-sys-42-0d6f1c2e9b8a4f37` |
| Reviewer | `Authorization: Bearer test-reviewer-key-rev-1-5a7e3b9c1d2f4086` |
| Viewer | `Authorization: Bearer test-viewer-key-viewer-1-8c4d2e6f0a1b3957` |

`POST /v1/test/reset` and `POST /v1/test/clock/advance` are open in `ENVIRONMENT=test`.

## Availability

API + worker stay always-on for Wed–Thu (`auto_stop_machines = off`, `min_machines_running = 1`). Region: `iad`.

## Tester target

`POST /tester/test/setup` may include `target` / `system_url` / `base_url`.  
Default if omitted: podmate at `https://34-57-175-31.sslip.io`.
