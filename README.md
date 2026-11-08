# EU_AI_ACT_MONITOR

Compliance monitoring system implementing **Part 1: detection and ingestion (API)**,
**Part 2: ledger and audit**, **Part 3: the worker** (gap sweeper and notifications), and
**Part 4 platform** (`/healthz`, test reset and clock advance, CI and the Docker image).
All of Parts 1 to 3 are built to `Phase3_Implementation_Spec.docx`.

See **[docs/part1_api.md](docs/part1_api.md)** for Part 1: endpoints, local setup, behaviour,
and what changed from its first version and why.

Where to look:

- **This README:** what the ledger is, how to check it, how to run everything, and its known
  limits; section 8 covers the worker.
- **[integration_notes.md](integration_notes.md):** what each part calls and provides, and what
  Part 4 was to do when replacing a stub (sections 5.12 and 10).
- **[docs/part1_test_map.md](docs/part1_test_map.md):** which test proves each Part 1 rule of
  the spec, and what happened to the first Part 1 tests.
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
| Part 4 platform: clock, sessions, baseline migration, seed fixtures | `app/clock.py`, `app/db.py`, `migrations/versions/0001_platform_stub.py`, `tools/seed_part1.py` |

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

The three seeded keys (spec 5) are also required by the app: `INGEST_KEY_SYS_42`,
`REVIEWER_KEY` and `VIEWER_KEY`. The test suite sets test-only values for them in
`tests/conftest.py`; set your own anywhere else.

### 5.5 Migrations

```sh
.venv/bin/alembic upgrade head      # runs as DATABASE_ADMIN_URL
.venv/bin/alembic current           # expect: 0007 (head)
```

The chain follows spec 4.4: 0001 Platform (Part 4), 0002 Ledger, 0003 Detection (Part 1) and
0004 Notifications (Part 3, section 8), then Phase 4's 0005 Review, 0006 Dispositions and 0007
Escalations (sections 10 to 12). Migration 0002 grants ledger privileges to the user named in
`DATABASE_URL`. It refuses to run if that user is the role running the migration. The stubs
Part 4 replaced are listed in [integration_notes.md](integration_notes.md), section 10.

**A database built from the `development-gap-sweeper` branch** is at an older revision `0003`
(`0003_worker.py`) that no longer exists, so `alembic downgrade base` fails with a missing
table. Drop and recreate the test database once, then rerun the role script (its "already
exists" errors are harmless):

```sh
psql "postgresql://postgres:postgres@localhost:5432/postgres" -c "DROP DATABASE monitor_test"
psql "postgresql://postgres:postgres@localhost:5432/postgres" -f tests/sql/bootstrap_roles.sql
```

Part 1's tables (metric samples, breach series, events and harm fields in 0003; API-key digests
and threshold versions in 0001) and its endpoints are described in
[docs/part1_api.md](docs/part1_api.md). To load the spec's seed fixtures by hand, run
`ENVIRONMENT=test .venv/bin/python -m tools.seed_part1` after the migrations (decision 128).

### 5.6 Tests and static checks

```sh
.venv/bin/pytest            # rebuilds the schema (alembic downgrade base, upgrade head) each run
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy              # strict; checks app, migrations, tests and tools
```

The same commands run in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) on every push
and pull request, against PostgreSQL 16. After the flagged pytest run, `tools/check_acceptance.py`
requires the AC-02, AC-04, AC-06 and AC-15 tests of Phase 3 and the Phase 4 tests of AC-07 to
AC-12, AC-14, AC-15 and AC-17 (`PHASE4_REQUIRED`, integration decision 218) in that run to have
passed. AC-12 is not a Phase 3 test (section 8.2). A second job builds the image and waits until `GET /healthz` returns 200.

At the time of writing the suite collects 947 tests: 532 for Part 2, 229 for Part 3 in
`tests/worker/` and 186 for Part 1 in `tests/part1/`, plus the deployment tests in
`tests/api/test_health.py`, `tests/api/test_platform.py` and `tests/test_lock_timeout.py`.

## 6. Known limits and operating notes

| Item | Decision |
|---|---|
| **Global append lock.** `append` takes one transaction-scoped advisory lock (key 1279607879) and holds it until the caller commits. So every append, including every event view (EVENT_VIEWED) and every audit call (AUDIT_VIEWED), waits for the one before it. | 10; M4 notes |
| **Lock timeout only when `LOCK_TIMEOUT` is set.** The deployed API and worker set it to `5s`. The test suite leaves it unset, so a test can hold the append lock until every caller is waiting. Without a timeout, a stuck transaction blocks every append. | 10, 110 |
| **Export shows webhook URLs.** Export returns every entry, including WEBHOOK_REGISTERED payloads, to any REVIEWER or VIEWER. Recommended: avoid secrets in webhook URLs. The API does not enforce this (decision 98). | 32, 98; M4 notes |
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
| **uvicorn is the server.** `python -m app.runtime api` execs it. Decision 50 recorded the earlier real-server check, before it was a dependency. | 50, 109 |
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
system stops sending data) and REQ-04 (the reviewer notified within a configurable bound,
default 15 minutes). It follows spec sections 3.1 to 3.8; the clause-by-clause check is
[docs/part3_spec_audit.md](docs/part3_spec_audit.md), and the settled choices are decisions 93
to 105. Run it as its own process, as the application role:

```sh
ENVIRONMENT=test .venv/bin/python -m app.worker
```

One loop runs the gap sweeper every `SWEEP_INTERVAL_SECONDS` (60) and a delivery pass every
`DELIVERY_POLL_SECONDS` (5), both on real elapsed time. Every decision about gaps, retries and
latency reads `clock.now()`, so in the test environment advancing the simulated clock takes
effect at the next sweep or pass. Only the sweeper heartbeat uses real time
(`clock.wall_now()`).

