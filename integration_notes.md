# Integration notes: Part 2 (ledger and audit), with Parts 1 and 3

**Parts 1, 2 and 3 are integrated on `developed-part123merged`.** Part 1 is built to the spec:
[docs/part1_api.md](docs/part1_api.md) describes it, and section 3.9 below says what changed in
Part 1 and why. Section 3.1 is kept as the record of the Part 1 stubs, all now replaced.
Section 5.12 lists what Part 4 must do when it lands, and section 10 the stubs that remain,
all Part 4's.

These notes are for the owners of Part 1 (API), Part 3 (worker) and Part 4 (platform). They say
what Part 2 provides, what every caller must do, and what each part replaces or checks when its
code lands. Part 3 is now in this repository too: section 4.6 gives its settled
interfaces, and section 10 lists the stubs Parts 1 and 4 replace.

How to read the citations:

- "decision N" is entry N in `docs/decisions.md`.
- A path such as `app/ledger/service.py` is a file in this repository. `file::test_name` is a
  test in that file.
- "spec, 'Section'" is a section of the Phase 3 specification. "addendum rule N" is a rule of
  the Part 2 addendum. Both are project documents outside this repository.
- **ASSUMPTION** marks anything about another part's code, which does not exist yet. Treat it
  as a proposal to confirm, not a fact.

## 1. What the ledger is

The ledger is one append-only PostgreSQL table, `ledger_entry`, created by migration 0002
(`migrations/versions/0002_ledger.py`). Every entry has eight fields: seq, entry_type,
actor_id, subject_id, payload, recorded_at, prev_hash and hash (spec, 'Ledger entry format';
`app/ledger/read.py`). Each hash is the SHA-256 of the previous entry's hash followed by the
canonical JSON of the entry's six content fields, so changing, removing or reordering an entry
breaks the chain, except removal from the end (`app/ledger/canonical.py`; decisions 13 and 16).
Other parts write to it only through `ledger.append`, inside their own transaction, so a state
change and its ledger entries commit together or not at all (`app/ledger/service.py`; decision
8). The application role can only INSERT and SELECT, and a trigger blocks UPDATE and DELETE for
every role, the owner included (`migrations/versions/0002_ledger.py`;
`tests/test_schema_permissions.py`). Anyone with an export can re-check the chain, with the
server's verify endpoint or with the standard-library script `tools/verify_ledger.py`, and the
two give the same result (decisions 52 to 61; `tests/api/test_cross_verify.py`).

### Public interface

Python functions (`app/ledger/service.py`, re-exported by `app/ledger/__init__.py`;
`app/audit.py`):

```python
from sqlalchemy.orm import Session
from app import audit, ledger
from app.auth import Principal                  # Principal(role, actor_id, system_id)
from app.ledger.canonical import LedgerError, Payload

ledger.append(session: Session, entry_type: str, actor_id: str,
              subject_id: str | None, payload: Payload) -> ledger.LedgerEntry
ledger.ensure_seed(session: Session, payload: Payload) -> None
audit.record_view(session: Session, principal: Principal, event_id: str) -> None
```

- `Session` is a SQLAlchemy 2.x ORM Session (decision 8). `ensure_seed` and `record_view`
  return None; only `append` returns the entry (decision 9).
- `Payload` is `dict[str, JsonValue]`, where `JsonValue` is `str | int | bool | None`, a dict
  with str keys, or a list, nested (`app/ledger/canonical.py`).
- `LedgerEntry` is a frozen dataclass with the eight fields: seq (int), entry_type, actor_id,
  subject_id (str or None), payload, recorded_at, prev_hash and hash (`app/ledger/service.py`).
- `record_view` appends EVENT_VIEWED with `principal.actor_id`, subject `event_id` and payload
  `{}` (`app/audit.py`).
- `ledger.verify_ledger(session)` and `ledger.verify_entries(rows)` return the verify result
  (`app/ledger/verify.py`). No other part needs to call them.

Endpoints (`app/api/ledger_routes.py`). All three accept REVIEWER or VIEWER only. No principal
gives 401 MISSING_OR_INVALID_KEY, then a wrong role gives 403 FORBIDDEN_ROLE (decision 29).

| Endpoint | Success | Writes |
|---|---|---|
| `GET /v1/events/{event_id}/audit` | 200, array of `{actor_id, action, timestamp}` for every entry whose subject_id is the event id, in seq order. 404 NOT_FOUND for an unknown event. | AUDIT_VIEWED after the response is built, so a call never lists its own view (decision 31). Nothing on 404. |
| `GET /v1/ledger/export` | 200, array of every entry with its eight fields, in seq order; `[]` when empty. | Nothing (decision 32). |
| `GET /v1/ledger/verify` | 200, exactly `{valid, entries_checked, first_mismatch}` (decision 33). | Nothing (decision 32). |

Standalone verifier: `python3 tools/verify_ledger.py EXPORT_FILE` (section 6).

## 2. Rules for all callers

### 2.1 Transaction and isolation

- Call `append`, `ensure_seed` and `record_view` on an ORM Session that is inside your own
  transaction (decision 8). They never commit, roll back or begin a transaction; you commit
  (`app/ledger/service.py`; `tests/test_append.py::test_append_does_not_commit`).
- The session must be at READ COMMITTED, the PostgreSQL default (addendum rule 5). At
  REPEATABLE READ or SERIALIZABLE, `append` and `ensure_seed` raise LedgerError before taking
  the lock (`tests/test_append.py::test_append_rejects_other_isolation_levels`). The stub
  engine sets READ COMMITTED explicitly (`app/db.py`; decision 26).
- Several appends in one transaction are fine, and a rollback leaves no gap: the next append
  reuses the seq (`tests/test_append.py::test_several_appends_in_one_transaction`,
  `tests/test_append.py::test_rollback_leaves_chain_unchanged_and_seq_is_reused`).

### 2.2 Append last, because of the global lock

- `append` takes the transaction-scoped advisory lock `pg_advisory_xact_lock(1279607879)` and
  holds it until your transaction commits or rolls back (`app/ledger/service.py`;
  `tests/test_append.py::test_lock_is_held_until_the_transaction_ends`). Every other append in
  the system waits for it (`tests/test_append.py::test_lock_serializes_appends`).
