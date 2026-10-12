# Integration notes: Part 2 (ledger and audit)

These notes are for the owners of Part 1 (API), Part 3 (worker) and Part 4 (platform). They say
what Part 2 provides, what every caller must do, and what each part replaces or checks when its
code lands.

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
| GAP_CLOSED | Part 3, `gaps.close_if_open`, called inside Part 1's ingest | **ASSUMPTION:** `system`; the spec does not say whether it is `system` or the ingesting principal | event id | `event_id`, `system_id`, `gap_seconds` |
| NOTIFICATION_REQUESTED | Part 3, `notifications.enqueue` (called by Part 1 for breaches, Part 3 for gaps) | **ASSUMPTION:** the caller's actor: `sys-42` in ingest, `system` in the sweeper | event id | `notification_id`, `event_id`, `notification_type` |
| NOTIFICATION_DELIVERED | Part 3, delivery pass | `system` (background work) | event id | `notification_id`, `webhook_id`, `attempt`, `latency_seconds`, `within_bound` |
| NOTIFICATION_FAILED | Part 3, delivery pass | `system` (background work) | event id | `notification_id`, `webhook_id`, `attempts`, `last_status` |
| WEBHOOK_REGISTERED | Part 3, `POST /v1/webhooks` | **ASSUMPTION:** the registering principal (`rev-1` or `viewer-1`) | webhook id | `webhook_id`, `url`, `event_types` |
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

| File | What it stubs | Replace with | Keep |
|---|---|---|---|
| `app/errors.py` | `ApiError` and the global handlers: 404 and 405 become 404 NOT_FOUND, 422 becomes 400 MALFORMED_BODY, and a middleware turns any other exception into 500 INTERNAL_ERROR (decisions 27, 28, 36, 37) | Part 1's errors module, `errors.ApiError(status, error_code, message)` plus handlers (spec, 'Shared interfaces between parts (Pinned)') | The contract error body; LedgerError becomes 500 with nothing leaked (decision 28) |
| `app/auth.py` | `resolve_principal()` (always REVIEWER rev-1) and `require(*roles)`; `Principal` (decisions 29 and 41) | Part 1's key auth | `require(*roles)` returns `Principal(role, actor_id, system_id)`; 401 before 403 before any 404 lookup (decision 29) |
| `app/events.py` | `exists(session, event_id)`: true when the ledger has an EVENT_FLAGGED or GAP_OPENED entry with that subject_id (decisions 30 and 44) | A lookup on Part 1's event table | Same signature, called after the role check |
| `app/db.py` | The engine (application role, READ COMMITTED, pool 10 plus 40, `pool_pre_ping`) and `get_session`, which rolls back on error, closes, and never commits (decisions 25, 26, 38) | Part 1 or Part 4 session handling | Application role, READ COMMITTED, one transaction per request, no commit in teardown |
| `app/main.py` | `create_app()`, which mounts only the Part 2 router and the error handlers, with `/docs`, `/redoc` and `/openapi.json` off (decision 39) | Part 1's app | It must include `app.api.ledger_routes.router` |
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
    if not events.exists(session, event_id):      # unknown id: 404, nothing written
        raise not_found()
    body = ...                                    # build the event response
    audit.record_view(session, principal, event_id)   # last write: takes the ledger lock
    session.commit()                              # commit BEFORE returning
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

## 4. Part 3 (worker owner)

### 4.1 Payloads

Keys from spec, 'Entry types and payloads'. Types follow section 2.4: integers stay integers,
non-integer numbers become plain decimal strings, datetimes become strings, no floats.

| entry_type | Payload |
|---|---|
| GAP_OPENED | `{"event_id": str, "system_id": str, "last_received_at": str}`. The datetime is a string (addendum rule 10). |
| GAP_CLOSED | `{"event_id": str, "system_id": str, "gap_seconds": int}`. **ASSUMPTION:** whole seconds as an integer; if fractional, a plain decimal string via `format(value, 'f')`. |
| NOTIFICATION_REQUESTED | `{"notification_id": str, "event_id": str, "notification_type": "flag_notification"}`. Gap events use the same type (spec, '3.5 Notifications (REQ-04)'). |
| NOTIFICATION_DELIVERED | `{"notification_id": str, "webhook_id": str, "attempt": int, "latency_seconds": int, "within_bound": bool}`. latency_seconds is an integer (addendum rule 2). |
| NOTIFICATION_FAILED | `{"notification_id": str, "webhook_id": str, "attempts": int, "last_status": ...}`. **ASSUMPTION:** last_status is the HTTP status as an integer, or null when there was no response. |
| WEBHOOK_REGISTERED | `{"webhook_id": str, "url": str, "event_types": [str, ...]}` |

`within_bound` must be a JSON boolean, never the string "true". **ASSUMPTION:** it is
`latency_seconds <= NOTIFY_MAX_DELAY_SECONDS` (900; spec, 'Configuration (environment
variables)').