| What | Where |
|---|---|
| Gap sweeper, and `gaps.close_if_open` for Part 1's ingest | `app/gaps.py` |
| `notifications.enqueue` and the delivery pass `notifications.deliver_due` | `app/notifications.py` |
| Webhook URL rules, the HTTP call and registration | `app/webhooks.py` |
| `POST /v1/webhooks` | `app/api/webhook_routes.py` |
| The worker loop and `health_values` for Part 4's `/healthz` | `app/worker.py` |
| `retain_until`, the API time format and ids | `app/formats.py` |
| Settings | `app/config.py` |
| Tables `webhook`, `notification`, `notification_delivery` | `migrations/versions/0004_notifications.py` |
| `monitored_system` and `sweeper_heartbeat` (Part 4's 0001); `event` and `harm_field` (Part 1) | `migrations/versions/0001_platform_stub.py`, `migrations/versions/0003_detection.py` |
| Tests | `tests/worker/` |

### 8.1 Behaviour

- **Gap.** Each system's threshold is its own `gap_timeout_intervals × sampling_interval_s`
  (seed: 3 × 300 = 900 seconds), and its silence is `clock.now() − last_received_at`. When the
  silence reaches the threshold and the system has no open gap, the sweeper creates a FLAGGED
  `monitoring_gap` event with four NOT_APPLICABLE harm fields and `retain_until` six calendar
  months later (at least 183 days), appends GAP_OPENED and requests a `flag_notification`, in
  one transaction. A system row another process holds is skipped until the next sweep. A
  system has at most one open gap. Its next sample closes the gap: the event moves to CLOSED
  and GAP_CLOSED records `gap_seconds` from the gap's `received_at`.
- **Notification.** The delivery pass routes each new notification to the webhooks subscribed
  to its type; with none, it waits until one is registered. It then claims due deliveries with
  a lease and POSTs `{notification_type, notification_id, event_id, system_id, type,
  detected_at, sent_at}` with a 5-second timeout and no transaction open. Any 2xx answer is a
  delivery: NOTIFICATION_DELIVERED with an integer `latency_seconds` from the event's
  `received_at` and a boolean `within_bound`. A failed attempt is retried after 10, 30, 60, 120
  and 300 seconds; the sixth failure appends NOTIFICATION_FAILED. Late deliveries are logged
  and counted, and never delayed. Delivery is at least once, so a receiver can see the same
  `notification_id` twice.
- **Webhook registration.** `POST /v1/webhooks {url, event_types}`, REVIEWER or VIEWER. The URL
  must be http or https; outside the test environment, https only and no private or loopback
  address. Any other URL is accepted and stored as received. The URL is pinged with
  `{"notification_type": "verification_ping", "sent_at": ...}` before anything is stored:
  201 `{webhook_id}` on a 2xx answer, 422 `WEBHOOK_UNVERIFIED` otherwise, and 400
  `MALFORMED_BODY` for a bad body or a refused URL, with no ping.
- **Observability.** Each sweep logs the systems examined and gaps opened; each delivery
  attempt logs the notification id, webhook id, attempt, status and duration; each late
  delivery logs a warning. `worker.health_values(engine)` returns the seconds since the last
  sweep, the number of pending deliveries and the age of the oldest one.

### 8.2 Acceptance criteria

| Id | Requirement | Where it is exercised | Method |
|---|---|---|---|
| AC-02 | REQ-02 | `tests/worker/test_gaps.py::test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do` (600 s and 899 s: no gap; 900 s: gap); `tests/worker/test_worker_system.py::test_gap_to_delivery_to_close_and_reverify` (exactly 15 minutes withheld, then the worker delivers to a real HTTP receiver) | Automated. The manual check of delivery reads NOTIFICATION_DELIVERED in `GET /v1/ledger/export`, or a registered webhook receiver |
| AC-04 | REQ-04 | `tests/worker/test_notifications.py`: `test_delivery_appends_delivered_with_integer_latency_and_boolean_bound`, `test_within_bound_compares_latency_with_the_configured_bound` (900 s in bound, 901 s not), `test_ac04_the_bound_is_configurable_to_five_minutes` (300 s in bound, 301 s not), `test_ac04_a_slow_receiver_with_a_low_bound_is_not_within_bound` | Automated: `latency_seconds` and `within_bound` in NOTIFICATION_DELIVERED |
| AC-12 | REQ-11 | Not in Phase 3: reminders need the deadlines of REQ-10. `reminder` is already a valid webhook type. | — |

### 8.3 Configuration

From spec 5 (decision 100). Each is read on every call.

| Variable | Default | Meaning |
|---|---|---|
| `ENVIRONMENT` | required | `test` allows any webhook address; anything else allows only public https URLs |
| `NOTIFY_MAX_DELAY_SECONDS` | 900 | REQ-04 bound, used for `within_bound`; it never delays sending |
| `SWEEP_INTERVAL_SECONDS` | 60 | How often the sweeper runs (real time) |
| `DELIVERY_POLL_SECONDS` | 5 | How often the delivery pass runs (real time) |
| `LOG_LEVEL` | INFO | The worker's log level |
| `SAMPLING_INTERVAL_SECONDS` | 300 | Seeded into sys-42's `monitored_system` row by Part 4; the sweeper reads the row, not this variable |
| `GAP_TIMEOUT_INTERVALS` | 3 | Seeded into sys-42's `monitored_system` row by Part 4, as above |

The webhook timeout (5 seconds) and the number of attempts (6) are fixed by the spec and are
constants, not settings.

### 8.4 Known limits

| Item | Decision |
|---|---|
| **Part 1 integration.** Ingest calls `gaps.close_if_open` and `notifications.enqueue` in its transaction; `GET /v1/events?type=monitoring_gap` lists worker gap events. | 93, 101 |
| **Platform tables.** `monitored_system` and `sweeper_heartbeat` come from Part 4's migration 0001, which keeps its stub-era file name `0001_platform_stub.py` ([integration_notes.md](integration_notes.md), section 10). `event` and `harm_field` are Part 1's real 0003. | 95, 127 |
| **Webhooks registered after routing miss that notification.** A notification is routed once, to the webhooks subscribed at that moment. One routed to nobody waits for the first webhook. | 94 |
| **At-least-once delivery.** A result that could not be recorded is retried when its lease runs out. | 94 |
| **Webhook URLs are not checked for secrets.** Any http or https URL is stored as received and shown in the export; avoiding secrets in it is recommended. | 98 |
| **Address check at registration only.** Outside the test environment, a host that later resolves to a private address is not re-checked at delivery. | 99 |
| **Reminders (REQ-11)** are accepted as a webhook type but not generated: they depend on Phase 4 deadlines. | 89 |
| **The two spec 3.9 rows that need Part 1's ingest**, "Duplicate resets silence" and "Wrong sender clock", are tested through the real ingest in `tests/worker/test_gaps_via_ingest.py`. | 105, 116 |

## 9. Deployment

`docker compose up --build` starts PostgreSQL 16, migrates, seeds sys-42 and the three API
keys, and runs the API on port 8000 and the worker. Open `http://127.0.0.1:8000/ui/` for the
console UI (health, reset, clock advance, ledger, and running the smoke/acceptance/full
test suite). `GET /healthz` returns the three worker values from `worker.health_values`
(decision 111). In `ENVIRONMENT=test`, `POST /v1/test/reset`,
`POST /v1/test/clock/advance`, and `POST /v1/test/suite` are available for the Pod demo
(decision 133). The role script runs once, as the database superuser, the first time the
volume is created. `docker compose down -v` drops that volume.

The same UI can be hosted on **Vercel** (`vercel.json` builds `web/` from `app/static`).
Set `API_BASE_URL` to a publicly reachable API (Docker behind a tunnel, or any host running
compose). Vercel only serves the static console; it does not run Postgres, the worker, or
pytest.

A one-command walkthrough of ingest → webhook → gap → verify is in
[`tools/demo_showcase.sh`](tools/demo_showcase.sh). The passwords and keys in the compose
file are the local test values from section 5.4 and `tests/conftest.py`. AWS is not created
from this repository. What is still open, including the Evidently report format, is in
[docs/phase3_uncertainties.md](docs/phase3_uncertainties.md).

## 10. Phase 4 Part 1: review and harm fields

Part 1 of Phase 4 adds the human review of a flagged breach event: the four harm fields are
pre-filled at flag time, a reviewer's first view starts the review, and a disposition's harm
decisions are validated and written. It is built on branch `phase4-part1-review` from the base
commit 4d1487e. Decisions 154 to 163 in [docs/decisions.md](docs/decisions.md) record the
choices the specification left open.

### 10.1 What Part 1 does

| Behavior | What happens | Spec |
| --- | --- | --- |
| Pre-fill at flag time | Ingest step 11 calls `review.prefill_harm_fields` for every new breach event. For each of the four keys, in contract order, a `harm_context` row for (system, key) gives the field the template text below as both `evidence` and `prefill_evidence`; no row gives null for both. Every field is PREFILLED_PENDING_CONFIRMATION with no confirmer, and no tier is written (INV-3). Gap events keep four NOT_APPLICABLE fields with null evidence. The EVENT_FLAGGED payload is unchanged. | 1.3 |
| Review start | A REVIEWER's GET /v1/events/{id} of a FLAGGED threshold_breach event moves it to UNDER_REVIEW, once, and the route appends EVENT_VIEWED then REVIEW_STARTED in the same transaction. Viewers, other states and gap events change nothing (INV-12). | 1.4, D3 |
| Validation of harm fields | `parse_harm_fields` turns a request's `harm_fields` into decisions, raising 400 MALFORMED_BODY for a malformed shape; `require_complete` raises 409 HARM_FIELDS_UNCONFIRMED unless all four keys are present and each is CONFIRMED or NOT_APPLICABLE. Both are pure. | 1.5, Addendum rule 2 |
| apply_harm_fields | Writes complete decisions to the four rows. CONFIRMED with text stores the text, CONFIRMED with null stores the pre-fill, NOT_APPLICABLE stores the text or null. Only a field whose status or stored evidence changes gets the caller as `confirmed_by` and `now` as `confirmed_at`. Returns the changed keys. | 1.5 |
| Event detail | GET /v1/events/{id} keeps the contract's Event shape and order, takes `disposition`, `revisions` and `deadline_utc` from `dispositions.summary`, and never shows `prefill_evidence`. | 1.6 |
| Schema | Migration 0005 adds `harm_context` (SELECT only for the app role) and `harm_field.prefill_evidence`. | 1.7 |
| Logging | On `api.review`: one DEBUG line per pre-fill with the event id and counts, one INFO line per changed harm field with the key and the two statuses, one INFO line per review start. No line carries evidence or context text. | 1.8 |

### 10.2 Where the code lives

| What | Where |
| --- | --- |
| Pre-fill, review start, parsing, completeness, writing | `app/review.py` |
| The pre-fill call in ingest step 11 | `app/ingestion.py` |
| The review start and REVIEW_STARTED in the event-detail route | `app/api/ingestion_routes.py`, `get_event` |
| The Event detail (summary fields from Part 2) | `app/events.py`, `detail` |
| `harm_context` and `prefill_evidence` | `migrations/versions/0005_review.py` |
| Decisions 154 to 163 | `docs/decisions.md`, Part 1 decisions |
| Tests | `tests/part1/test_review_*.py`, and the Phase 3 tests in 10.6 |

### 10.3 The evidence template

The template is one line. Values are plain decimal strings with their stored digits, the word
is `above` for gt and `below` for lt, the time is in the API format, and one space separates
the sentence from the context text (decision 155):

`{metric_name} = {observed_value} is {above|below} the limit {threshold_value} (threshold {threshold_version_id}) at {detected_at}. {context_text}`

For AC-07 (seeded sys-42, `fundamental_rights_infringement`, accuracy 0.61 against the 0.70
limit of thr-2 at 2026-10-05T09:00:00Z) the stored evidence is:

`accuracy = 0.61 is below the limit 0.70 (threshold thr-2) at 2026-10-05T09:00:00Z. sys-42 makes automated decisions about natural persons, so degraded performance can infringe their fundamental rights (Art. 3(49)(c)).`

The other three fields of that event have null evidence.

### 10.4 Running the tests

With the test database of section 5 running:

| Run | Command | Result on Part 1's branch (`d4d8c1f`) |
| --- | --- | --- |
| Part 1's review tests | `.venv/bin/pytest tests/part1/test_review_*.py` | 124 passed, 44 skipped |
| The whole suite | `.venv/bin/pytest` | 1086 passed, 44 skipped |
| The integration-marked tests | `.venv/bin/pytest --run-integration -m integration` | 44 items, all fail at Part 2's 501 stub |
| List the integration-marked tests | `.venv/bin/pytest --collect-only -q -m integration` | 44 of 1130 collected |

The 44 integration items are the five functions of `tests/part1/test_review_integration.py`.
They need Part 2's real POST /v1/events/{id}/disposition and pass only once Parts 1 and 2 are
merged:

| Test | Items | Needs |
| --- | --- | --- |
| `test_rejected_harm_fields_write_nothing_and_append_nothing` | 40 | Part 2's disposition route calling `parse_harm_fields` and `require_complete` before any write |
| `test_ac07_confirmation_record_through_the_api` | 1 | Part 2's route calling `apply_harm_fields` with `now` = assigned_at |
| `test_an_evidence_override_through_the_api_keeps_the_prefill` | 1 | as above, and revisions |
| `test_a_revision_through_the_api_touches_only_the_changed_field` | 1 | as above, and revisions |
| `test_ac17_detection_time_and_assignment_anchored_deadline` | 1 | Part 2's real deadlines (DEATH 10 days, `DEADLINE_CALENDAR=calendar_days`) |

### 10.5 Facts for Parts 2 and 3

The dataclasses in `app/review.py` are frozen and keep the base commit's shape:

| Type | Fields |
| --- | --- |
| `PrefillFacts` | `metric_name: str`, `observed_value: Decimal`, `threshold_value: Decimal`, `comparator: str` (gt or lt), `threshold_version_id: str`, `detected_at: datetime` |
| `HarmFieldDecision` | `status: str`, `evidence: str or None` (None both for null and for an omitted evidence) |
| `ParsedHarmFields` | `complete: bool`, `fields: Mapping[str, HarmFieldDecision]` (the keys sent, in contract order, any of the three statuses) |
| `HarmDecisions` | `fields: Mapping[str, HarmFieldDecision]` (all four keys, in contract order, each CONFIRMED or NOT_APPLICABLE) |

The two exceptions and their exact response bodies:

| Exception | Raised by | Status and body |
| --- | --- | --- |
| `HarmFieldsMalformed` | `parse_harm_fields` | 400 `{"error_code":"MALFORMED_BODY","message":"the request is malformed"}` |
| `HarmFieldsUnconfirmed` | `require_complete` | 409 `{"error_code":"HARM_FIELDS_UNCONFIRMED","message":"all four harm fields must be CONFIRMED or NOT_APPLICABLE"}` |

What the disposition route and the report guard (both Part 2) can rely on:

- `parse_harm_fields(raw)` takes None for BOTH an absent and a null `harm_fields`: pass
  `body.get("harm_fields")`. An empty object `{}` is 409, not 400. Every value is checked
  before completeness, so a malformed value is 400 even when keys are missing.
- The disposition route must take `harm_fields` as RAW JSON, with no required field and no
  content validation in its request model. Otherwise an absent `harm_fields` would become a
  400 instead of the 409 the spec pins.
- `apply_harm_fields(session, event_id, principal, decisions, now)` expects the event row
  already locked by the caller. Its `now` becomes `confirmed_at`: pass the disposition's
  assigned_at. It appends nothing to the ledger and writes nothing to the event row. It
  returns the changed keys in contract order (it locks the four rows in field_key order).
- `start_if_reviewer` appends nothing. The event-detail route appends EVENT_VIEWED then
  REVIEW_STARTED. Part 2 does not call `start_if_reviewer`.
- `dispositions.summary(session, event_id)` must return the contract's Event form
  `{disposition, revisions, deadline_utc}` with times as API strings. events.detail places
  these three values as given; `tests/part1/test_review_detail.py` pins this with contract
  Example 2's values.
- G2 is decision 154: evidence containing U+0000 or a lone surrogate is 400 MALFORMED_BODY,
  because PostgreSQL cannot store it.
- Evidence length counts code points: 2,000 is accepted and 2,001 is 400 (decision 156).
- CONFIRMED with null over an earlier override puts the pre-fill back and counts as a change
  (decision 158).

### 10.6 Phase 3 tests changed (Addendum 9.10)

| Test | Why |
| --- | --- |
| `tests/part1/test_ingestion.py`, the two contract Example 1 tests | the rights field is now pre-filled |
| `tests/api/test_transactions.py::test_concurrent_audits_and_views_keep_a_clean_chain` | a reviewer view appends REVIEW_STARTED once |
| `tests/api/test_cross_verify.py::test_live_export_through_the_api_verifies` | 13 entries instead of 12, one REVIEW_STARTED |
| `tests/api/test_audit.py::test_views_appear_in_the_audit_in_seq_order_after_the_earlier_entries` | the views follow REVIEW_STARTED at seq 10 and 11 |
| `tests/worker/test_gaps.py::test_a_gap_event_has_the_spec_fields_and_four_not_applicable_harm_fields` | gap fields have a null `prefill_evidence` |
| `tests/worker/test_schema.py`, the Phase 4 Part 1 slot | `harm_context` and `prefill_evidence` |

### 10.7 Known limitations

| Limitation | Decision |
| --- | --- |
| **The downgrade test runs other parts' downgrades.** The (b) test that downgrades to 0004 and back runs Parts 2 and 3's real downgrades once they are merged. | |
| **A reviewer's view starts the review.** A reviewer's GET of a FLAGGED breach event starts the review (D3), so passive reads must use the viewer key. | 157, 161 |
| **Field-change log lines come before the commit.** `apply_harm_fields` writes its INFO lines inside Part 2's transaction, so a rolled-back disposition can leave lines for changes that were not committed. | 162 |
| **docs/part1_api.md is stale.** It still describes the Phase 3 harm fields (null evidence, no pre-fill) and the event detail without the review start; Part 1 does not update it. | |
| **AC-17 assumes the default calendar.** The integration test expects deadline = assigned_at + 10 days, which holds only for `DEADLINE_CALENDAR=calendar_days`. | |

## 11. Phase 4 Part 2: dispositions and deadlines

Part 2 of Phase 4 records the reviewer's decision: one of exactly four dispositions, an
append-only revision history, the absolute Art. 73 deadline counted from the first assignment,
the rule that a deadline can shorten but never lengthen silently, and the report guard that
stops an event without a disposition from advancing (REQ-08a, 08b, 09, 10, 13). It is built on
branch `phase4-part2-dispositions`, which starts at Part 1's head `d4d8c1f`, itself on the base
commit `4d1487e` (decision 174). Decisions 174 to 190 in [docs/decisions.md](docs/decisions.md)
record the choices the specification left open.

### 11.1 What Part 2 does

| Behavior | What happens | Spec |
| --- | --- | --- |
| POST /v1/events/{id}/disposition | REVIEWER only. Body `{tier, rationale, harm_fields}`. In one transaction: lock the event row, run the checks, write the harm fields (Part 1), append a `disposition` row, set `event.state` and `event.deadline_utc`, call `escalations.plan` (Part 3), append one ledger entry, commit. 200 with the full Event. | 2.4 |
| Order of checks | 401, 403, 400 MALFORMED_BODY, 400 RATIONALE_REQUIRED, 404, 409 EVENT_NOT_REVIEWABLE, 409 HARM_FIELDS_UNCONFIRMED, 409 DEADLINE_EXTENSION_REQUIRES_RATIONALE. The first failure decides; nothing is written or appended before it. | 7, 2.4 |
| Revisions | Every later call on the same event is a revision: a new row with `supersedes_id` = the previous row, whatever the state (DISPOSITIONED or CLOSED, D2). Rows are never updated or deleted (trigger plus grants). | 2.4, 2.6, 2.9 |
| Deadline | `deadline_utc = compute(anchor, tier, calendar)`, where the anchor and calendar are the first row's. NOT_SERIOUS has none and closes the event. | 2.5, 6 |
| No silent extension | A serious tier whose window is longer than the most recent serious tier's needs a non-blank rationale, including on a reopen from NOT_SERIOUS (409 without one). | Addendum rule 1 |
| Event fields | `disposition`: tier and rationale from the latest row, reviewer_id and assigned_at from the first. `revisions`: one entry per later row, oldest first. `deadline_utc`: the event row's. | 2.7 |
| GET /v1/events/{id}/report | REVIEWER only. 404 unknown event; 409 NO_DISPOSITION without a disposition row (gap events always); otherwise the report (200), see section 14. | Phase 5 spec 3.6 |
| Ledger | DISPOSITION_RECORDED (first) and DISPOSITION_REVISED (later), actor the reviewer, subject the event id, with the payloads of spec 2.10. | 2.10 |
| Logging | One INFO line per recorded disposition on `api.disposition`, after the commit; never the rationale or evidence. | decision 187 |

### 11.2 Errors

| Status | error_code | message |
| --- | --- | --- |
| 400 | MALFORMED_BODY | `the request is malformed` |
| 400 | RATIONALE_REQUIRED | `rationale is required when tier is NOT_SERIOUS` |
| 404 | NOT_FOUND | `resource not found` |
| 409 | EVENT_NOT_REVIEWABLE | `a monitoring gap event cannot be dispositioned` |
| 409 | HARM_FIELDS_UNCONFIRMED | `all four harm fields must be CONFIRMED or NOT_APPLICABLE` |
| 409 | DEADLINE_EXTENSION_REQUIRES_RATIONALE | `extending a running deadline requires a rationale` |
| 409 | NO_DISPOSITION (report) | `no disposition has been recorded for this event` |

### 11.3 Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEADLINE_CALENDAR` | `calendar_days` | `calendar_days` or `working_days`. Anything else stops start-up. |
| `DEADLINE_HOLIDAYS` | (empty) | Comma-separated `YYYY-MM-DD` dates that are not working days, used only with `working_days`. A malformed list stops start-up (decision 176). |

The first disposition of an event records the calendar (`calendar_days` or
`working_days;holidays=YYYY-MM-DD,...`), and every revision reuses it, so a configuration change
never moves an existing deadline. Working days are Monday to Friday on UTC dates, counted from
the day after the anchor, at the anchor's UTC time of day (decision 177).

### 11.4 Where the code lives

| What | Where |
| --- | --- |
| Deadline engine and the rule 1 check (pure) | `app/deadlines.py` |
| Request parsing, the transaction, `summary`, the report guard | `app/dispositions.py` |
| The two routes | `app/api/disposition_routes.py` (replaces the deleted `app/api/stub_routes.py`) |
| `DEADLINE_CALENDAR`, `DEADLINE_HOLIDAYS` | `app/config.py`, Part 2 slot |
| The `disposition` table | `migrations/versions/0006_dispositions.py` |
| Tests | `tests/part2/`, and the Phase 3 tests in 11.7 |

### 11.5 Running the tests

With the test database of section 5 running:

| Run | Command | Result on Part 2's branch |
| --- | --- | --- |
| Part 2's tests | `.venv/bin/pytest tests/part2` | 159 passed, 8 skipped |
| The whole suite | `.venv/bin/pytest` | 1247 passed, 52 skipped (the base of this branch: 1086 passed, 44 skipped) |
| The integration-marked tests | `.venv/bin/pytest --run-integration -m integration` | 44 passed (all of Part 1's), 8 failed (Part 2's, no `escalation` table yet) |

Part 2's integration-marked tests (Addendum rule 10) are in
`tests/part2/test_disposition_integration.py`. They check the `escalation` rows that Part 3's
migration 0007 and `escalations.plan` create, so they failed on Part 2's branch, where 0007 was
still a placeholder. With Part 3 merged they pass:

| Test | Needs |
| --- | --- |
| `test_ac09_a_not_serious_first_disposition_has_no_escalation_rows` | Part 3's escalation table |
| `test_d2_revisions_cancel_and_replan_the_reminders` | Part 3's plan (cancel, then back to PENDING) |
| `test_a_stricter_revision_moves_the_unsent_reminders` | Part 3's plan (re-plan PENDING rows) |
| `test_addendum_rule_1_reopens_and_the_reminders` (4 cases) | Part 3's plan |
| `test_a_looser_revision_without_a_rationale_leaves_the_reminders_alone` | Part 3's plan |

Because this branch carries Part 1's real code, Part 1's integration-marked tests
(`tests/part1/test_review_integration.py`) pass here with `--run-integration`.

### 11.6 Facts for Parts 3 and 4

- `escalations.plan(session, event_id, anchor, deadline_utc)` is called once per successful
  call, after the event row is updated and before the ledger append, with the event row locked.
  `anchor` is the first row's `assigned_at` (it never changes); `deadline_utc` is None for
  NOT_SERIOUS. It is looked up on the module at call time, so it can be replaced in tests.
- `dispositions.summary(session, event_id)` returns `{disposition, revisions, deadline_utc}`
  with API-format times: `disposition.tier` is the tier in force and `disposition.assigned_at`
  is the first assignment (the anchor), as Addendum rule 3 expects.
- `deadlines.compute(anchor, tier, calendar)` and `deadlines.current_calendar()` are pure apart
  from reading the two settings; `deadlines.TIER_DAYS` and `deadlines.TIERS` are the tier
  tables.
- A disposition row cannot be updated or deleted even by the owner role; test reset empties the
  table with TRUNCATE as the owner, through `app.platform.with_phase4_tables`.

### 11.7 Phase 3 tests changed (Addendum 9.10)

| Test | Why |
| --- | --- |
| `tests/part1/test_auth.py::test_role_matrix` (ROUTES and STUBS) | the report is REVIEWER only; the allowed role now gets 400 (disposition) and 409 NO_DISPOSITION (report) instead of 501 |
| `tests/part1/test_events_api.py::test_disposition_and_report_stubs`, renamed `test_disposition_and_report_check_the_key_and_role_first` | the stubs are gone: 400 for a broken disposition body, 409 or 404 for the report, 403 for viewer and system |
| `tests/part1/test_observability.py::test_a_stub_501_is_logged_at_info_and_a_500_at_error`, the 501 half | the 501 now comes from the report guard after a recorded disposition |
| `tests/worker/test_schema.py`, the Phase 4 Part 2 slot | the `disposition` columns |

### 11.8 Known limitations

| Limitation | Decision |
| --- | --- |
| **Working days are UTC days.** A team in another time zone gets its weekend boundaries in UTC. | 177 |
| **The report is only a guard until Phase 5.** A reviewer gets 501 once a disposition exists, so AC-14's "reviewer succeeds" for reports completes in Phase 5. | D1, 186 |
| **One seeded reviewer.** Only rev-1 exists, so `disposition.reviewer_id` (first row) and a revision's `reviewer_id` cannot differ through the API; the test for which row feeds which field inserts rows as the owner. | |
| **Built on Part 1's head.** The branch includes Part 1's commits up to `d4d8c1f` (Addendum 9.2 expects part branches to be independent). | 174 |

## 12. Phase 4 Part 3: escalations and reminders

Part 3 of Phase 4 makes sure whoever registered for reminders hears about a deadline at 50%, 80%
and 100% of its window: never early, never twice and never missed, including when a revision moves
the deadline, when the test clock jumps far ahead, and when the worker and a clock advance run at
the same moment (REQ-11, AC-12). It is built on branch `phase4-part3-escalations`, which starts at
`phase4-integration` `4580d28` (decision 194). Decisions 194 to 206 in
[docs/decisions.md](docs/decisions.md) record the choices the specification left open.

### 12.1 What Part 3 does

| Behavior | What happens | Spec |
| --- | --- | --- |
| First plan | The plan that finds no rows inserts one PENDING row per configured percentage. That set is the event's for good. A first NOT_SERIOUS has no rows | 3.3, rule 5 |
| Later plan | PENDING rows move to the new window; CANCELLED rows return to PENDING; SENT rows never change | 3.3 |
| NOT_SERIOUS | Every PENDING row becomes CANCELLED. A reminder already triggered is still delivered | 3.3, rule 5 |
| Instant | anchor + ceil((deadline - anchor) x pct / 100) in whole microseconds, always from the first assignment | 6 |
| Firing | Each reminder in its own transaction: lock the event, lock the row (SKIP LOCKED), re-check the due time, mark SENT, enqueue, append REMINDER_TRIGGERED (actor `system`, subject the event id) | 3.4 |
| Delivery | The Phase 3 outbox: routing by type, lease, retries at 10, 30, 60, 120 and 300 s, six attempts. At least once, so receivers de-duplicate on notification_id | 3.4, 3.5 |
| Body | The flag fields plus tier, assigned_at, deadline_utc, escalation_pct and due_at, built from the escalation row, so a later revision never changes a retry | 3.5, rule 5 |
| Latency | NOTIFICATION_DELIVERED measures a reminder from due_at, not received_at | 3.5 |
| Worker | Each iteration: the sweep if due, fire_due, deliver_due. An error in fire_due is logged by type name and deliveries still run | 3.6 |
| Health | overdue_escalations counts PENDING rows more than 60 s past due. A growing value means the worker is stopped or behind | 3.6 |

### 12.2 Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ESCALATION_PERCENTAGES` | 50,80,100 | Integers 1 to 100, strictly ascending. A bad value stops start-up (decision 196). Read when an event gets its first plan; later changes do not touch existing rows |

### 12.3 Where the code lives

| What | Where |
| --- | --- |
| Plan and fire | `app/escalations.py` |
| Enqueue parameters, reminder body, latency, claim order | `app/notifications.py` |
| Worker pass and health value | `app/worker.py` |
| Setting | the Phase 4 Part 3 slot in `app/config.py`; the start-up check in `app/main.py` and `app/worker.py` |
| Table and notification changes | `migrations/versions/0007_escalations.py` |
| Tests | `tests/part3/`, and the Phase 3 tests in 12.6 |

### 12.4 Running the tests

| Run | Command | Result |
| --- | --- | --- |
| Part 3's tests, on Part 3's branch | `.venv/bin/pytest tests/part3` | 81 passed, 6 skipped |
| The whole suite, no flag, on Part 3's branch | `.venv/bin/pytest` | 1330 passed, 58 skipped, 0 failed |
| Part 3's trial merge with Part 4, flagged | `.venv/bin/pytest --run-integration --ignore=tests/acceptance` | 1513 passed, 3 failed: Part 4 console tests, CRLF on a Windows checkout only (they pass with LF; Part 3's report, now kept outside the repository, decision 224) |
| All four parts merged, no flag | `.venv/bin/pytest --ignore=tests/acceptance` | 1474 passed, 71 skipped, 0 failed |
| All four parts merged, flagged | `.venv/bin/pytest --run-integration --ignore=tests/acceptance` | 1545 passed, 0 failed |


