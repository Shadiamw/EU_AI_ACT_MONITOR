# EU_AI_ACT_MONITOR

Compliance monitoring system implementing **Part 1: detection and ingestion (API)**,
**Part 2: ledger and audit**, and **Part 3: the worker** (gap sweeper and notifications).
Part 4 deployment, reset, and simulated-clock support remain pending.

See **[docs/part1_api.md](docs/part1_api.md)** for Part 1 endpoints, local setup,
credentials, persistence, integration details, and assumptions to confirm with the team.

Where to look:

- **This README:** what the ledger is, how to check it, how to run everything, and its known
  limits; section 8 covers the worker.
- **[integration_notes.md](integration_notes.md):** what Parts 1, 3 and 4 must do when they call
  the ledger or replace a stub.
- **[docs/decisions.md](docs/decisions.md):** every settled decision, with its reason. "Decision
  N" below means entry N there.
- **[docs/ac06_storage_evidence.md](docs/ac06_storage_evidence.md):** the storage-layer evidence
  for AC-06: the table grants and the trigger definition.

## 1. What the ledger is

The ledger is one append-only PostgreSQL table, `ledger_entry`. Every entry has eight fields:
seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash and hash. Each entry's
hash covers its own content and the previous entry's hash, so changing, removing or reordering
an entry breaks the chain, except removal from the end (section 6). Other parts write to it
only through `ledger.append`, inside their own transaction. Anyone with an export can re-check
the chain, either with `GET /v1/ledger/verify` or with the standard-library script
`tools/verify_ledger.py`, and both give the same result.

| What | Where |
|---|---|
| Canonical form and hash | `app/ledger/canonical.py` |
| `ledger.append`, `ledger.ensure_seed` and the advisory lock | `app/ledger/service.py` |
| Server-side verify | `app/ledger/verify.py` |
| Audit trail and export queries | `app/ledger/read.py` |
| `audit.record_view` | `app/audit.py` |
| `GET /v1/events/{event_id}/audit`, `GET /v1/ledger/export`, `GET /v1/ledger/verify` | `app/api/ledger_routes.py` |
| Table, grants and UPDATE/DELETE trigger | `migrations/versions/0002_ledger.py` |
| Standalone verifier | `tools/verify_ledger.py` |
| The three-entry fixture | `tests/fixtures/fixture_export.json` |
| Part 1 API, authentication and event reads | `app/ingestion.py`, `app/api/ingestion_routes.py`, `app/auth.py`, `app/events.py` |
| Remaining platform clock and baseline migration | `app/clock.py`, `migrations/versions/0001_platform_stub.py` |

## 2. Hash definition

This is the definition other teams re-verify against (spec, 'Ledger entry format'; addendum
rule 7).

```text
canonical = JSON of the six content fields {seq, entry_type, actor_id, subject_id, payload, recorded_at}
            with keys sorted at every level, separators "," and ":" (no whitespace),
            and non-ASCII characters written as themselves (ensure_ascii False)
hash      = lowercase hex SHA-256 of the UTF-8 bytes of (prev_hash + canonical)
genesis   = entry 1's prev_hash is 64 zeros: 0000000000000000000000000000000000000000000000000000000000000000
```

- **Numbers:** a payload holds integers only. Any non-integer number is stored as a plain decimal
  string, such as `"0.34"` (addendum rules 2 and 10).
- **Strings:** a string may not contain U+0000 or a lone surrogate (decision 1).
- **recorded_at:** the form is `YYYY-MM-DDTHH:MM:SS.mmmZ` in UTC (decision 7).

The same definition in Python:

```python
import hashlib
import json

CONTENT_FIELDS = ("seq", "entry_type", "actor_id", "subject_id", "payload", "recorded_at")


def entry_hash(prev_hash: str, entry: dict) -> str:
    content = {field: entry[field] for field in CONTENT_FIELDS}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
```

## 3. Worked example

These are the three entries of addendum rule 7, which are also in
`tests/fixtures/fixture_export.json`. Each entry's canonical string is hashed with the previous
entry's hash in front of it. `tests/test_readme_example.py` checks this block against hashlib,
the fixture and the verifier.