Subjects and actors are in section 2.5. Entries about an event use the event id as subject_id,
so they appear in that event's audit trail.

### 4.2 Connection role

Append as the application role (DATABASE_URL), on a session at READ COMMITTED (spec, 'Schema and
migrations': the application role has INSERT and SELECT only on the ledger; addendum rule 5;
decision 26). The owner role is only for migrations and test reset (`app/config.py`).

### 4.3 Delivery results go in a new, short transaction

Never deliver a webhook inside a transaction that has appended (spec, 'Transactions and lock
order (Pinned)'). **ASSUMPTION:** the delivery pass works in three steps:

1. Claim due delivery rows (`FOR UPDATE SKIP LOCKED`, spec, 'Pinned technology') and commit.
2. Make the HTTP call with no transaction open.
3. In a new, short transaction: update the delivery row, append NOTIFICATION_DELIVERED or
   NOTIFICATION_FAILED last, and commit.

The sweeper's order is: lock the system row, create the gap event, append GAP_OPENED, then call
`notifications.enqueue`, which appends NOTIFICATION_REQUESTED, then commit (spec, '3.4 Gap
sweeper (REQ-02)').

### 4.4 Webhook URLs are visible to every VIEWER

`GET /v1/ledger/export` returns every entry, WEBHOOK_REGISTERED payloads included, to any
REVIEWER or VIEWER (decision 32; `docs/decisions.md`, "M4 notes"). A webhook URL must not
carry a secret (a token in the path or query string). The ledger cannot remove it later,
because entries are never updated or deleted.

### 4.5 How a long-held lock affects the worker

Every append waits for the one ledger lock (section 2.2). While any transaction that has
appended stays open, the worker's appends wait, with no timeout in the ledger code (decision
10), so gap events and delivery results stall. The reverse also holds: a worker transaction that
appends and then does slow work blocks every event view and audit call. Keep worker transactions
that append short. Once Part 4 sets `lock_timeout`, a wait that runs out raises
`OperationalError` (psycopg `LockNotAvailable`, SQLSTATE 55P03); this was checked in the sandbox
against the ledger's lock key. **ASSUMPTION:** the worker rolls back and retries on its next
pass.

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

- Part 4 owns migration 0001. Here it is an empty stub with revision id `"0001"`, which exists
  only so 0002 has a parent (`migrations/versions/0001_platform_stub.py`).
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
(`tests/conftest.py`).

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
     the stored hash.
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
means `.venv/bin/pytest`, which must pass with no failures or errors. Today it runs 484 tests.

### Part 1

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

1. Run the gap sweeper past the gap timeout. Expect GAP_OPENED then NOTIFICATION_REQUESTED in
   the export, with the gap event id as subject_id, and `GET /v1/events/{id}/audit` listing
   both.
2. Send a sample after the gap. Expect GAP_CLOSED with an integer `gap_seconds`, in the same
   transaction as the ingest.
3. Deliver a notification to a local receiver. Expect NOTIFICATION_DELIVERED with an integer
   `latency_seconds` and a boolean `within_bound`. Also expect that no transaction that has
   appended was open during the HTTP call (section 4.3).
4. Register a webhook. Expect WEBHOOK_REGISTERED with a URL that carries no secret (section
   4.4).
5. Hold an appending transaction open in a test, and run a delivery pass with `lock_timeout`
   set. Expect a 55P03 error that is rolled back and retried, not a crash (section 4.5).
6. Save the export to a file and run `python3 tools/verify_ledger.py export.json`. Expect exit
   0 and `"valid": true`.
7. Run the full suite. Expect it all to pass.

### Part 4

1. Create the roles and migration 0001. Run `.venv/bin/alembic downgrade base` then
   `.venv/bin/alembic upgrade head` as the owner. Expect it to end at `0002 (head)`.
2. Run `.venv/bin/pytest tests/test_schema_permissions.py`. Expect all pass: the application
   role has INSERT and SELECT only, the trigger is enabled, and the owner can TRUNCATE.
3. Implement `POST /v1/test/reset`. Then call `GET /v1/ledger/verify`. Expect
   `{"valid": true, "entries_checked": 1, "first_mismatch": null}` (spec, 'Stubs so all four
   can work in parallel').
4. Restart the web and worker processes together. Expect still exactly one SEED.
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

6. The actor_id for GAP_CLOSED, NOTIFICATION_REQUESTED and WEBHOOK_REGISTERED is not pinned by
   the spec (section 2.5). Parts 1 and 3.
7. GAP_CLOSED is appended at ingest step 6, so the ledger lock is held for the rest of the
   ingest transaction (section 3.4). Parts 1 and 3.
8. Who creates the database roles, given that migrations run as a role without CREATEROLE
   (section 5.2). Part 4.
9. Whether the real `clock.now()` uses a second connection inside `append` (section 5.8).
   Part 4.
10. The Part 2 endpoint tests depend on the auth override, the ledger-based event stub and the
    exact error messages (section 3.1). Part 1.