### 12.5 Tests that wait for integration

These are in `tests/part3/test_escalations_integration.py`. They need Part 2's endpoint, already on
this branch, and Part 4's clock-advance endpoint, which requires a key and fires due reminders, so
they failed on Part 3's branch until Part 4 merged. They pass with all four parts merged.

| Test | Checks |
| --- | --- |
| `test_model_5_ex_1_a_stricter_revision_through_the_api` | Ex-1 timing: +5d, +8d, +10d |
| `test_a_looser_revision_with_a_rationale_through_the_api` | reminders move to the 15-day instants |
| `test_not_serious_then_reopen_through_the_api` | D2, sent percentages are not resent |
| `test_no_duplicates_through_the_clock_advance_endpoint` | ten further advances and worker passes |
| `test_a_worker_pass_and_a_clock_advance_across_the_50_instant_50_times` | one reminder each time |
| `test_healthz_shows_a_real_overdue_row` | /healthz shows a real overdue row |

### 12.6 Phase 3 tests changed (Addendum 9.10)

| Test | Why |
| --- | --- |
| `tests/worker/test_schema.py::test_notification_references_the_event_once` | the partial unique index replaces `uq_notification_event` |
| `tests/worker/test_notifications.py::test_enqueue_records_the_request_and_routes_nothing` | exact `SELECT *`, so it gains `escalation_pct`; not listed in 9.10 (decision 206) |
| `tests/worker/test_schema.py`, the Phase 4 Part 3 slot | the `escalation` table and `notification.escalation_pct` |