```text
entry 1
prev_hash: 0000000000000000000000000000000000000000000000000000000000000000
canonical: {"actor_id":"system","entry_type":"SEED","payload":{"systems":["sys-42"],"threshold_count":"2"},"recorded_at":"2026-09-27T10:00:00.000Z","seq":1,"subject_id":null}
hash:      3e17607bd7884dfbaff84c7a4a5a4762d56f329c1f63a91420831a1078db080c

entry 2
prev_hash: 3e17607bd7884dfbaff84c7a4a5a4762d56f329c1f63a91420831a1078db080c
canonical: {"actor_id":"sys-42","entry_type":"EVENT_FLAGGED","payload":{"event_id":"evt-7f3a","metric_name":"accuracy_drift","observed_value":"0.34","system_id":"sys-42","threshold_value":"0.30","threshold_version_id":"thr-1"},"recorded_at":"2026-09-27T10:05:00.250Z","seq":2,"subject_id":"evt-7f3a"}
hash:      e6ba0c725b25a5aa1c3f40d9c26e1823c01798d77f7ad5ed63be90894d0ad92c

entry 3
prev_hash: e6ba0c725b25a5aa1c3f40d9c26e1823c01798d77f7ad5ed63be90894d0ad92c
canonical: {"actor_id":"viewer-1","entry_type":"EVENT_VIEWED","payload":{},"recorded_at":"2026-09-27T10:06:00.000Z","seq":3,"subject_id":"evt-7f3a"}
hash:      d557f21d771adea81d93c2aab306f00d61898207c26d1f8bc767a828efa6a5b2
```

## 4. Standalone verifier

```text
usage: verify_ledger.py EXPORT_FILE
```

```sh
python3 tools/verify_ledger.py export.json
```

- **Input.** `EXPORT_FILE` is the body of `GET /v1/ledger/export`, saved unchanged.
- **Dependencies.** The script uses only the Python standard library and imports nothing from
  `app/` (decisions 55 and 61).
- **Python versions.** It is written for Python 3.9 or newer, but **tested on Python 3.12
  only** (decisions 61 and 73).

| Case | stdout (exactly one line) | stderr | Exit code |
|---|---|---|---|
| Valid | `{"valid": true, "entries_checked": N, "first_mismatch": null}` | nothing | 0 |
| Empty array `[]` | `{"valid": true, "entries_checked": 0, "first_mismatch": null}` | nothing | 0 |
| Mismatch | `{"valid": false, "entries_checked": K, "first_mismatch": "SEQ"}` | `ledger verify mismatch: seq=SEQ check=NAME` | 1 |
| Unusable file (missing, unreadable, not UTF-8, not JSON, not an array, or an element that is not an object) | `{"valid": false, "entries_checked": 0, "first_mismatch": null}` | `unusable input: REASON` | 1 |
| Not exactly one argument | nothing | the usage line above | 2 |

Sources: decisions 52, 53, 54 and 60.

- NAME is `seq`, `prev_hash`, `hash` or `uncanonicalizable`. These are the same names the
  server logs (decisions 21 and 60).
- `first_mismatch` is a string: the stored seq of the first failing entry.
- No payload value or stored hash is ever printed.

The checks are the server's (decisions 13, 18, 58 and 70):

- Entries are sorted by stored seq.
- For each entry, three checks run in order:
  1. seq: the first entry is 1, and each later entry is the previous seq plus 1.
  2. prev_hash: equals the previous entry's hash (64 zeros for the first entry).
  3. hash: the hash recomputed from the six content fields and the entry's **stored** prev_hash
     equals the stored hash.
- Verification stops at the first failure.
- An entry that cannot be canonicalized fails check 3 as `uncanonicalizable` (decisions 56 and
  57). Unknown extra keys are ignored (decision 24).

What it will not do:

- Detect removal of entries from the end of the ledger, or a forged last entry with a consistent
  hash (decision 16).
- Give an exact `first_mismatch` string for an entry whose seq is missing, a boolean or not an
  integer. It only reports invalid and never crashes (decisions 19 and 59).
- Check payload keys per entry type (decision 11).
- Contact the server or the database.

How another team re-verifies an export:

1. Call `GET /v1/ledger/export` with a REVIEWER or VIEWER key, and save the body byte for byte.
2. Run `python3 tools/verify_ledger.py export.json`. The result should equal
   `GET /v1/ledger/verify` taken at the same time.
3. To demonstrate tamper detection, change one character in one entry's payload in the file and
   run the script again. It names that entry's seq (addendum rule 8). For example, changing
   `"0.34"` to `"0.35"` in the fixture gives
   `{"valid": false, "entries_checked": 2, "first_mismatch": "2"}` and exit code 1.

[integration_notes.md](integration_notes.md) section 6 has more detail for other teams.

## 5. Setup, migrations, tests and checks

Requirements: Python 3.12 (`pyproject.toml` requires `>=3.12,<3.13`) and PostgreSQL 16. Tests
always run against a real PostgreSQL 16 server, never SQLite.

