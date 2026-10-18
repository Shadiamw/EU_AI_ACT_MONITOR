# Part 1: ingestion, detection and events

Part 1 builds `POST /v1/metrics`, `GET /v1/events`, `GET /v1/events/{event_id}`, the two 501
stubs (`POST /v1/events/{event_id}/disposition`, `GET /v1/events/{event_id}/report`),
authentication and the error format, to `Phase3_Implementation_Spec.docx` ("the spec")
sections 1.1 to 1.9. It calls Part 2's `ledger.append` and `audit.record_view` and Part 3's
`gaps.close_if_open` and `notifications.enqueue`. The decisions it rests on are 116 to 131 in
`docs/decisions.md`; the tests that prove each rule are mapped in `docs/part1_test_map.md`; the
clause-by-clause check is `docs/part1_spec_audit.md`.

Not built here (Part 4): the real clock and `clock_state`, `POST /v1/test/reset`,
`POST /v1/test/clock/advance`, `/healthz`, roles setup, CI, Docker and AWS, the acceptance
runner and the sender simulator. Their stand-ins are marked STUB (integration_notes.md
section 10).

## Code

| File | What it holds |
|---|---|
| `app/auth.py` | `Principal`, Bearer key resolution, `require(*roles)` (decision 123) |
| `app/errors.py` | `ApiError` and the global handlers (Part 2's module, adopted; decision 131) |
| `app/ingestion.py` | The request model and the step 2 to 11 algorithm of spec 1.4 |
| `app/evidence.py` | The evidence bundle and its SHA-256 (decision 119) |
| `app/events.py` | The contract Event object and list summaries |
| `app/jsonout.py` | JSON responses that write Decimals as exact JSON numbers (decision 124) |
| `app/observability.py` | The request log line and counters (decision 126) |
| `app/api/ingestion_routes.py` | The three endpoints |
| `app/api/stub_routes.py` | The two 501 stubs (decision 125) |
| `migrations/versions/0003_detection.py` | metric_sample, breach_series, event, harm_field (decision 127) |
| `migrations/versions/0001_platform_stub.py` | Part 4's stub, with api_key and threshold_version added |
| `tools/seed_part1.py` | STUB for Part 4: the spec's seed fixtures (decision 128) |

## Run locally

Use the Python 3.12 and PostgreSQL 16 setup in README section 5. With DATABASE_URL,
DATABASE_ADMIN_URL, ENVIRONMENT=test and the three keys set:

```sh
export INGEST_KEY_SYS_42=... REVIEWER_KEY=... VIEWER_KEY=...   # your own test values
.venv/bin/alembic upgrade head
.venv/bin/python -m tools.seed_part1      # ENVIRONMENT=test only; uses DATABASE_ADMIN_URL
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

uvicorn is not a project dependency yet (integration_notes.md section 5.7). The keys come from
the environment (spec 5) and only their SHA-256 digests are stored. The test suite sets its own
test-only values in `tests/conftest.py`.

```sh
curl -X POST http://127.0.0.1:8000/v1/metrics \
  -H "Authorization: Bearer $INGEST_KEY_SYS_42" -H 'Content-Type: application/json' \
  -d '{"system_id":"sys-42","metric_name":"accuracy_drift","metric_type":"drift","value":0.34,"timestamp":"2026-09-27T10:05:00Z"}'
```

returns 201 `{"sample_id": "smp-…", "flagged_event_id": "evt-…"}` (contract Example 1).

## Behaviour in brief

- **Auth** (spec 1.3, 4): `Authorization: Bearer <key>`; SHA-256 digest compared in constant
  time against every stored digest. 401, then 403 FORBIDDEN_ROLE, then 400 MALFORMED_BODY, then
  403 SYSTEM_MISMATCH. Roles: ingest SYSTEM only; list any role; detail, audit, export and
  verify REVIEWER or VIEWER; disposition REVIEWER only; report REVIEWER or VIEWER.
- **Ingest** (spec 1.4): validate (decision 121), lock the system row, insert-or-ignore on
  (system_id, metric_name, timestamp). A duplicate is 200 with the original ids, sets
  last_received_at and does nothing else (decision 116). Otherwise set last_received_at,
  close an open gap, read the newest threshold version (decision 120): none gives 201
  `{sample_id}`; healthy closes the open series (decision 129); a breach joins the open series
  or creates the event, its four harm fields and a series, appends EVENT_FLAGGED, then
  enqueues the notification. One transaction; lock order in decision 122.
- **Thresholds** are strict: gt breaches when value > limit, lt when value < limit, compared
  as Decimal. A value equal to the limit never breaches.
- **A new event** (spec 1.5): detected_at is the sample's timestamp, received_at is
  `clock.now()`, retain_until is detected_at plus six calendar months and at least 183 days,
  harm fields PREFILLED_PENDING_CONFIRMATION with null evidence and confirmer.
- **List** (spec 1.6): a bare array of summaries, newest first, any role, no ledger entry,
  limit 50 by default and capped at 200 (decisions 117, 118).
- **Detail** (spec 1.6): the contract Event object, exact decimals, EVENT_VIEWED in the same
  transaction; unknown id 404 with nothing written.
- **Logs and counters** (spec 1.8): decision 126.

## What changed in Part 1 and why (for atharej)

Your Part 1 (`5310ae8`) was written before the spec reached us, against a pre-fix Part 3. It
was merged into this branch with a real merge commit, so your commit stays in the history,
then reworked to the spec. The full list, clause by clause, is `docs/part1_spec_audit.md`; the
fate of each of your tests is the last table of `docs/part1_test_map.md`. The main changes:

| Was | Now | Why |
|---|---|---|
| `X-API-Key` header | `Authorization: Bearer <key>` | Spec 4, Auth (pinned) |
| Published fixed keys in the seed and in this file | Keys from INGEST_KEY_SYS_42, REVIEWER_KEY, VIEWER_KEY | Spec 5 |
| Disabled keys (`enabled` column) | No such column | Spec 4.4 lists the api_key columns |
| Duplicate identity includes metric_type; changed value 409; heartbeat unchanged | (system_id, metric_name, timestamp); 200 with the original ids; last_received_at set | Spec 1.4 step 5 (decision 116) |
| No threshold: 422 THRESHOLD_NOT_CONFIGURED | 201 `{sample_id}` | Spec 1.4 step 7; 422 is not a spec code |
| Unknown system after a valid key: 404 | 500 | Spec 1.4 step 4 |
| Threshold valid at the sample's time | Highest `version` | Spec 1.4 step 7 (decision 120) |
| accuracy limit 0.80 | lt 0.70 | Spec 4, Seed fixtures |
| detected_at = server time | detected_at = the sample's timestamp | Spec 1.5 and contract Example 2 |
| Only a newer healthy sample ends a series | Any healthy sample does | Spec 1.4 step 9 (decision 129) |
| `event.series_open` | breach_series table | Spec 1.7 |
| Evidence: all metrics, a bare list | This metric only, `{window, samples}` | Spec 1.5 (decision 119) |
| Harm-field evidence text | Null evidence, PREFILLED_PENDING_CONFIRMATION | Spec 1.5 and 4 (Phase 3 stub shape) |
| Gap harm fields shown as PREFILLED | NOT_APPLICABLE, as Part 3 writes them | Spec 4 |
| JSON numbers through float | Exact decimals | Spec 1.6 (decision 124) |
| List: full Event objects, oldest first, X-Total-Count, SYSTEM sees its own system | Summaries, newest first, no header, any role sees all | Spec 1.6 (decisions 117, 118) |
| limit 0 or above 200 is 400 | limit 0 gives `[]`; above 200 is capped | Spec 1.6 "max 200" and the user's decision |
| Extra body fields 400; names limited to 256 characters, empty refused | Extra fields ignored; no length rule | Spec 1.4; decision 121 |
| No disposition or report endpoints | 501 stubs after auth | Spec 1.6 (decision 125) |
| No request log line or counters | Both | Spec 1.8 (decision 126) |
| Migration 0004 altering `event`, keys and thresholds read-only for the app | Tables in 0001 and 0003; normal read and write | Spec 4.4 (decision 127) |

Kept from your version: the short autocommit connection for the key lookup (decision 114), the
system-row lock first, insert-or-ignore on a unique key, Decimal parsing with `parse_float`,
the 100-digit and 50-place bound on values, and the tests' real-webhook and rollback checks.