### 12.7 Known limitations

| Limitation | Decision or spec |
| --- | --- |
| **No reminders while the machines are stopped.** On wake each due reminder fires once, late; none is lost or duplicated. | spec 3.9 |
| **Webhooks only.** There is no email or named compliance-officer account. | spec 3.9 |
| **At-least-once delivery.** Receivers de-duplicate on notification_id. | spec 3.4 |
| **A triggered reminder is delivered after a revision to NOT_SERIOUS.** The outbox has no cancel status. | rule 5 |
| **Late routing.** A reminder enqueued before any matching webhook exists goes to one registered later. | rule 5 |
| **A retry can reorder reminders.** Order holds for first attempts only; receivers read escalation_pct. | 204 |

## 13. Phase 4 Part 4: platform

Branch: `phase4-part4-platform`, from the Phase 4 base commit `4d1487e`. Owner: Bhanu.

### What this part owns

Contract test endpoints, the simulated clock's process lifetime, `/healthz`, the console
reviewer panel, the Model 5 fixtures, the HTTP acceptance runner, the webhook sink, the Phase 4
demo script, and the deployment notes that sit on top of the base commit.

| Surface | Contract behaviour |
| --- | --- |
| `POST /v1/test/reset` | REVIEWER or VIEWER; **204** empty body; truncates Phase 4 tables when present |
| `POST /v1/test/clock/advance` | REVIEWER or VIEWER; body `{ "seconds": <int≥0> }`; **200** `{ "simulated_time" }` only; then `gaps.sweep` → `escalations.fire_due` → `notifications.deliver_due` |
| `POST /v1/test/suite` | REVIEWER only (named exception to the common matrix). Scopes `smoke` and `acceptance` only; anything else is 400 |
| Outside `ENVIRONMENT=test` | Standard 404 **before** any key check |
| `/healthz` | Adds `overdue_escalations`; **503** when the database is unreachable (LIM-09) or the sweeper is stale |