- So make the ledger append the last write before commit, and commit promptly (spec, 'Transactions
  and lock order (Pinned)'). Lock order is always: (1) the monitored-system row, (2) event or
  notification rows, (3) the ledger lock inside `append` (same spec section).
- The key 1279607879 ("LEDG" in ASCII) is reserved for the ledger. No other code may take an
  advisory lock with it (`app/ledger/service.py`).
- Invalid input raises before the lock is taken, so it never holds the lock
  (`tests/test_append.py::test_invalid_input_changes_nothing_and_does_not_hold_lock`).

### 2.3 No network calls

Never make a network call (webhook delivery, verification ping) while a transaction that has
appended is open (spec, 'Transactions and lock order (Pinned)'). The ledger functions make none.

### 2.4 Allowed values

- **payload** is a dict with str keys. Values may be str, int, bool, None, and nested dicts
  (str keys) or lists of these (addendum rule 2; `app/ledger/canonical.py`).
- **Rejected with LedgerError:** any float (including 1.0, NaN and infinity), Decimal,
  datetime, bytes, set and tuple (`app/ledger/canonical.py`; addendum rule 2).
- **Decimals** must already be plain strings: `format(value, 'f')`, so `Decimal("0.34")` becomes
  `"0.34"` and scientific notation never appears (addendum rule 10). The addendum's worked
  example stores `"observed_value":"0.34"` and `"threshold_value":"0.30"` (addendum rule 7).
- **Integers** of any size are accepted. Above 2**53, a JavaScript re-verifier loses precision
  (decision 6).
- **Datetimes** inside a payload must be strings (addendum rule 10). The ledger does not check
  their format. **ASSUMPTION:** use the API format from spec, 'Shared rules (Pinned)'
  (`YYYY-MM-DDTHH:MM:SSZ`, fractional seconds only when present).
- **Strings** must not contain U+0000 or a lone surrogate, anywhere (decision 1).
- **actor_id** is a non-empty string. **subject_id** is a non-empty string, or None for "no
  subject"; never `""` (decision 5).
- **recorded_at** is set by `append` from `clock.now()`, truncated to milliseconds in UTC
  (decision 7). Callers do not pass it.
- `append` stores a deep copy of the payload (`tests/test_append.py::test_returned_payload_is_a_copy`).
- There is no per-type payload shape check: a misspelled or missing payload key is stored
  without error (decision 11). Your own tests must check your payload keys.

### 2.5 The ten entry types

entry_type must be one of these ten; anything else raises LedgerError (`app/ledger/canonical.py`).
"Written by" and the payload keys come from spec, 'Entry types and payloads'. The allowed
actor ids are rev-1, viewer-1, sys-42, or `system` for background work (spec, 'Ledger entry
format'). The spec says notification entries use the event id as subject_id, webhook entries
use the webhook id, and SEED uses null (same section).

| entry_type | Written by | actor_id | subject_id | Payload keys |
|---|---|---|---|---|
| SEED | Part 4, via `ensure_seed` | `system` (set by `ensure_seed`) | null | `systems` (list of ids), `threshold_count` (string, e.g. `"2"`, addendum rule 7) |
| EVENT_FLAGGED | Part 1, ingest | the system principal, e.g. `sys-42` (addendum rule 7) | event id | `event_id`, `system_id`, `metric_name`, `observed_value`, `threshold_value`, `threshold_version_id` |
| GAP_OPENED | Part 3, `gaps.sweep` | `system` (background work) | event id (decision 30) | `event_id`, `system_id`, `last_received_at` |
| GAP_CLOSED | Part 3, `gaps.close_if_open`, called inside Part 1's ingest | `system` (spec 3.2: the worker's actor is `system`; decision 101) | event id | `event_id`, `system_id`, `gap_seconds` |
| NOTIFICATION_REQUESTED | Part 3, `notifications.enqueue` (called by Part 1 for breaches, Part 3 for gaps) | `system`, for breaches and gaps alike; enqueue takes no actor (decision 93) | event id | `notification_id`, `event_id`, `notification_type` |
| NOTIFICATION_DELIVERED | Part 3, delivery pass | `system` (background work) | event id | `notification_id`, `webhook_id`, `attempt`, `latency_seconds`, `within_bound` |
| NOTIFICATION_FAILED | Part 3, delivery pass | `system` (background work) | event id | `notification_id`, `webhook_id`, `attempts`, `last_status` |
| WEBHOOK_REGISTERED | Part 3, `POST /v1/webhooks` | the registering principal (`rev-1` or `viewer-1`; decision 103) | webhook id (spec 3.6) | `webhook_id`, `url`, `event_types` |
| EVENT_VIEWED | Part 2, `audit.record_view`, called by Part 1 | `principal.actor_id` | event id | none (`{}`) |
| AUDIT_VIEWED | Part 2, the audit endpoint | `principal.actor_id` | event id | none (`{}`) |

The audit endpoint lists every entry whose subject_id is the event id (spec, 'Endpoints you
own'), so an entry about an event must use that event's id as subject_id or it will not appear
in the event's audit trail.

### 2.6 LedgerError is a caller bug

LedgerError means the caller passed something invalid (entry type, ids, payload or isolation
level). At the API edge it must become a 500 with the contract body
`{"error_code": "INTERNAL_ERROR", "message": "an internal error occurred"}`, never a crash or a
stack trace, and the transaction must roll back (decisions 28 and 36; `app/errors.py`;
`tests/api/test_errors_roles.py::test_append_failure_is_a_generic_500_and_commits_nothing`).
Only the exception's type name is logged (decision 36). **ASSUMPTION:** in the worker, where
there is no HTTP response, catch it, roll back, log the type name and move on.

### 2.7 No lock timeout

The ledger code waits for the advisory lock with no timeout (decision 10). One transaction that
appends and then hangs blocks every other append, which means every event view, every audit
call, every ingest that flags, and every worker append. Part 4 sets the timeout (section 5.4).

## 3. Part 1 (API owner)

### 3.1 Stubs to replace

Done: every row below was replaced by Part 1 on `developed-part123merged`, keeping the "Keep"
column (decisions 123 and 131; section 3.9). The table is kept as the record.

| File | What it stubs | Replace with | Keep |
|---|---|---|---|
| `app/errors.py` | `ApiError` and the global handlers: 404 and 405 become 404 NOT_FOUND, 422 becomes 400 MALFORMED_BODY, and a middleware turns any other exception into 500 INTERNAL_ERROR (decisions 27, 28, 36, 37) | Part 1's errors module, `errors.ApiError(status, error_code, message)` plus handlers (spec, 'Shared interfaces between parts (Pinned)') | The contract error body; LedgerError becomes 500 with nothing leaked (decision 28) |
| `app/auth.py` | `resolve_principal()` (always REVIEWER rev-1) and `require(*roles)`; `Principal` (decisions 29 and 41) | Part 1's key auth | `require(*roles)` returns `Principal(role, actor_id, system_id)`; 401 before 403 before any 404 lookup (decision 29) |
| `app/events.py` | `exists(session, event_id)`: true when the ledger has an EVENT_FLAGGED or GAP_OPENED entry with that subject_id (decisions 30 and 44) | A lookup on Part 1's event table | Same signature, called after the role check |
| `app/db.py` | The engine (application role, READ COMMITTED, pool 10 plus 40, `pool_pre_ping`) and `get_session`, which rolls back on error, closes, and never commits (decisions 25, 26, 38) | Part 1 or Part 4 session handling | Application role, READ COMMITTED, one transaction per request, no commit in teardown |
| `app/main.py` | `create_app()`, which mounts only the Part 2 router and the error handlers, with `/docs`, `/redoc` and `/openapi.json` off (decision 39) | Part 1's app | It must include `app.api.ledger_routes.router` and `app.api.webhook_routes.router` (Part 3) |
| `tests/api/g2_routes.py` | Test-only `GET /v1/events/{event_id}` and two `/_test/` routes; never mounted by `app/main.py` (decision 34) | Part 1's real event-detail endpoint | The test-only routes stay out of the real app |

Effects on the Part 2 tests:

- The endpoint tests choose the caller by overriding `app.auth.resolve_principal` with an
  `X-Test-Actor` header reader (`tests/api/conftest.py`; decision 42). **ASSUMPTION:** if Part
  1's `require` gets the principal from a different dependency, the override in
  `tests/api/conftest.py` must move to that dependency.
- The tests create events with ledger entries only (`flag_event` in `tests/api/conftest.py`).
  Once `events.exists` reads the event table, those tests also need an event row, or they get
  404.
- The tests compare exact error messages (decision 37; `tests/api/conftest.py`). If Part 1's
  messages differ, update those constants, and nothing else.

### 3.2 Calling record_view from GET /v1/events/{id}

Do it in the same transaction as the request (spec, 'GET /v1/events/{event_id}'). The order
below is the one the test route uses (`tests/api/g2_routes.py`):

```python
@router.get("/v1/events/{event_id}")
def get_event(event_id: str, principal: Reader, session: DbSession) -> ...:
    if not events.exists(session, event_id):  # unknown id: 404, nothing written
        raise not_found()
    body = ...  # build the event response
    audit.record_view(session, principal, event_id)  # last write: takes the ledger lock
    session.commit()  # commit BEFORE returning
    return body
```

- Auth (REVIEWER or VIEWER) runs first. A 403 returns before anything is written (addendum rule
  9).
- An unknown id gives 404 NOT_FOUND and no ledger entry (spec, 'GET /v1/events/{event_id}';
  `tests/api/test_audit.py::test_unknown_event_is_404_and_writes_nothing`).

### 3.3 Commit before return

Every handler that appends commits explicitly before it returns, and never in a dependency's
teardown or a background task (decision 25). Teardown can run after the response is sent, so
the next request could miss the previous view. The read-only export and verify handlers commit
too, under the same rule (decision 40). The guard test
`tests/api/test_audit.py::test_each_audit_view_is_committed_before_the_response` catches a late
commit, and must not be deleted or weakened (decisions 43 and 49).

### 3.4 What the ingest transaction appends, and in what order

From spec, 'POST /v1/metrics (exact algorithm)':

1. Step 6: `gaps.close_if_open(session, system_id)` (Part 3). If the system had an open gap,
   it appends GAP_CLOSED.
2. Step 11 (breach with no open series): `ledger.append(session, "EVENT_FLAGGED", ...)`, then
   `notifications.enqueue(session, event_id)` (Part 3), which appends NOTIFICATION_REQUESTED.
3. Nothing else is appended: duplicates (step 5), no breach (step 9) and series joins (step 10)
   write no ledger entry.

So one ingest appends at most GAP_CLOSED, EVENT_FLAGGED and NOTIFICATION_REQUESTED, in that
order, and commits them with the sample and the event.

- `observed_value` and `threshold_value` must be plain decimal strings (section 2.4).
  **ASSUMPTION:** parse the request's `value` as a Decimal, not a float, so that
  `format(value, 'f')` gives `"0.34"` and not a float's binary expansion.
- Because GAP_CLOSED is appended at step 6, the ledger lock is held for the rest of the ingest
  transaction (steps 7 to 11). This follows from the spec's order (section 9).

### 3.5 Roles

The three Part 2 endpoints and `GET /v1/events/{event_id}` accept REVIEWER or VIEWER. A SYSTEM
key gets 403 FORBIDDEN_ROLE and no key gets 401 MISSING_OR_INVALID_KEY (spec, 'Shared rules
(Pinned)'; decision 29; `tests/api/test_errors_roles.py`).

### 3.6 Wrong method is 404 (confirm)

A wrong HTTP method on a known route returns 404 NOT_FOUND with the contract body, because the
spec has no code for 405 (decision 27;
`tests/api/test_errors_roles.py::test_wrong_method_is_404_not_found`). Part 1 must confirm this.

### 3.7 Reading first_mismatch

- `first_mismatch` is a **string** (the failing entry's stored seq), or null when valid
  (decisions 14 and 23; addendum rule 6). Do not convert it to a number: the endpoint returns
  plain JSON with no response model, for this reason (decision 39).
- Entries are checked in ascending seq: seq, then prev_hash, then the recomputed hash, stopping
  at the first failure. `entries_checked` counts the entries examined, the bad one included
  (decision 13).
- A deleted middle row reports the seq of the row after it, and a deleted first row reports
  the first remaining seq (addendum rule 11). If row k's seq is edited to k+100, verify reports
  `"k+1"`; if the last row N is edited to N+100, it reports `"N+100"` (decision 23).
- Which check failed is logged on the `ledger` logger, never returned (decisions 14 and 21).

### 3.8 Checks to run after swapping each stub

Commands are listed in section 8, Part 1.

### 3.9 What Part 1 provides, and what changed

- `auth.require(*roles)` returns `Principal(role, actor_id, system_id)` from a real
  `Authorization: Bearer <key>` header: 401, then 403 (decision 123). Tests that choose the
  caller still override `app.auth.resolve_principal` (section 3.1); tests that need a real key
  use `bearer(actor)` from `tests/db_helpers.py` after the seed or `install_keys`.
- `events.exists` and the real `GET /v1/events/{event_id}` read the `event` table, and the
  detail is the contract Event object. Tests that build the ledger by hand also insert the
  event row (`insert_event` in `tests/db_helpers.py`).
- The ingest appends at most GAP_CLOSED, EVENT_FLAGGED and NOTIFICATION_REQUESTED, in that
  order (section 3.4), and locks the open breach series before `gaps.close_if_open`
  (decision 122).
- Wrong method on a known route stays 404 NOT_FOUND (decision 27, confirmed in decision 131).

What changed from atharej's first Part 1 (`5310ae8`), and why, is the table "What changed in
Part 1 and why" in [docs/part1_api.md](docs/part1_api.md).

## 4. Part 3 (worker owner)

### 4.1 Payloads

Keys from spec, 'Entry types and payloads'. Types follow section 2.4: integers stay integers,
non-integer numbers become plain decimal strings, datetimes become strings, no floats.

| entry_type | Payload |
|---|---|
| GAP_OPENED | `{"event_id": str, "system_id": str, "last_received_at": str}`. The datetime is a string in the API format `YYYY-MM-DDTHH:MM:SSZ`, fraction only when present (addendum rule 10; decision 84). |
| GAP_CLOSED | `{"event_id": str, "system_id": str, "gap_seconds": int}`. Whole seconds, rounded down, from the gap's `received_at` to the closing moment (spec 3.4; decision 101). |
| NOTIFICATION_REQUESTED | `{"notification_id": str, "event_id": str, "notification_type": "flag_notification"}`. Gap events use the same type (spec, '3.5 Notifications (REQ-04)'). |
| NOTIFICATION_DELIVERED | `{"notification_id": str, "webhook_id": str, "attempt": int, "latency_seconds": int, "within_bound": bool}`. latency_seconds is an integer (addendum rule 2), from the event's `received_at` (spec 3.5; decision 94). |
| NOTIFICATION_FAILED | `{"notification_id": str, "webhook_id": str, "attempts": 6, "last_status": int or null}`. last_status is the last HTTP status as an integer, or null when there was no response; the `notification_delivery.last_status` column holds the same value as text (decision 96). |
| WEBHOOK_REGISTERED | `{"webhook_id": str, "url": str, "event_types": [str, ...]}` |

`within_bound` must be a JSON boolean, never the string "true". It is
`latency_seconds <= NOTIFY_MAX_DELAY_SECONDS` (900; spec 3.5 and 'Configuration (environment
variables)'; decision 94). The exact key sets are pinned by
`tests/worker/test_gaps.py::test_gap_ledger_entries_have_exact_payload_keys_actor_and_subject`,
`tests/worker/test_notifications.py::test_notification_ledger_entries_have_exact_payload_keys`
and `tests/worker/test_webhooks.py::test_register_pings_then_stores_and_appends`.

Subjects and actors are in section 2.5. Entries about an event use the event id as subject_id,
so they appear in that event's audit trail.

### 4.2 Connection role

Append as the application role (DATABASE_URL), on a session at READ COMMITTED (spec, 'Schema and
migrations': the application role has INSERT and SELECT only on the ledger; addendum rule 5;
decision 26). The owner role is only for migrations and test reset (`app/config.py`).

### 4.3 Delivery results go in a new, short transaction

Never deliver a webhook inside a transaction that has appended (spec, 'Transactions and lock
order (Pinned)'). The delivery pass (spec 3.5; decision 94) works in these steps:

1. Route each notification that has no delivery rows yet to the webhooks subscribed to its
   type, and commit. With no such webhook it waits for the next pass.
2. Claim due PENDING rows (`FOR UPDATE SKIP LOCKED`, spec, 'Pinned technology'), set
   `lease_until` and count the attempt, and commit.
3. Make the HTTP call with no transaction open.
4. In a new, short transaction: update the delivery row, append NOTIFICATION_DELIVERED or
   NOTIFICATION_FAILED last, and commit.

The sweeper's order is: lock the system row, create the gap event, append GAP_OPENED, then call
`notifications.enqueue`, which appends NOTIFICATION_REQUESTED, then commit (spec, '3.4 Gap
sweeper (REQ-02)').

### 4.4 Webhook URLs are visible to every VIEWER

`GET /v1/ledger/export` returns every entry, WEBHOOK_REGISTERED payloads included, to any
REVIEWER or VIEWER (decision 32; `docs/decisions.md`, "M4 notes"). Recommended: avoid secrets
in webhook URLs; the export shows every URL to any VIEWER, and the ledger cannot remove it
later, because entries are never updated or deleted.

This is advice, not a rule the API enforces. `POST /v1/webhooks` accepts any http or https URL,
including one with a query string, a user name and password, or a fragment, and stores it as
received (spec 3.6; decision 98). Outside the test environment it accepts https only and
refuses private and loopback addresses (decision 99).

Logs never carry webhook URLs: the HTTP client's loggers are held at WARNING (decision 106).

### 4.5 How a long-held lock affects the worker

Every append waits for the one ledger lock (section 2.2). While any transaction that has
appended stays open, the worker's appends wait, with no timeout in the ledger code (decision
10), so gap events and delivery results stall. The reverse also holds: a worker transaction that
appends and then does slow work blocks every event view and audit call. Keep worker transactions
that append short. Once Part 4 sets `lock_timeout`, a wait that runs out raises
`OperationalError` (psycopg `LockNotAvailable`, SQLSTATE 55P03); this was checked in the sandbox
against the ledger's lock key. The worker rolls it back, logs the type name, and the delivery
is claimed again when its lease runs out
(`tests/worker/test_notifications.py::test_a_lock_timeout_while_recording_is_rolled_back_and_retried`;
decision 94).

### 4.6 What Part 3 provides (implemented)

Part 3 is in this repository and follows spec sections 3.1 to 3.8 (README section 8; decisions
93 to 105, which supersede parts of decisions 81 to 89). The clause-by-clause check is
`docs/part3_spec_audit.md`. The Part 3 **ASSUMPTION**s above are settled as written there:
every worker entry, GAP_CLOSED and NOTIFICATION_REQUESTED included, uses actor `system`;
WEBHOOK_REGISTERED uses the registering principal; `gap_seconds` and `latency_seconds` are
integers; `within_bound` is `latency_seconds <= NOTIFY_MAX_DELAY_SECONDS`; `last_status` in the
payload is an integer or null; and a lock timeout while recording a delivery is rolled back and
retried.

The interfaces are the spec's pinned signatures ('Shared interfaces between parts (Pinned)').
`session` is a SQLAlchemy ORM `Session` at READ COMMITTED, and `engine` a SQLAlchemy `Engine`
for the application role.

```python
from app import gaps, notifications, worker

# Part 1's ingest, inside its transaction; neither commits.
gaps.close_if_open(session, system_id) -> str | None   # step 6: GAP_CLOSED if a gap was open
notifications.enqueue(session, event_id, notification_type="flag_notification") -> str
                                                       # step 11, after EVENT_FLAGGED

# Part 4's clock-advance endpoint and the worker; each commits its own short transactions.
gaps.sweep(engine) -> list[str]            # new gap event ids; also writes sweeper_heartbeat
notifications.deliver_due(engine) -> int   # delivery attempts made in this pass

# Part 4's /healthz; read-only.
worker.health_values(engine) -> worker.HealthValues
#   .sweeper_last_ran_seconds_ago: int | None    (real time)
#   .pending_deliveries: int
#   .oldest_pending_delivery_seconds: int | None (clock.now())
notifications.late_delivery_count() -> int  # late deliveries counted by this process
```

For Part 1 (ingest and events):

- Lock the `monitored_system` row before step 6, as spec 3.4 says. `close_if_open` locks it
  again, which is a no-op, so the lock order holds either way.
- Set `monitored_system.last_received_at` to `clock.now()` for every accepted sample,
  duplicates included (spec 3.4 step 2). The sweeper measures silence from it.
- The gap sweeper inserts `monitoring_gap` events and their four NOT_APPLICABLE `harm_field`
  rows into Part 1's `event` and `harm_field` tables, using the spec 1.7 column names, and
  `close_if_open` moves them to CLOSED. Part 1's real 0003 migration must keep those names,
  the state check, and the partial unique index on `event(system_id)` for open gaps
  (section 10).
- `app.formats.retain_until(detected_at)` computes "six calendar months, at least 183 days"
  (decision 102); breach events can use it.
- `GET /v1/events?type=monitoring_gap` reads the event table. Gaps appear without any ingest.
- Mount `app.api.webhook_routes.router` in the real app.

For Part 4 (platform):

- Replace the 0001 stub with the real migration. It must keep `monitored_system` and
  `sweeper_heartbeat` as described in section 10, and grant the application role normal read
  and write on them. Seed or reset sets `last_received_at` to the new current time (spec 4.4
  and 4.6).
- Test reset truncates, as the owner, every table the worker uses: `notification_delivery`,
  `notification`, `webhook`, `harm_field` and `event`, as spec 4.6 lists, along with the
  ledger and Part 1's other tables. The worker tolerates this while it runs (decision 104).
- `POST /v1/test/clock/advance` calls `gaps.sweep(engine)` once and then
  `notifications.deliver_due(engine)` once (spec 4.6). Row claiming makes this safe while the
  worker runs.
- `/healthz` reads `worker.health_values(engine)` (decision 97).
- Run `python -m app.worker` as its own process, as the application role, with `ENVIRONMENT`
  set (decision 100). Add the `ensure_seed` call at its start-up (decision 89).
- The worker reads `clock.now()` for every decision and `clock.wall_now()` only for the
  heartbeat. The real clock module must provide both (section 10).

## 5. Part 4 (platform owner)

### 5.1 Roles

| Role (test name) | URL | Ledger privileges |
|---|---|---|
| `monitor_owner` | `DATABASE_ADMIN_URL` | Owner. Migrations and test-only reset (`app/config.py`; `migrations/env.py`). |
| `monitor_app` | `DATABASE_URL` | INSERT and SELECT only (`migrations/versions/0002_ledger.py`; `tests/test_schema_permissions.py::test_app_role_has_insert_and_select_only`). |

- The names come from the stub `tests/sql/bootstrap_roles.sql`. Part 4 may choose other names:
  migration 0002 reads the application role from the user name in DATABASE_URL.
- Neither role may be a superuser, because a superuser bypasses the grants; the test suite
  refuses to run if one is (`tests/conftest.py`).

### 5.2 Migrations 0001 and 0002

- Part 4 owns migration 0001. Here it is a stub with revision id `"0001"`
  (`migrations/versions/0001_platform_stub.py`). It gives 0002 its parent and creates only the
  Part 4 tables Parts 1 and 3 use: `monitored_system`, `sweeper_heartbeat`, `api_key` and
  `threshold_version` (section 10; decisions 95 and 127). The chain is 0001, 0002, 0003 (Part
  1's Detection) and 0004 (Part 3's Notifications), with head `0004` (spec 4.4).
- 0002 expects:
  - revision `"0001"` as its parent;
  - both roles to exist already;
  - the application role to have USAGE on schema `public` (the stub grants it in
    `tests/sql/bootstrap_roles.sql`);
  - migrations to run as the owner role (`migrations/env.py` uses DATABASE_ADMIN_URL).
- 0002 grants SELECT and INSERT on `ledger_entry` to the user named in DATABASE_URL, revokes
  everything from PUBLIC, and refuses to run if that user is not a plain lowercase role name or
  is the role running the migration (`migrations/versions/0002_ledger.py`). So DATABASE_URL must
  be set when migrations run (`app/config.py`).
- **ASSUMPTION:** the spec puts the database roles in migration 0001 (spec, 'Schema and
  migrations'). Creating roles needs CREATEROLE, which the stub owner role does not have
  (`tests/sql/bootstrap_roles.sql`). Part 4 decides who creates them (section 9).

### 5.3 Trigger, and no TRUNCATE trigger

- `ledger_entry_block_update_delete` is `BEFORE UPDATE OR DELETE ... FOR EACH STATEMENT`. It
  calls `ledger_entry_block_update_delete_fn()`, which raises for every role, the owner included
  (`migrations/versions/0002_ledger.py`;
  `tests/test_schema_permissions.py::test_owner_update_and_delete_fail_while_trigger_enabled`).
  It stays enabled in production (addendum rule 1).
- There is deliberately no TRUNCATE trigger, because test reset truncates the ledger as the
  owner (addendum rule 1;
  `tests/test_schema_permissions.py::test_trigger_definition_blocks_update_and_delete_but_not_truncate`).
- Tamper tests disable the trigger, edit and re-enable it in one owner transaction
  (`tests/db_helpers.py`).

### 5.4 Test reset and startup seeding

`POST /v1/test/reset` (ENVIRONMENT=test only; spec, 'POST /v1/test/reset'):

1. As the owner role (DATABASE_ADMIN_URL): `TRUNCATE` the ledger along with the other tables,
   and commit. The owner can truncate; the application role cannot
   (`tests/test_schema_permissions.py::test_owner_can_truncate`,
   `tests/test_schema_permissions.py::test_app_role_cannot_update_delete_or_truncate`).
2. Then `ledger.ensure_seed(session, payload)` on an application-role session at READ
   COMMITTED, and commit. The chain restarts at seq 1 (addendum rule 4).

The tests do the same in two steps: `empty_ledger` in `tests/conftest.py` truncates as the
owner, and `seed` in `tests/api/conftest.py` calls `ensure_seed` as the application role.
**ASSUMPTION:** the SEED payload is `{"systems": ["sys-42"], "threshold_count": "2"}`, as in
addendum rule 7 and the tests (`tests/api/conftest.py`).

At startup, call `ensure_seed` once in each process that starts (addendum rule 4). It appends
SEED only when the ledger is empty, so a routine restart appends nothing, and concurrent starts
give exactly one SEED (`tests/test_append.py::test_ensure_seed_on_non_empty_ledger_appends_nothing`,
`tests/test_append.py::test_twenty_concurrent_ensure_seed_calls_give_one_seed`). Reset is not
safe during live traffic (spec, 'POST /v1/test/reset').

### 5.5 lock_timeout or statement_timeout

Set one at connection level, because the ledger has none (decision 10). `lock_timeout` covers
the ledger's advisory lock: in the sandbox, a second transaction with `lock_timeout = '500ms'`
failed after 0.5 s with SQLSTATE 55P03 while another held key 1279607879. Two ways to set it
(the value is Part 4's choice):

```sql
ALTER ROLE monitor_app SET lock_timeout = '5s';
```

```python
create_engine(url, connect_args={"options": "-c lock_timeout=5000"})
```

In the API, the resulting error becomes 500 INTERNAL_ERROR like any other exception (decision
36).

### 5.6 Pool settings and max_connections

- The application engine uses `pool_size=10`, `max_overflow=40` and `pool_pre_ping=True`
  (`app/db.py`; decision 38). That is up to 50 connections per web process, one for each of the
  40 requests FastAPI's default thread pool runs at once, plus headroom.
- Size PostgreSQL's `max_connections` above this total:
  (web processes × 50) + the worker's connections + owner connections for migrations and reset
  + `superuser_reserved_connections`.
- The postgres:16 image defaults to `max_connections = 100` and
  `superuser_reserved_connections = 3` (checked in the test container). So two web processes at
  full load would already exhaust it.
- A request waiting for the ledger lock holds its connection the whole time, so a stuck lock can
  exhaust the pool as well as block appends (sections 2.7 and 5.5).

### 5.7 uvicorn is not a dependency

`pyproject.toml` has no ASGI server. uvicorn 0.30.6 was installed outside the project for the
real-server check only (decision 50). Part 4 adds the server dependency.

### 5.8 The clock stub

`app/clock.py` returns `datetime.now(UTC)`. Part 4 replaces it with real time plus
`clock_state.offset_seconds` in the test environment (spec, 'Clock (Pinned)').

- `clock.now()` must return a timezone-aware datetime. A naive one makes `append` raise
  LedgerError (decision 7).
- The stub also has `clock.wall_now()`, real UTC time with no offset, which the worker uses
  only for `sweeper_heartbeat` (spec 3.3; decision 101). The real module must keep it.
- `tests/worker/test_clock_rule.py::test_no_direct_system_time_calls_outside_the_clock_module`
  is the grep test of spec 4.5: no file outside `app/clock.py` reads the system time.
- Callers use `clock.now()` through the module, so tests can patch `app.clock.now`
  (`tests/test_append.py::test_recorded_at_comes_from_clock`,
  `tests/api/test_audit.py::test_view_timestamp_is_the_overridden_clock`).
- `append` calls `clock.now()` after it takes the ledger lock (`app/ledger/service.py`).
  **ASSUMPTION:** if the real `now()` reads `clock_state` on its own connection, every append
  uses a second connection while holding the lock (section 9).

### 5.9 Loggers

- `ledger` (`app/ledger/verify.py`): INFO `ledger verify: valid=... entries_checked=...`, and
  on a mismatch, WARNING `ledger verify mismatch: seq=N check=NAME`.
- `api` (`app/errors.py`): ERROR with an exception's type name only.
- Tests read the check name from the WARNING text, so the format must not change (decisions 21
  and 60).
- Neither logger ever writes a payload value or a stored hash (decisions 14 and 28).
- **ASSUMPTION:** Part 4's logging configuration keeps both loggers propagating at WARNING or
  lower.

### 5.10 Running the tests

Setup and commands are in `README.md`. In short:

```sh
# Docker container (preferred)
docker run -d --name ledger-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
psql "postgresql://postgres:postgres@localhost:5432/postgres" -f tests/sql/bootstrap_roles.sql
.venv/bin/pytest            # defaults to localhost:5432 (tests/conftest.py)

# Native PostgreSQL 16 on port 5433
export DATABASE_URL=postgresql+psycopg://monitor_app:monitor_app@localhost:5433/monitor_test
export DATABASE_ADMIN_URL=postgresql+psycopg://monitor_owner:monitor_owner@localhost:5433/monitor_test
.venv/bin/pytest

.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

Each run rebuilds the schema (`alembic downgrade base`, then `upgrade head`) as the owner
(`tests/conftest.py`). A database last built from the `development-gap-sweeper` branch is at
an older revision `0003` that no longer exists, so the downgrade fails; drop and recreate it
once (README section 5.5).

**Docker daemon restart** (decision 12): in the sandbox the daemon sometimes stops between
sessions, and tests then fail with `OperationalError` (connection refused). Restart the daemon,
then the container:

```sh
pgrep dockerd >/dev/null || (sudo nohup dockerd > /tmp/dockerd.log 2>&1 &)
sleep 4
docker start ledger-pg
```

### 5.11 Phase 5 work

- **Nightly verification** (INV-7) is Phase 5 (addendum rule 9).
- **External storage of the chain head** is also Phase 5. The chain alone cannot detect removal
  of entries from the end (addendum rule 12; decision 16). `head_seq` and `head_hash` are not
  in the verify response (decision 33).

### 5.12 Part 4 hand-off from the Part 1 integration

1. **Logging.** Keep the `httpx` and `httpcore` loggers at WARNING or higher. httpx logs each
   request's full URL at INFO, and a webhook URL can carry a secret; `app/webhooks.py` holds
   both at WARNING (decision 106), and Part 4's logging configuration must not lower them. Do not turn on
   PostgreSQL statement logging (`log_statement`, `log_min_duration_statement` with
   parameters) and do not set SQLAlchemy `echo`: both would write key digests, payloads and
   webhook URLs to the logs. The `api.request` line format is decision 126. `create_app` attaches its own stderr handler to the `api` logger (decision 132); if Part 4's logging configuration adds a root handler, remove that one so lines are not written twice.
2. **Body size limit.** Nothing in the app limits the request body. Set one at the load
   balancer or server (Uvicorn has none by default), for example 64 KiB for every route.
3. **lock_timeout or statement_timeout** at connection level (section 5.5). A timeout inside
   the ingest becomes 500 INTERNAL_ERROR and rolls everything back.
4. **The clock.** The request log's `ts` and every ingest call `clock.now()`. With the real
   test-environment clock, which reads `clock_state` on each call (spec 4.5), each request
   reads it a few times more.
5. **Seed and reset.** Replace `tools/seed_part1.py` with the startup seed and
   `POST /v1/test/reset` (spec 4.6). Reset must also truncate metric_sample, breach_series,
   event, harm_field, api_key and threshold_version, and reseed the keys from the
   environment. Then replace the PENDING steps in `tests/part1/test_smoke.py` with the real
   endpoints.
6. **Merge style.** Bring this branch into main with a merge commit, not a squash or a
   rebase, so atharej's Part 1 commit `5310ae8` and the Part 2 and Part 3 history stay in it.

## 6. The standalone verifier

### Usage

```sh
python3 tools/verify_ledger.py EXPORT_FILE
```

EXPORT_FILE is the body of `GET /v1/ledger/export`, saved unchanged. The script uses only the
Python standard library and imports nothing from `app/` (decisions 55 and 61). It is written
for Python 3.9 or newer, but tested on Python 3.12 only (decision 61). It also runs in isolated
mode (`python3 -I -S`) from any directory (`tests/test_verify_script.py`).

### Output and exit codes

| Case | stdout (exactly one line) | stderr | Exit |
|---|---|---|---|
| Valid | `{"valid": true, "entries_checked": N, "first_mismatch": null}` | nothing | 0 |
| Empty array `[]` | `{"valid": true, "entries_checked": 0, "first_mismatch": null}` | nothing | 0 |
| Mismatch | `{"valid": false, "entries_checked": K, "first_mismatch": "SEQ"}` | `ledger verify mismatch: seq=SEQ check=NAME` | 1 |
| Unusable file: missing, unreadable, not UTF-8, not JSON, not an array, or an element that is not an object | `{"valid": false, "entries_checked": 0, "first_mismatch": null}` | `unusable input: REASON` | 1 |
| Not exactly one argument | nothing | `usage: verify_ledger.py EXPORT_FILE` | 2 |

Sources: decisions 52, 53, 54 and 60. NAME is `seq`, `prev_hash`, `hash` or
`uncanonicalizable`, the same names the server logs (decision 60). Neither stream ever carries a
payload value or a stored hash (decision 60).

### The checks

These match the server (decisions 13, 18, 23, 24 and 58):

- The script sorts entries by stored seq.
- For each entry, it checks in order:
  1. seq: an integer; the first is 1 and each later one is the previous plus 1.
  2. prev_hash: equal to the previous entry's hash (64 zeros for the first).
  3. hash: the hash recomputed from the six content fields and the **stored** prev_hash equals
     the stored hash. In the script this is `hash_check(row)`, which receives only the row, so
     it cannot use any other row's hash (decision 70).
- It stops at the first failure.
- An entry that cannot be canonicalized fails check 3 as `uncanonicalizable`. That covers a
  missing content field, a non-integer number, NaN or Infinity, a string with U+0000 or a lone
  surrogate, or a wrong type or format (decisions 56 and 57).
- Unknown extra keys are ignored. An explicit `"subject_id": null` is valid.

### What it will not do

- It does not detect removal of entries from the end of the ledger, or a forged last entry with
  a consistent hash (decision 16).
- It does not check payload keys per entry type (decision 11).
- It does not promise the exact first_mismatch string for an entry with a missing, boolean or
  non-integer seq. It only promises to report invalid and not crash (decisions 19 and 59).
- It does not contact the server or the database, and it does not repair anything.
- The JSON parser takes the last value for a duplicated key, and refuses integers with more than
  4300 digits, which it reports as unusable input (`tools/verify_ledger.py`).

### How another team re-verifies an export

1. Call `GET /v1/ledger/export` with a REVIEWER or VIEWER key and save the body byte for byte.
2. Run `python3 tools/verify_ledger.py export.json`. The result should equal
   `GET /v1/ledger/verify` taken at the same time (`tests/api/test_cross_verify.py`).
3. To demonstrate tamper detection, change one character in one entry's payload in the file
   and run the script again. It names that entry's seq (spec, 'Standalone verifier
   (deliverable)'; addendum rule 8).

To reimplement the check in another language, use this hash definition (spec, 'Ledger entry
format'; addendum rule 7; `app/ledger/canonical.py`):

```
canonical = JSON of {seq, entry_type, actor_id, subject_id, payload, recorded_at},
            keys sorted at every level, no whitespace (separators "," and ":"),
            non-ASCII characters written as themselves (not \u escapes), integers as integers
hash      = lowercase hex SHA-256 of the UTF-8 bytes of (prev_hash + canonical)
```

Entry 1's prev_hash is 64 zeros. Worked example (addendum rule 7; the same three entries are in
`tests/fixtures/fixture_export.json` and are checked by `tests/test_verify_script.py`):

Entry 1 (prev_hash = 64 zeros):

```
{"actor_id":"system","entry_type":"SEED","payload":{"systems":["sys-42"],"threshold_count":"2"},"recorded_at":"2026-09-27T10:00:00.000Z","seq":1,"subject_id":null}
hash = 3e17607bd7884dfbaff84c7a4a5a4762d56f329c1f63a91420831a1078db080c
```

Entry 2 (prev_hash = entry 1's hash):

```
{"actor_id":"sys-42","entry_type":"EVENT_FLAGGED","payload":{"event_id":"evt-7f3a","metric_name":"accuracy_drift","observed_value":"0.34","system_id":"sys-42","threshold_value":"0.30","threshold_version_id":"thr-1"},"recorded_at":"2026-09-27T10:05:00.250Z","seq":2,"subject_id":"evt-7f3a"}
hash = e6ba0c725b25a5aa1c3f40d9c26e1823c01798d77f7ad5ed63be90894d0ad92c
```

Entry 3 (prev_hash = entry 2's hash):

```
{"actor_id":"viewer-1","entry_type":"EVENT_VIEWED","payload":{},"recorded_at":"2026-09-27T10:06:00.000Z","seq":3,"subject_id":"evt-7f3a"}
hash = d557f21d771adea81d93c2aab306f00d61898207c26d1f8bc767a828efa6a5b2
```

Running the script on that fixture prints
`{"valid": true, "entries_checked": 3, "first_mismatch": null}`. Changing `"0.34"` to `"0.35"` in
entry 2 prints `{"valid": false, "entries_checked": 2, "first_mismatch": "2"}`
(`tests/test_verify_script.py`).

## 7. Known limits

| Limit | Source |
|---|---|
| Removing entries from the end of the ledger, or forging the last entry with a consistent hash, is not detected by the chain alone until Phase 5. | Decision 16; addendum rule 12 |
| A direct edit to Part 1's event table is not covered by the chain. Only ledger entries are. | Addendum rule 12 (no decision number) |
| Integers above 2**53 are accepted, and a JavaScript re-verifier loses precision on them. | Decision 6 |
| Verify, in the server and in the script, reads every entry into memory. This is fine for Phase 3. | Decision 20 |
| Export shows every entry to any VIEWER, webhook URLs included. | Decision 32; `docs/decisions.md`, "M4 notes" |
| The single global lock serializes every append, so every event view and audit call waits behind every other append. | Decision 10; `docs/decisions.md`, "M4 notes" |

Verify and export take no lock and never wait for appends (decisions 17 and 32).

## 8. Integration checklists

Run all commands from the repository root with the environment from section 5.10. "Full suite"
means `.venv/bin/pytest`, which must pass with no failures or errors. Today it runs 947 tests.

### Part 1

Done on `developed-part123merged`: steps 1 to 9 hold, with the full suite passing. The
in-process run of the spec section 9 smoke test is
`tests/part1/test_smoke.py::test_spec_section_9_smoke_in_process`.

1. Replace `app/errors.py`. Run `.venv/bin/pytest tests/api/test_errors_roles.py`. Expect all
   pass, with only the message constants in `tests/api/conftest.py` updated if Part 1's
   messages differ (decision 37).
2. Replace `app/auth.py`, and move the test override if needed (section 3.1). Run
   `.venv/bin/pytest tests/api/test_errors_roles.py tests/api/test_audit.py`. Expect all pass:
   401, then 403, then 404.
3. Replace `app/events.py`. Make the test fixtures create an event row (section 3.1). Run
   `.venv/bin/pytest tests/api/test_audit.py`. Expect all pass, and
   `test_unknown_event_is_404_and_writes_nothing` still writes nothing.
4. Replace `app/db.py`. Run
   `.venv/bin/pytest tests/api/test_transactions.py tests/api/test_audit.py`. Expect the session
   to be `monitor_app` at `read committed`, and the guard
   `test_each_audit_view_is_committed_before_the_response` to pass.
5. Replace `app/main.py`. Run
   `.venv/bin/pytest tests/api/test_errors_roles.py::test_the_real_app_does_not_mount_the_test_only_routes`.
   Expect pass. Then call `GET /v1/ledger/verify` on the real app. Expect 200 and three keys.
6. Wire `audit.record_view` into the real `GET /v1/events/{event_id}` (section 3.2). Then:
   - as viewer-1, GET the event;
   - as rev-1, GET the audit.
   Expect EVENT_VIEWED with `actor_id` `viewer-1` (spec, 'Tests you write', AC-15). An unknown
   id gives 404, and the ledger count is unchanged.
7. Ingest one breaching sample (accuracy_drift 0.34 for sys-42). Then GET the export. Expect
   EVENT_FLAGGED then NOTIFICATION_REQUESTED, with `"observed_value": "0.34"` as a string.
   `GET /v1/ledger/verify` returns valid.
8. Confirm the wrong-method 404 (decision 27). For example, `POST /v1/ledger/verify` should
   give 404 NOT_FOUND.
9. Run the full suite. Expect it all to pass.

### Part 3

Part 3 is in this repository; these checks are for after Parts 1 and 4 replace their stubs
(section 10).

1. Run `.venv/bin/pytest tests/worker`. Expect all pass against the real 0001 and 0003
   migrations.
2. With the real clock, `POST /v1/test/clock/advance` by 900 s with no samples. Expect a
   `monitoring_gap` event in `GET /v1/events`, GAP_OPENED then NOTIFICATION_REQUESTED in the
   export with the gap event id as subject_id, and `GET /v1/events/{id}/audit` listing both.
   At 600 s and 899 s, expect nothing (AC-02).
3. Send a sample after the gap. Expect the event CLOSED and GAP_CLOSED with an integer
   `gap_seconds` from the gap's `received_at`, in the same transaction as the ingest.
4. Register a webhook, then flag an event. Expect the spec 3.5 body at the receiver and
   NOTIFICATION_DELIVERED with an integer `latency_seconds` and a boolean `within_bound`, with
   no transaction that has appended open during the HTTP call (section 4.3).
5. With `ENVIRONMENT` other than `test`, register `https://127.0.0.1/` and
   `http://example.com/`. Expect 400 MALFORMED_BODY for both, and no ping (decision 98).
6. Hold an appending transaction open in a test, and run a delivery pass with `lock_timeout`
   set. Expect a 55P03 error that is rolled back and retried, not a crash (section 4.5).
7. Save the export to a file and run `python3 tools/verify_ledger.py export.json`. Expect exit
   0 and `"valid": true`.
8. Run the full suite. Expect it all to pass.

### Part 4

1. Create the roles and migration 0001. Run `.venv/bin/alembic downgrade base` then
   `.venv/bin/alembic upgrade head` as the owner. Expect it to end at `0004 (head)`.
   Run `.venv/bin/pytest tests/worker/test_schema.py`. Expect all pass, after adding the
   new Part 4 tables to its expected table list.
2. Run `.venv/bin/pytest tests/test_schema_permissions.py`. Expect all pass: the application
   role has INSERT and SELECT only, the trigger is enabled, and the owner can TRUNCATE.
3. Implement `POST /v1/test/reset`. Then call `GET /v1/ledger/verify`. Expect
   `{"valid": true, "entries_checked": 1, "first_mismatch": null}` (spec, 'Stubs so all four
   can work in parallel').
4. Restart the web and worker processes together. Expect still exactly one SEED.
   Call `/healthz`. Expect `sweeper_last_ran_seconds_ago`, `pending_deliveries` and
   `oldest_pending_delivery_seconds` from `worker.health_values` (decision 97).
5. Set `lock_timeout` (section 5.5). Then, with an appending transaction held open, call
   `GET /v1/events/{id}`. Expect 500 INTERNAL_ERROR after the timeout, not a hang.
6. Replace `app/clock.py`. Run
   `.venv/bin/pytest tests/test_append.py tests/api/test_audit.py`. Expect all pass,
   including `test_recorded_at_comes_from_clock`.
7. Check the pool arithmetic against `max_connections` (section 5.6).
8. Add the CI job. Expect the full suite to pass, plus
   `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy`.

## 9. Open items for integration

From `docs/decisions.md`, "Open items for integration" (M4 close-out):

1. A wrong HTTP method on a known route returns 404 NOT_FOUND (decision 27). Part 1 must
   confirm this.
2. The stubs to replace when Part 1 lands are listed in `docs/decisions.md`, "Stubs to replace
   when Part 1 lands", and in section 3.1 here.
3. Part 4 sets `lock_timeout` or `statement_timeout` at connection level (decision 10).
4. Real-server result (decision 50): over real HTTP under uvicorn, these all held:
   commit-before-return, concurrency, verify and export.
5. Index finding (decision 51): `(subject_id, seq)` is indexed, and the audit lookup takes about
   0.5 ms at 20,000 rows.

Raised while writing these notes. None changes Part 2 code, and each needs the named owner to
confirm:

6. Settled: the actor_id for GAP_CLOSED and NOTIFICATION_REQUESTED is `system` (spec 3.2;
   decisions 93 and 101), and for WEBHOOK_REGISTERED the registering principal (decision 103)
   (section 2.5).
7. GAP_CLOSED is appended at ingest step 6, so the ledger lock is held for the rest of the
   ingest transaction (section 3.4). Parts 1 and 3.
8. Who creates the database roles, given that migrations run as a role without CREATEROLE
   (section 5.2). Part 4.
9. Whether the real `clock.now()` uses a second connection inside `append` (section 5.8).
   Part 4.
10. Settled: the Part 2 endpoint tests keep the auth override and the exact error messages;
    the ledger-based event stub is gone and the fixtures insert event rows (section 3.9).

Raised by the Part 2 and Part 3 integration (decisions 93 to 105):

11. Settled (decision 127): Part 1's real 0003 keeps the spec 1.7 names the gap sweeper's SQL uses: the `event`
    and `harm_field` columns, `ck_event_state`, and the partial unique index on
    `event(system_id)` for open gaps. The stub's `harm_field` has no foreign key to `event`,
    because spec 1.7 names none (section 10). Part 1.
12. Part 4's real 0001 must keep `sweeper_heartbeat` to one row. The worker upserts with
    `ON CONFLICT (singleton)`, so a different single-row mechanism needs a matching change in
    `app/gaps.py` (decision 95). Part 4.
13. `oldest_pending_delivery_seconds` counts PENDING delivery rows only. A notification that is
    waiting for its first webhook has no delivery row and is not counted (decision 97). Part 4,
    for `/healthz`.
14. Outside the test environment, a refused webhook URL is 400 MALFORMED_BODY, because the
    spec names no code for it. The private-address check runs at registration, not again at
    delivery (decisions 98 and 99). All parts.
15. Settled: the two spec 3.9 rows that need Part 1's ingest, "Duplicate resets silence" and
    "Wrong sender clock", are tested through the real ingest in
    `tests/worker/test_gaps_via_ingest.py`.

Raised by the Part 1 integration (decisions 116 to 131), for the other team or Part 4:

16. Confirm with the other team (spec Appendix B): a duplicate is 200 with the original ids
    and does not close a gap (decision 116); GET /v1/events is a bare array that every role can
    read, with limit capped at 200 (decisions 117 and 118).
17. The Part 4 hand-off items in section 5.12.

## 10. Stubs to replace when Part 4 lands

Part 1's stubs are all replaced (section 3.1). What remains is Part 4's. Each stub below is
marked `STUB` in its first line. Replacing one should need no change to Part 1, 2 or 3 code, as
long as the "Keep" column holds. After each replacement, run the checks named for it, then the
full suite, `ruff check .`, `ruff format --check .` and `mypy` (section 5.10).

| File | What the stub contains | Replaced by | Keep | Re-run |
|---|---|---|---|---|
| `migrations/versions/0001_platform_stub.py` | Revision `"0001"`. `monitored_system`, `sweeper_heartbeat` (single row, decision 95), `api_key` (key_hash PK; role SYSTEM, REVIEWER or VIEWER; actor_id; system_id set exactly for SYSTEM) and `threshold_version` (spec 4.4 columns; comparator gt or lt; unique (system_id, metric_name, version)). Grants SELECT, INSERT, UPDATE, DELETE to the user in DATABASE_URL; nothing to PUBLIC | Part 4's real 0001, adding `clock_state` and the roles (spec 4.4) | Revision id `"0001"`; the four tables' names, columns and constraints; normal read and write for the application role | `alembic downgrade base` then `upgrade head` (expect `0004 (head)`); `tests/worker/test_schema.py`; `tests/part1/test_schema.py`; `tests/test_schema_permissions.py` |
| `app/clock.py` | `now()` and `wall_now()`, both real UTC time | The real clock: real time plus `clock_state.offset_seconds` in test (spec 4.5) | `now()` timezone-aware; `wall_now()` with no offset, for the heartbeat; callers patch `app.clock.now` | `tests/test_append.py`, `tests/api/test_audit.py`, `tests/worker/test_clock_rule.py`, `tests/worker/test_gaps.py::test_sweep_writes_the_wall_clock_heartbeat` |
| `app/db.py` | The application-role engine and `get_session` (Part 1 uses it as it is; decision 131) | Part 4's session settings, if any (`lock_timeout`, pool sizes) | READ COMMITTED; no commit in teardown | `tests/api/test_transactions.py`; `tests/worker/test_webhooks.py` |
| `tools/seed_part1.py` | The spec's seed fixtures from the environment, ENVIRONMENT=test only (decision 128) | Part 4's startup seed and `POST /v1/test/reset` (spec 4.6) | The fixtures exactly; keys only as digests; SEED only on an empty ledger | `tests/part1/test_seed.py`; then switch `tests/part1/conftest.py` to reset |
| `tests/part1/test_smoke.py` (steps 1 and 4) | Reset and clock advance done in process, marked PENDING | Calls to `POST /v1/test/reset` and `POST /v1/test/clock/advance` | The five steps and their expected results (spec 9) | The test itself, then Part 4's real smoke script |
| `tests/sql/bootstrap_roles.sql` | Creates `monitor_owner`, `monitor_app` and the `monitor_test` database; grants USAGE on `public` | Part 4's role setup (section 5.2) | Neither role a superuser; the app role is not the migration role | `tests/test_schema_permissions.py`; `tests/worker/test_schema.py` |
| `tests/worker/conftest.py` (`add_system`, `add_event`, `WORKER_TABLES`) | Test helpers that insert a `monitored_system` row and an `event` row as the owner | Part 4's seed and reset; Part 1's ingest is already real | The column values the tests assume (sys-42, 300 s, 3 intervals) | `tests/worker/` |

The tests in `tests/worker/test_schema.py` pin the columns exactly. When a real migration adds
columns or tables, extend `EXPECTED_COLUMNS` there with the new ones from the spec; do not
remove the ones Parts 1 and 3 use.
