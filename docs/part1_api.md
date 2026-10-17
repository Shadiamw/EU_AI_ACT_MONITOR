# Part 1: detection and ingestion

Part 1 implements `POST /v1/metrics`, `GET /v1/events`, and
`GET /v1/events/{event_id}`. It uses the existing Part 2 ledger/audit and
Part 3 gap/notification functions. Disposition, reports, deployment, reset and
the simulated-clock endpoint remain outside this implementation.

## Run locally

Use the Python 3.12/PostgreSQL 16 setup in README section 5. With both database URLs
configured, run:

```sh
python -m pip install -e '.[dev]'
python -m alembic upgrade head
# Only for local/test fixtures; never enable these public credentials in production.
ENVIRONMENT=test python -m tools.seed_part1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# In a separate terminal, with the same database environment:
python -m app.worker
```

PowerShell uses `$env:ENVIRONMENT='test'` before `python -m tools.seed_part1`.
Seeding is explicit and idempotent. Migrations and normal startup never install
demo credentials. The seed command uses DATABASE_ADMIN_URL; the API and worker use
DATABASE_URL. Part 4 retains ownership of startup SEED ledger entries and reset.

The optional `dev` installation includes uvicorn. Alembic head is now **0004**;
the new migration extends the existing schema without rewriting revisions 0001-0003.

## Credentials and fixtures

Send credentials in the `X-API-Key` header. Only SHA-256 digests are stored in `api_key`.
Credential lookup uses a short autocommit connection closed before handler work, so
authentication cannot leave a transaction open during webhook verification.
The following are intentionally public, local-test credentials:

| Role | Actor | Key |
| --- | --- | --- |
| SYSTEM | sys-42 | local-test-system-sys42-9e4bb169f4584fa69d92 |
| REVIEWER | rev-1 | local-test-reviewer-rev1-830607463f814883a285 |
| VIEWER | viewer-1 | local-test-viewer-1-6f57355ca3764cce9acf |

Seeded thresholds, effective from 2000-01-01T00:00:00Z:

| Version | System | Metric | Type | Breach condition |
| --- | --- | --- | --- | --- |
| thr-1 | sys-42 | accuracy_drift | drift | value > 0.30 |
| thr-2 | sys-42 | accuracy | performance | value < 0.80 |

The drift fixture matches the convention's example. The accuracy fixture and its
0.80 limit are provisional. For production, Part 4 must provision independently
generated high-entropy keys and the team's real thresholds using the owner role.
The application can read keys/thresholds but cannot insert, update, or delete them.
Threshold changes are new rows with distinct version IDs and effective times.

Example request:

```sh
curl -X POST http://127.0.0.1:8000/v1/metrics \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: local-test-system-sys42-9e4bb169f4584fa69d92' \
  -d '{"system_id":"sys-42","metric_name":"accuracy_drift","metric_type":"drift","value":0.34,"timestamp":"2026-10-17T12:00:00Z"}'
```

Success is 201 with `{sample_id, flagged_event_id}` for breaches, or `{sample_id}`
otherwise, following the convention's fixed example. Event IDs and sample IDs are
opaque strings. Event detail returns the convention's Event shape, with JSON
numbers for metric values and all four HarmField objects. Exact decimal values
are retained in PostgreSQL, evidence bundles, and ledger payload strings; HTTP
numeric presentation uses the usual floating-point conversion.

## Ingestion behavior and assumptions to confirm with the team

These choices fill gaps in the supplied materials. They are implemented and tested,
but should be reconciled with the separate Phase 3 specification if it is recovered.

1. **Identity/retries.** The contract has no snapshot ID. A sample is identified by
   `(system_id, metric_name, metric_type, timestamp)`; equivalent UTC representations
   identify the same instant. An identical numeric value returns the original 201
   IDs. A changed value returns 409 DUPLICATE_CONFLICT. Retries change no heartbeat,
   gap, event, notification or ledger state.
2. **Thresholds.** Select the newest version effective at the sample's timestamp.
   Comparators are explicit `gt` or `lt`; equality is healthy. Missing configuration
   returns 422 THRESHOLD_NOT_CONFIGURED and writes nothing. Each sample records its
   evaluated version. An event keeps the first breach's value and threshold version
   even if later occurrences were evaluated under a newer version.