Ordinary process start (`runtime.seed`) does **not** zero the clock offset. Only `prepare` and
reset do, so a Fly idle stop mid-demo does not snap simulated time back to wall time.

**Test-route keys (decision 137).** In `ENVIRONMENT=test` only, when no `api_key` row matches the
presented key, the test routes also accept the configured `REVIEWER_KEY` and `VIEWER_KEY` (with
their roles), so a reset works on an empty key table. The system key is never accepted this way,
a stored row always wins, and ordinary routes never use it.

**The console suite runner (LIM-02, decision 141).** `acceptance` runs the HTTP acceptance runner
(`tests/acceptance`) as a subprocess against this service's own URL (`SUITE_BASE_URL`, default
`http://127.0.0.1:8000`), with **a reset before and after the run**, even when it fails. The
runner gets the service's keys and no `DATABASE_*` variable, and is started with
`--confcutdir=tests/acceptance`. `smoke` calls `/healthz`, reset and ledger verify. **The
database-level pytest suite never runs in a service; it runs only in CI** (there is no `full`
scope). The image has what the runner needs: the Dockerfile copies `tests/` and installs the
`[dev]` extra (pytest; httpx is a main dependency).

### Console, fixtures, demo

- Console (`app/static`, served at `/ui/`): event review panel with the four pinned disposition
  labels, harm-field confirmations, risk indicator (REQ-15), a revisions table and the
  escalation timeline (see "Console" below).