### 5.1 Python environment

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

### 5.2 PostgreSQL 16

Use either option. The test suite has been run on both.

**Option A: Docker container (preferred)**

```sh
docker run -d --name ledger-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
until docker exec ledger-pg pg_isready -h localhost -U postgres >/dev/null 2>&1; do sleep 1; done
```

On first start the container initializes its data directory before it accepts TCP connections.
The second line waits for that. Without it, the next step can fail with "server closed the
connection unexpectedly".

**Restarting the Docker daemon (decision 12).** In some sandboxes the Docker daemon stops between
sessions. Tests then fail with `OperationalError` (connection refused). Start the daemon again,
then the existing container:

```sh
pgrep dockerd >/dev/null || (sudo nohup dockerd > /tmp/dockerd.log 2>&1 &)
sleep 4
docker start ledger-pg
until docker exec ledger-pg pg_isready -h localhost -U postgres >/dev/null 2>&1; do sleep 1; done
```

**Option B: native PostgreSQL 16 server**

On Ubuntu with the `postgresql-16` package installed. `initdb` refuses to run as root, so it runs
as the `postgres` user, on port 5433 to avoid clashing with any system cluster:

```sh
PGBIN=/usr/lib/postgresql/16/bin
DATA=/var/lib/postgresql/ledger-test
sudo -u postgres sh -c "echo postgres > /var/lib/postgresql/pwfile"
sudo -u postgres $PGBIN/initdb -D $DATA -U postgres \
    --auth-local=trust --auth-host=scram-sha-256 --pwfile=/var/lib/postgresql/pwfile
sudo -u postgres $PGBIN/pg_ctl -D $DATA -o "-p 5433" -l $DATA/server.log -w start
```

With option B, use port `5433` instead of `5432` in the commands and URLs below. Stop it with
`sudo -u postgres $PGBIN/pg_ctl -D $DATA -w stop`.

### 5.3 Roles and test database (once per server)

`tests/sql/bootstrap_roles.sql` creates two roles and the `monitor_test` database. It stands in
for Part 4's migration 0001, which owns the real roles:

| Role | Used by | Ledger privileges |
|---|---|---|
| `monitor_owner` | migrations and test-only reset (`DATABASE_ADMIN_URL`) | owner |
| `monitor_app` | the application (`DATABASE_URL`) | `INSERT`, `SELECT` only |

Neither role is a superuser, because a superuser bypasses the grants under test. The test suite
refuses to run if either one is (`tests/conftest.py`).

```sh
psql "postgresql://postgres:postgres@localhost:5432/postgres" -f tests/sql/bootstrap_roles.sql
```

If `psql` is not installed on the host, run the client inside the container instead:

```sh
docker exec -i ledger-pg psql -U postgres -d postgres -f - < tests/sql/bootstrap_roles.sql
```

On a server where the script has already run, a second run prints `role "monitor_owner" already
exists` and similar errors for the other role and the database, and still exits 0. Those errors
are harmless.

### 5.4 Environment

The test suite defaults to these values (`tests/conftest.py`), so they only need setting for
another server or port. Migrations run by hand always need them:

```sh
export DATABASE_URL=postgresql+psycopg://monitor_app:monitor_app@localhost:5432/monitor_test
export DATABASE_ADMIN_URL=postgresql+psycopg://monitor_owner:monitor_owner@localhost:5432/monitor_test
export ENVIRONMENT=test
```

### 5.5 Migrations

```sh
.venv/bin/alembic upgrade head      # runs as DATABASE_ADMIN_URL
.venv/bin/alembic current           # expect: 0004 (head)
```

Migration 0002 grants ledger privileges to the user named in `DATABASE_URL`. It refuses to run
if that user is the role running the migration. Migration 0003 creates the worker's tables
(section 8). Migration 0004 extends the event table and adds metric samples, API-key digests,
and threshold versions (see docs/part1_api.md).

### 5.6 Tests and static checks

```sh
.venv/bin/pytest            # rebuilds the schema (alembic downgrade base, upgrade head) each run
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy              # strict; checks app, migrations, tests and tools
```

At the time of writing the suite collects 615 tests. All pass on the Docker setup. The 532
Part 2 tests also passed on the native setup; the 83 Part 3 tests have not been run there.

## 6. Known limits and operating notes