3. **Series.** At most one open series exists per system/metric/type. Repeated
   breaches link additional stored samples to it and return the same event ID,
   without further flag or notification entries. A healthy sample newer than all
   previously stored samples ends the series. This does not close the human review
   event: it stays FLAGGED. Late healthy data cannot end a current series. A late
   breach joins the current series or creates one if none exists. A later breach
   after recovery creates a fresh event. Phase 4 must clear `series_open` on human
   closure if it introduces an independent closure path.
4. **Time.** `detected_at` and `last_received_at` use the service's `clock.now()`;
   sample timestamps are preserved separately. Requests require UTC ISO-8601
   timestamps and finite JSON numbers (not numeric strings or booleans). Future
   timestamps are accepted because no skew bound was specified. A sample timestamp
   does not advance the service clock. Text IDs and metric names are limited to
   256 characters; values support up to 100 digits with up to 50 fractional digits.
5. **Evidence.** Capture a fixed snapshot of this system's already stored samples
   in the inclusive preceding hour `[sample timestamp - 1 hour, sample timestamp]`,
   across metrics, including the triggering sample. Future or later-arriving samples
   are not added retroactively. `event.evidence_bundle` holds decimal strings,
   sample IDs, timestamps and evaluated threshold versions for future reporting.
   Each harm field contains factual metric context with
   PREFILLED_PENDING_CONFIRMATION, with no human confirmation or legal judgment.
6. **Listing.** Return an array of Event objects (the full object also serves as a
   summary), ordered by `detected_at, event_id` ascending. Query parameters:
   `system_id`, `state`, `type`, inclusive `since`, `limit` (1-200, default 50),
   `offset` (default 0). Pagination metadata uses `X-Total-Count`, `X-Limit`,
   `X-Offset` headers. Lists do not append EVENT_VIEWED; detail reads do.
7. **Roles.** Every authenticated role may list events, with SYSTEM restricted to
   its own system. REVIEWER/VIEWER may read event detail; existing ledger, audit
   and webhook roles are preserved. SYSTEM alone may ingest and must match the
   body's system_id; mismatches return 403 SYSTEM_MISMATCH. An authenticated system
   missing from monitored_system returns 404 NOT_FOUND. Auth precedes body validation
   and resource lookup. Wrong HTTP methods retain the existing 404 behavior.

## Transactions and platform handoff

Ingestion locks the monitored-system row, then checks duplicates and selects the
threshold. It stores the sample, updates receipt time, and closes an open gap through
`gaps.close_if_open`. A new breach stores its evidence/event/occurrence, appends
EVENT_FLAGGED, then calls `notifications.enqueue` (NOTIFICATION_REQUESTED).
All writes commit before the response. A failure rolls back all of them, including
gap closure. No network calls occur in this transaction.

The existing lock order is preserved: system row, event/notification rows, ledger
lock. As the integration notes require, gap closure may acquire the ledger lock
before the remaining ingest work. The per-system lock and unique constraints
serialize concurrent requests without duplicate events or notifications.

Migration 0004 supplies minimal `api_key` and `threshold_version` platform contracts
so Part 1 is runnable before Part 4 lands. It also adds `metric_sample`, event
evidence, series tracking, threshold version, and reserved disposition/deadline/revision
fields for contract serialization. Part 4 should extend these through new migrations,
include the new tables in reset, and add its normal clock, seeding and timeout setup.

Worker gap rows remain compatible. Their legacy null harm fields are serialized
as pending HarmField objects with null evidence/confirmation; the worker's storage
format and behavior are unchanged.

## Verification

`python -m pytest tests/part1` covers real-key authorization, threshold boundaries,
version selection, replay/conflicts, evidence precision, series/recovery, pagination,
gap closure, rollback after a ledger write, and concurrent retries. Run the full
suite plus Ruff and strict mypy as described in README section 5.


Validation on 2026-10-17 used Windows, Python 3.12.10 and a real PostgreSQL 16.15
server with separate non-superuser owner/application roles. All 34 Part 1 tests
passed, including a real HTTP webhook receiver and the check that verification and
delivery do not run inside an application transaction. A separate migration check
preserved a populated Part 3 event and ledger hash through upgrade, downgrade, and
re-upgrade. Ruff and strict mypy passed. The disposable test cluster used disabled
crash-durability flushes during part of validation; crash recovery was not tested.


The broad regression run exercised 649 cases: 648 passed and one legacy worker
assertion expected only the pre-0004 event columns. That assertion now also checks
all seven added columns and their gap defaults; its rerun passed against the main
test database. The 34 Part 1 cases were additionally rerun on the final authentication
implementation in an isolated database and all passed. No worker behavior was changed.