- Model 5 fixtures: [`tests/fixtures/phase4/`](tests/fixtures/phase4/) (Ex-1, Ex-2, Ex-4; the
  README there gives the step format and the source of every expected value), run with
  `python tools/run_model5.py [--example ex1]` and the runner's environment variables. It exits
  1 at the first mismatch in any example (decision 144).
- Webhook sink: `python tools/webhook_sink.py` (POST/GET/DELETE `/hook/{channel}`), and the
  `sink` service in `docker-compose.yml` on port 8765. **Anything sent to a channel is readable
  by anyone who knows the channel name**: the sink has no authentication (LIM-06).
- Demo: `python tools/demo_phase4.py [--keep-going]`, HTTP only, with `BASE_URL`, the three keys,
  `WEBHOOK_SINK_URL` and `WEBHOOK_SINK_READ_URL`. It walks spec section 12's ten steps in order,
  prints `step N ok` or `step N FAILED`, and exits 1 on the first mismatch (status, body field,
  state, ledger count or receiver content); `--keep-going` reports every step and still exits 1.
  It registers step 2's webhook on `WEBHOOK_SINK_URL` and a reminder-only webhook on the same
  URL plus `-reminder`, and ends by exporting the ledger and running `tools/verify_ledger.py`
  on it (decision 146). Before Parts 1–3 are merged it fails at the first step they own.