| Item | Decision |
|---|---|
| **Global append lock.** `append` takes one transaction-scoped advisory lock (key 1279607879) and holds it until the caller commits. So every append, including every event view (EVENT_VIEWED) and every audit call (AUDIT_VIEWED), waits for the one before it. | 10; M4 notes |
| **No lock timeout in the ledger.** Part 4 must set `lock_timeout` or `statement_timeout` at connection level. Without one, a stuck transaction blocks every append. | 10 |
| **Export shows webhook URLs.** Export returns every entry, including WEBHOOK_REGISTERED payloads, to any REVIEWER or VIEWER. Webhook URLs must not carry secrets. | 32; M4 notes |
| **Pool sizing.** The application engine uses `pool_size=10` and `max_overflow=40`, so up to 50 connections per web process. Size PostgreSQL's `max_connections` above the total across all processes. | 38 |
| **Docker daemon restart.** See section 5.2. | 12 |
| **Large integers.** Integers above 2**53 are accepted. A JavaScript re-verifier would lose precision on them. | 6 |
| **Verify memory.** Verify, in the server and in the script, reads every entry into memory. This is fine for Phase 3. | 20 |
| **End-of-ledger truncation and forged last row.** Removing entries from the end, or forging the last entry with a consistent hash, is not detected by the chain alone until Phase 5. Both are pinned by tests that expect valid. | 16 |
| **ORM Session handle.** `ledger.append`, `ledger.ensure_seed` and `audit.record_view` take a SQLAlchemy 2.x ORM `Session`, at READ COMMITTED. | 8 |
| **No per-entry-type payload shape check.** A missing or misspelled payload key is accepted. | 11 |
| **Verify check order.** seq, then prev_hash, then the recomputed hash. Verify stops at the first failure, and `entries_checked` includes the bad entry. | 13 |
| **Standalone verifier.** Usage, output, exit codes and stderr are as in section 4. | 52–54, 60 |
| **Re-verifying an export.** The hash definition and the worked example are in sections 2 and 3, and the steps in section 4. | 74; addendum rules 7 and 8 |
| **Python versions.** The script is written for 3.9 or newer and tested on 3.12 only. | 61, 73 |
| **What the script will not do.** It does not detect end-of-ledger removal, and gives no exact strings for degenerate seq values. | 16, 59 |
| **Integration hand-off.** See [integration_notes.md](integration_notes.md). | 71 |
| **Wrong method is 404.** A wrong HTTP method on a known route returns 404 NOT_FOUND, because the spec has no 405 code. Part 1 retains and tests this behavior. | 27 |
| **ASGI server.** uvicorn is included in the dev extra; run `python -m uvicorn app.main:app`. | 50 |
| **Logs.** An unexpected exception, LedgerError included, returns 500 INTERNAL_ERROR. The `api` logger records only the exception's type name, never its message or a traceback. The `ledger` logger never writes a payload value or a stored hash. | 14, 28, 36 |

## 7. Requirements and acceptance criteria

Only the ids that the spec or the addendum names for Part 2 are listed here.

| Id | Source | Where it is exercised |
|---|---|---|
| REQ-06 | Spec, Part 2 'Mission and scope' | The chain, verify and the standalone verifier |
| REQ-14 | Spec, Part 2 'Mission and scope'; spec, 'GET /v1/events/{event_id}' | `audit.record_view` and the audit endpoint |
| AC-06 | Spec, Part 2 'Mission and scope'; addendum rules 1 and 12 | Tamper tests in `tests/test_verify.py` and `tests/api/test_export_verify.py`; the storage-layer half in `docs/ac06_storage_evidence.md` |
| AC-15 | Spec, Part 2 'Mission and scope'; addendum rule 12 | `tests/api/test_audit.py`. The spec notes: "in Phase 3 only the “view” and system-written entries can be exercised; a user “edit” path arrives with disposition in Phase 4". |

## 8. Worker (Part 3): gap sweeper and notifications

The worker covers REQ-02 (a monitoring-gap event, and the reviewer notified, when a monitored
system stops sending data) and REQ-04 (the reviewer notified within 15 minutes of detection).
Run it as its own process, as the application role:

```sh
.venv/bin/python -m app.worker
```

Each loop runs the gap sweeper once a minute (wall clock), then a delivery pass every 5
seconds. Every decision about gaps, retries and latency reads `clock.now()`, so in the test
environment advancing the simulated clock takes effect at the next sweep or pass.

| What | Where |
|---|---|
| Gap sweeper, and `gaps.close_if_open` for Part 1's ingest | `app/gaps.py` |
| `notifications.enqueue` and the delivery pass | `app/notifications.py` |
| Webhook URL rules, the HTTP call and registration | `app/webhooks.py` |
| `POST /v1/webhooks` | `app/api/webhook_routes.py` |
| The worker loop | `app/worker.py` |
| Tables: monitoring_gap, webhook, notification, notification_delivery | `migrations/versions/0003_worker.py` |
| Shared event and monitored-system tables | `migrations/versions/0001_platform_stub.py`, `migrations/versions/0004_ingestion.py`, `app/events.py` |
| Tests | `tests/worker/` |

