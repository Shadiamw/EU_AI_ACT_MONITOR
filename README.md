# EU_AI_ACT_MONITOR

Compliance monitoring system. This repository currently implements **Part 2: ledger and audit**
(the append-only, SHA-256 hash-chained ledger). Parts 1, 3 and 4 are stubbed where Part 2 depends
on them.

## Development setup

Requirements: Python 3.12 and PostgreSQL 16. Tests always run against a real PostgreSQL 16
server, never SQLite.

### 1. Python environment

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

### 2. PostgreSQL 16

Use either option. Both have been checked with the test suite.

**Option A: Docker container (preferred, matches CI)**

```sh
docker run -d --name ledger-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
```

If the Docker daemon is not running (for example in a sandbox), start it first with
`sudo dockerd &`.

**Option B: native PostgreSQL 16 server (fallback)**

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

### 3. Roles and test database (once per server)

`tests/sql/bootstrap_roles.sql` creates two roles and the `monitor_test` database. It stands in
for Part 4's migration 0001, which owns the real roles:

| Role | Used by | Ledger privileges |
|---|---|---|
| `monitor_owner` | migrations and test-only reset (`DATABASE_ADMIN_URL`) | owner |
| `monitor_app` | the application (`DATABASE_URL`) | `INSERT`, `SELECT` only |

Neither role is a superuser, because a superuser bypasses the grants under test.

```sh
psql "postgresql://postgres:postgres@localhost:5432/postgres" -f tests/sql/bootstrap_roles.sql
```

### 4. Environment

The test suite defaults to these values, so they only need setting for another server or port:

```sh
export DATABASE_URL=postgresql+psycopg://monitor_app:monitor_app@localhost:5432/monitor_test
export DATABASE_ADMIN_URL=postgresql+psycopg://monitor_owner:monitor_owner@localhost:5432/monitor_test
export ENVIRONMENT=test
```

Migration 0002 grants ledger privileges to the user named in `DATABASE_URL`, and refuses to run if
that user is the owner role.

### 5. Run the checks

```sh
.venv/bin/pytest            # rebuilds the schema (alembic downgrade base, upgrade head) each run
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
```

To apply migrations by hand: `.venv/bin/alembic upgrade head` (runs as `DATABASE_ADMIN_URL`).

## The ledger table (migration 0002)

- `ledger_entry(seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash)`,
  primary key `seq`, unique `prev_hash`, `payload` stored as `jsonb`, `recorded_at` stored as text.
- The application role has `INSERT` and `SELECT` only; `UPDATE`, `DELETE` and `TRUNCATE` are
  revoked, and `PUBLIC` has nothing.
- Trigger `ledger_entry_block_update_delete` (`BEFORE UPDATE OR DELETE … FOR EACH STATEMENT`)
  raises on any update or delete, for every role including the owner. It stays enabled in
  production. Tamper tests disable it as the owner, edit, and re-enable it in one transaction.
- There is no `TRUNCATE` trigger, because test reset truncates the ledger as the owner.