- HTTP acceptance: `tests/acceptance/` (see below).

### Console (spec 4.6, decision 142)

- **Act as** defaults to **Viewer**. Loading an event as a reviewer starts the review (D3, an
  audited state change), so it is an explicit choice.
- Harm fields in contract order, each with its stored evidence or the literal `no evidence
  available`. **No status is pre-selected**: only a decision already stored (CONFIRMED or
  NOT_APPLICABLE) is shown. A field left unchosen is sent as pending, so the service answers
  409 `HARM_FIELDS_UNCONFIRMED`; the console never confirms anything itself.
- Revisions table from the contract keys `prior_tier`, `new_tier`, `reviewer_id`, `revised_at`.
- Risk indicator text as spec 4.6, beside `deadline_utc`. **"Now" is the simulated clock, read
  with `POST /v1/test/clock/advance {"seconds": 0}` with the selected key, once per event load**
  (Addendum rule 7 names this as the way to read simulated now; spec 4.6 names no side-effect
  free source). **Side effect:** that call also runs `gaps.sweep`, `escalations.fire_due` and
  `notifications.deliver_due` once (spec 4.5), so loading an event can open a due gap and send due
  reminders and deliveries, exactly as the worker would on its next pass. It is never called on
  a timer or a poll. Outside `ENVIRONMENT=test` the call is 404 and the browser clock is used;
  the indicator says which clock it used.
- Escalation timeline: the 50/80/100 % instants with Part 3's formula (anchor = first
  `assigned_at`, `anchor + ceil((deadline − anchor) × p / 100)` µs), for display only.
- API errors are shown as `HTTP <status> <error_code>: <message>`.

**Manual check (needs Parts 1 and 2; run on the integration branch).** Reset; ingest
`accuracy = 0.61`; load the event as Viewer (state stays FLAGGED; rights evidence shown, the
other three `no evidence available`; no option selected); switch to Reviewer and load (state
UNDER_REVIEW); submit OTHER_SERIOUS with a field unchosen (409 `HARM_FIELDS_UNCONFIRMED` shown);
choose all four and submit (deadline +15 d, three timeline instants); submit DEATH (one revisions
row OTHER_SERIOUS → DEATH by rev-1, deadline +10 d from the same anchor); as Viewer submit (403
`FORBIDDEN_ROLE` shown).

### HTTP acceptance runner

`tests/acceptance` talks to a running stack over HTTP only (spec 4.8). Run it from the
repository root with its own conftest only:

```bash
docker compose up -d --build --wait     # API, worker, PostgreSQL and the sink
BASE_URL=http://127.0.0.1:8000 \
WEBHOOK_SINK_URL=http://sink:8765/hook/ci \
WEBHOOK_SINK_READ_URL=http://127.0.0.1:8765/hook/ci \
pytest tests/acceptance --confcutdir=tests/acceptance -p no:cacheprovider
```

- `--confcutdir=tests/acceptance` is required. It keeps `tests/conftest.py` out, whose session
  fixture downgrades and upgrades the database named by `DATABASE_ADMIN_URL`. The runner needs
  no `DATABASE_*` variable and never connects to a database
  (`tests/test_runner_isolation.py`).
- Without `BASE_URL` every test is skipped. Each test starts with `POST /v1/test/reset`.
- `WEBHOOK_SINK_URL` is the address the **API** posts to; `WEBHOOK_SINK_READ_URL` is where the
  **runner** reads the same channel back (it defaults to `WEBHOOK_SINK_URL`). With compose they
  differ, because the API container reaches the sink as `sink`. Keys come from `INGEST_KEY` (or
  `INGEST_KEY_SYS_42`), `REVIEWER_KEY` and `VIEWER_KEY`, with the test defaults.
- The in-process suite skips this folder: CI runs `pytest --ignore=tests/acceptance`. The image
  job runs the runner, `tools/run_model5.py` and `tools/demo_phase4.py` against the compose
  stack, under the Addendum 9.5 condition (main, `phase4-integration`, pull requests into main).
- The acceptance gate (`tools/check_acceptance.py`) merges the Phase 3 and Phase 4 criteria
  per criterion as a union (decision 151).

**One file per criterion (spec 4.8).** The expected values come from the contract, the Phase 3
and Phase 4 specifications and sections 10.3, 10.5, 11.2 and 11.6 of this README, never from
the code. `tests/acceptance/api.py` holds the HTTP helpers and those values. Ids are
`tests/acceptance/<file>::<test>`:

| AC | File | Tests |
| --- | --- | --- |
| AC-01 | `test_ac01_ingest.py` | `test_an_hour_at_the_default_interval_is_collected_without_a_gap`, `test_every_sample_is_stored_once_with_a_distinct_id` |
| AC-02 | `test_ac02_gaps.py` | `test_two_withheld_intervals_open_no_gap`, `test_three_withheld_intervals_open_one_gap_event`, `test_a_sample_after_the_outage_closes_the_gap`, `test_the_gap_is_notified_within_the_configured_delay` (sink) |
| AC-03 | `test_ac03_flagging.py` | `test_contract_example_1_flags_with_sample_and_event_ids`, `test_a_value_exactly_at_the_threshold_does_not_flag` (drift, performance), `test_one_unit_beyond_the_threshold_flags_with_the_exact_fields` (drift, performance) |
| AC-04 | `test_ac04_latency.py` | `test_the_flag_notification_is_delivered_within_the_bound` (sink), `test_a_webhook_registered_only_for_reminders_gets_no_flag` (sink) |
| AC-06 | `test_ac06_export_verify.py` | `test_the_export_has_the_pinned_entry_format`, `test_the_export_verifies_offline_and_matches_the_server`, `test_an_edited_record_is_named_by_the_offline_verifier` |
| AC-07 | `test_ac07_harm_fields.py` | `test_fields_with_evidence_are_prefilled_and_the_others_are_blank`, `test_the_report_before_reviewer_action_is_refused`, `test_a_field_left_pending_is_refused_and_nothing_is_recorded`, `test_each_confirmation_records_the_reviewer_and_the_time` |
| AC-08 | `test_ac08_report_guard.py` | `test_the_report_without_a_disposition_is_409_and_changes_nothing` (flagged, under review), `test_a_gap_event_never_reaches_reporting`, `test_an_unknown_event_is_404`, `test_after_a_disposition_the_guard_passes_to_the_phase_5_report` |
| AC-09 | `test_ac09_tiers.py` | `test_each_of_the_four_options_is_accepted`, `test_a_fifth_option_is_malformed_and_changes_nothing`, `test_not_serious_without_a_rationale_is_contract_example_3`, `test_not_serious_with_the_rationale_omitted_is_example_3`, `test_the_rationale_check_comes_before_the_event_lookup`, `test_not_serious_with_a_rationale_closes_with_no_deadline` |
| AC-10 | `test_ac10_revisions.py` | `test_a_revision_keeps_the_original_and_records_who_and_when`, `test_the_ledger_holds_both_decisions`, `test_a_same_tier_revision_is_listed_too` |
| AC-11 | `test_ac11_deadlines.py` | `test_the_deadline_is_exactly_the_window_after_assignment` (2, 10, 15 days), `test_a_stricter_revision_shortens_the_deadline_from_the_original_assignment`, `test_a_looser_revision_without_a_rationale_does_not_extend_the_deadline`, `test_a_looser_revision_with_a_rationale_extends_it_and_logs_the_rationale` |
| AC-12 | `test_ac12_reminders.py` | `test_the_instants_are_the_spec_table`, `test_reminders_fire_at_50_80_and_100_percent_and_never_early` (sink), `test_the_reminder_body_is_the_pinned_shape` (sink) |
| AC-14 | `test_ac14_role_matrix.py` | `test_role_matrix` (13 endpoints × no key, unknown key, system, viewer, reviewer), `test_a_non_reviewer_is_denied_both_actions_and_the_reviewer_is_not` (viewer, system) |
| AC-15 | `test_ac15_audit.py` | `test_a_view_only_action_is_logged_with_the_viewer_and_the_time` (Phase 3), `test_a_view_by_one_user_and_an_edit_by_another_are_both_logged` (Phase 4 edit) |
| AC-17 | `test_ac17_risk_gap.py` | `test_detection_and_the_assignment_deadline_are_both_visible_with_the_gap` |