### 8.1 Behaviour

- **Gap.** A system is silent since its last sample, or since monitoring started if it has never
  sent one. Silence of at least the timeout (3 × 300 = 900 seconds by default) opens one gap:
  a FLAGGED `monitoring_gap` event, GAP_OPENED, and a `flag_notification`. A system has at most
  one open gap. Its next sample closes the gap (GAP_CLOSED, with `gap_seconds` the whole silent
  time) and moves the event to CLOSED. Gap events skip review, deadlines and reports.
- **Notification.** One delivery per webhook subscribed to the type. A 2xx answer within the
  timeout is a delivery: NOTIFICATION_DELIVERED with an integer `latency_seconds` from detection
  and a boolean `within_bound`. A failed attempt is retried after 30, 60, 120 and 240 seconds;
  the fifth failure appends NOTIFICATION_FAILED. No webhook is called while a transaction is
  open. Delivery is at least once, so a receiver can see the same `notification_id` twice.
- **Webhook registration.** `POST /v1/webhooks {url, event_types}`, REVIEWER or VIEWER. The
  URL is pinged first; 201 `{webhook_id}` on a 2xx answer, 422 `WEBHOOK_VERIFICATION_FAILED`
  otherwise, and 400 `MALFORMED_BODY` for a bad body or a URL with credentials, a query string
  or a fragment (webhook URLs are visible in the export).

### 8.2 Acceptance criteria

| Id | Requirement | Where it is exercised | Method |
|---|---|---|---|
| AC-02 | REQ-02 | `tests/worker/test_gaps.py::test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do` (600 s and 899 s: no gap; 900 s: gap); `tests/worker/test_worker_system.py::test_gap_to_delivery_to_close_and_reverify` (exactly 15 minutes withheld, then the worker delivers to a real HTTP receiver within 15 minutes of the third missed interval) | Automated. The manual check of delivery reads NOTIFICATION_DELIVERED in `GET /v1/ledger/export`, or a registered webhook receiver |
| AC-04 | REQ-04 | `tests/worker/test_notifications.py`: `test_delivery_appends_delivered_with_integer_latency_and_boolean_bound`, `test_within_bound_compares_latency_with_the_configured_bound` (900 s in bound, 901 s not), `test_ac04_the_bound_is_configurable_to_five_minutes` (300 s in bound, 301 s not) | Automated: `latency_seconds` and `within_bound` in NOTIFICATION_DELIVERED |
| AC-12 | REQ-11 | Not in Phase 3: reminders need the deadlines of REQ-10. `reminder` is already a valid webhook type. | — |

### 8.3 Configuration

All are optional environment variables, in whole seconds or counts (decision 81).

| Variable | Default | Meaning |
|---|---|---|
| `SAMPLING_INTERVAL_SECONDS` | 300 | REQ-01 sampling interval |
| `GAP_TIMEOUT_INTERVALS` | 3 | Missed intervals before a gap (REQ-02) |
| `NOTIFY_MAX_DELAY_SECONDS` | 900 | REQ-04 bound, used for `within_bound` |
| `SWEEP_INTERVAL_SECONDS` | 60 | How often the sweeper runs (wall clock) |
| `DELIVERY_POLL_SECONDS` | 5 | How often the delivery pass runs (wall clock) |
| `WEBHOOK_TIMEOUT_SECONDS` | 5 | Timeout for a delivery or a verification ping |
| `DELIVERY_MAX_ATTEMPTS` | 5 | Attempts before NOTIFICATION_FAILED |

### 8.4 Known limits

| Item | Decision |
|---|---|
| **Part 1 integration.** Ingest calls `gaps.close_if_open` and `notifications.enqueue` in its transaction; `GET /v1/events?type=monitoring_gap` lists worker gap events. | 82, 85 |
| **Webhooks registered later miss earlier notifications.** Deliveries are created when the notification is requested. | 86 |
| **No webhook, no delivery.** A notification requested while no webhook is subscribed is recorded, but nothing is sent. | 86 |
| **At-least-once delivery.** A result that could not be recorded is retried when its lease runs out. | 88 |
| **Reminders (REQ-11)** are accepted as a webhook type but not generated: they depend on Phase 4 deadlines. | 89 |
| **Webhook calls are not restricted by destination.** Any http(s) host can be registered, private addresses included. | 87 |