Beside the criteria, `test_model5.py::test_model5_example[ex1|ex2|ex4]` (sink) runs the Model 5
fixtures and `test_demo.py::test_the_demo_walks_the_ten_steps` (sink) runs the demo, both
against the same stack.

What the HTTP runner cannot show, and where it is shown instead:

- **"(sink)" tests skip without `WEBHOOK_SINK_URL`.** The in-service run (`POST /v1/test/suite
  {"scope":"acceptance"}`) inherits the service's environment, so it skips them unless the
  service sets that variable. The compose `api` service sets it
  (`http://sink:8765/hook/suite`); a service started without it, and a "passed" from it, says
  nothing about AC-02's notification, AC-04, AC-12, Model 5 or the demo. In CI the image job's
  runner step fails if a sink is configured and any test skipped (decisions 143, 145).
- **AC-04's alternate bound** (for example 5 min) needs a restart with another
  `NOTIFY_MAX_DELAY_SECONDS`. The in-process worker tests cover it.
- **AC-06's direct modification** bypasses the application, which HTTP cannot do. The runner
  covers the export form (export, `tools/verify_ledger.py`, edit, verify again); the storage
  permissions and the direct edit are in-process (`tests/test_schema_permissions.py`,
  `tests/test_verify*.py`).
- **AC-09's on-screen options** are the manual half: the console offers exactly the four tiers.
- **AC-11 and AC-17 assume `DEADLINE_CALENDAR=calendar_days`** (the default), as README 10.7
  says for Part 1.
- **AC-14's "reviewer succeeds" for the report** is 409 before a disposition and 501 after
  (D1); the report itself is Phase 5.

### Phase 3 tests this part changed (Addendum 9.10)

`tests/api/test_platform.py` and `tests/part1/test_smoke.py` now send a reviewer or viewer key,
expect 204 on reset, `{simulated_time}` on advance (including zero seconds), and read gap events
through `GET /v1/events?type=monitoring_gap`.

### Integration-marked (Addendum rule 10)

`/healthz` with real `overdue_escalations`, the seed row against the real `harm_context` table,
`tests/acceptance`, the Model 5 runner, `tools/demo_phase4.py` and the console panel. They need
Parts 1–3.

## 14. Phase 5 Part 1: reports and submission

A reviewer asks for a report, the system assembles it once per disposition, stores the bytes
exactly as produced, and a reviewer can then record that it was submitted. Nothing is sent to an
authority. The code is in `app/reports.py` (assembly, storage, submission, listing),
`app/report_render.py` (JSON and PDF), `app/report_template.py` (the Article 73 field mapping)
and `app/api/report_routes.py`; the tables come from migration 0008. The decisions are
254 to 266 in `docs/decisions.md`. The Phase 5 specification is the source for everything here.

### 14.1 Endpoints

| Endpoint | Who | What it does |
| --- | --- | --- |
| GET /v1/events/{id}/report[?format=json\|pdf] | REVIEWER | The first request after a disposition assembles the report in one transaction (render JSON and PDF once, store both, append REPORT_ASSEMBLED). Every later request returns the stored bytes and writes nothing. Headers: `ETag`, `X-Report-Id`, `X-Report-Version`, `X-Report-Sha256`. |
| POST /v1/events/{id}/report/submission | REVIEWER | Body `{report_id, json_sha256, report_type, expected_next_report_date?, authority_reference?}`. 201, one REPORT_SUBMITTED entry. The event stays DISPOSITIONED. |
| GET /v1/events/{id}/reports | REVIEWER, VIEWER | The report rows, oldest first, without content: `status` is ASSEMBLED, SUBMITTED, SUPERSEDED or SUBMITTED_SUPERSEDED, derived when read. |
| GET /v1/reports/{report_id}[?format=pdf] | REVIEWER | The stored bytes of that exact version. Never assembles. |

Order of checks on the report: 401, 403, 400 MALFORMED_QUERY (`format` must be json or pdf), 404,
410 RECORD_ERASED, 409 NO_DISPOSITION, then 200. Submission: 401, 403, 400 MALFORMED_BODY, 404,
410, 409 NOT_SUBMITTABLE (a not-serious closure report), 409 REPORT_NOT_CURRENT, 409
REPORT_HASH_MISMATCH, 409 REPORT_ALREADY_SUBMITTED, 201. A revision adds a disposition row, so the
next report request assembles version 2; version 1 stays stored and can be submitted only while it
is current.

### 14.2 What a report contains

The JSON has the shape of `docs/contract/phase5/report.schema.json`: the event data and evidence
bundle, the four Article 3(49) harm fields, the reviewer's determination (tier, label, rationale,
deadline, revisions), the detection, determination and assembly timestamps (microsecond
resolution, so they are distinct), the ledger head at assembly, and `article73_template`, one
entry for every field of the Commission template v1.0.0 (SB-10407). A field the system holds is
`mapped` or `derivable` and has a value; every other field is listed by number and name as
`requires_reviewer_input` or `not_available` with a null value, never guessed. A NOT_SERIOUS
disposition gives a closure report marked `NOT FOR SUBMISSION` (JSON and every PDF page) that
cannot be submitted.

### 14.3 Known limits

- The report contains the rationale and the evidence text, which are personal data. Until Part 2's
  sealing lands, the stored bytes are plain; the code already calls `personal_data.seal_bytes` and
  `open_bytes`, so sealing needs no change here.
- Assembly holds the ledger append lock while the PDF is rendered (decision 258).
- The template is a Commission draft of 26 September 2025; if a final version exists, the mapping
  in `app/report_template.py` is redone.
