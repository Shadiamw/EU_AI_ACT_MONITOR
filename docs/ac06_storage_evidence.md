# AC-06 storage evidence: ledger grants and trigger

This is the manual half of AC-06: the storage-layer write permissions (addendum rule 1: "record
the table grants and the trigger definition"). The automated half is the hash-chain tamper
tests (section 5).

How this was produced (decision 72):

- Both setups were used: the `postgres:16` Docker container on port 5432, and a native
  PostgreSQL 16.15 server on port 5433.
- On each, the roles came from `tests/sql/bootstrap_roles.sql`, and the schema was rebuilt with
  `alembic downgrade base` then `alembic upgrade head`, from the repository root.
- Every output below is the raw, unedited terminal output of the command shown above it.
- `psql -a` echoes each statement before its result. `[exit code N]` is the command's exit
  status, appended by the capture script.
- Captured on 2026-10-13 against commit `0a92692` (app/ and migrations/
  identical to e5b6973).

## 1. Container (postgres:16, port 5432)

```text
$ export DATABASE_URL=postgresql+psycopg://monitor_app:monitor_app@localhost:5432/monitor_test
$ export DATABASE_ADMIN_URL=postgresql+psycopg://monitor_owner:monitor_owner@localhost:5432/monitor_test

$ .venv/bin/alembic downgrade base
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running downgrade 0002 -> 0001, Ledger: ledger_entry table, permission grants and revokes, UPDATE/DELETE-blocking trigger.
INFO  [alembic.runtime.migration] Running downgrade 0001 -> , STUB for Part 4: platform tables and database roles.
[exit code 0]

$ .venv/bin/alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, STUB for Part 4: platform tables and database roles.
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, Ledger: ledger_entry table, permission grants and revokes, UPDATE/DELETE-blocking trigger.
[exit code 0]

$ .venv/bin/alembic current
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
0002 (head)
[exit code 0]

$ psql -X -a "postgresql://postgres:postgres@localhost:5432/monitor_test" <<'SQL'
SELECT version();
\dp ledger_entry
SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid = 'ledger_entry'::regclass AND NOT tgisinternal;
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname IN ('monitor_owner', 'monitor_app') ORDER BY rolname;
\du monitor_owner
\du monitor_app
SQL
SELECT version();
                                                       version                                                        
----------------------------------------------------------------------------------------------------------------------
 PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit
(1 row)

\dp ledger_entry
                                         Access privileges
 Schema |     Name     | Type  |          Access privileges          | Column privileges | Policies 
--------+--------------+-------+-------------------------------------+-------------------+----------
 public | ledger_entry | table | monitor_owner=arwdDxt/monitor_owner+|                   | 
        |              |       | monitor_app=ar/monitor_owner        |                   | 
(1 row)

SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid = 'ledger_entry'::regclass AND NOT tgisinternal;
                                                                            pg_get_triggerdef                                                                             
--------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 CREATE TRIGGER ledger_entry_block_update_delete BEFORE DELETE OR UPDATE ON public.ledger_entry FOR EACH STATEMENT EXECUTE FUNCTION ledger_entry_block_update_delete_fn()
(1 row)

SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname IN ('monitor_owner', 'monitor_app') ORDER BY rolname;
    rolname    | rolsuper | rolcreaterole | rolcreatedb | rolbypassrls | rolcanlogin 
---------------+----------+---------------+-------------+--------------+-------------
 monitor_app   | f        | f             | f           | f            | t
 monitor_owner | f        | f             | f           | f            | t
(2 rows)

\du monitor_owner
       List of roles
   Role name   | Attributes 
---------------+------------
 monitor_owner | 

\du monitor_app
      List of roles
  Role name  | Attributes 
-------------+------------
 monitor_app | 

[exit code 0]
```

## 2. Native PostgreSQL 16.15 (port 5433)

```text
$ export DATABASE_URL=postgresql+psycopg://monitor_app:monitor_app@localhost:5433/monitor_test
$ export DATABASE_ADMIN_URL=postgresql+psycopg://monitor_owner:monitor_owner@localhost:5433/monitor_test

$ .venv/bin/alembic downgrade base
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running downgrade 0002 -> 0001, Ledger: ledger_entry table, permission grants and revokes, UPDATE/DELETE-blocking trigger.
INFO  [alembic.runtime.migration] Running downgrade 0001 -> , STUB for Part 4: platform tables and database roles.
[exit code 0]

$ .venv/bin/alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, STUB for Part 4: platform tables and database roles.
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, Ledger: ledger_entry table, permission grants and revokes, UPDATE/DELETE-blocking trigger.
[exit code 0]

$ .venv/bin/alembic current
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
0002 (head)
[exit code 0]

$ psql -X -a "postgresql://postgres:postgres@localhost:5433/monitor_test" <<'SQL'
SELECT version();
\dp ledger_entry
SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid = 'ledger_entry'::regclass AND NOT tgisinternal;
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname IN ('monitor_owner', 'monitor_app') ORDER BY rolname;
\du monitor_owner
\du monitor_app
SQL
SELECT version();
                                                                 version                                                                  
------------------------------------------------------------------------------------------------------------------------------------------
 PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1) on x86_64-pc-linux-gnu, compiled by gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0, 64-bit
(1 row)

\dp ledger_entry
                                         Access privileges
 Schema |     Name     | Type  |          Access privileges          | Column privileges | Policies 
--------+--------------+-------+-------------------------------------+-------------------+----------
 public | ledger_entry | table | monitor_owner=arwdDxt/monitor_owner+|                   | 
        |              |       | monitor_app=ar/monitor_owner        |                   | 
(1 row)

SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid = 'ledger_entry'::regclass AND NOT tgisinternal;
                                                                            pg_get_triggerdef                                                                             
--------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 CREATE TRIGGER ledger_entry_block_update_delete BEFORE DELETE OR UPDATE ON public.ledger_entry FOR EACH STATEMENT EXECUTE FUNCTION ledger_entry_block_update_delete_fn()
(1 row)

SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname IN ('monitor_owner', 'monitor_app') ORDER BY rolname;
    rolname    | rolsuper | rolcreaterole | rolcreatedb | rolbypassrls | rolcanlogin 
---------------+----------+---------------+-------------+--------------+-------------
 monitor_app   | f        | f             | f           | f            | t
 monitor_owner | f        | f             | f           | f            | t
(2 rows)

\du monitor_owner
       List of roles
   Role name   | Attributes 
---------------+------------
 monitor_owner | 

\du monitor_app
      List of roles
  Role name  | Attributes 
-------------+------------
 monitor_app | 

[exit code 0]
```

## 3. Demonstration (scratch database, not the working one)

This is a live session in a throwaway database, `ledger_scratch`, built by the same migrations
and dropped at the end:

- As `monitor_app`, INSERT and SELECT succeed, and UPDATE, DELETE and TRUNCATE are refused.
- As `monitor_owner`, UPDATE and DELETE are refused by the trigger, and TRUNCATE succeeds.

The row inserted is a placeholder, not a real chain entry.

### 3.1 Container

```text
$ psql -X -a "postgresql://postgres:postgres@localhost:5432/postgres" <<'SQL'
CREATE DATABASE ledger_scratch OWNER monitor_owner;
SQL
CREATE DATABASE ledger_scratch OWNER monitor_owner;
CREATE DATABASE
[exit code 0]

$ psql -X -a "postgresql://postgres:postgres@localhost:5432/ledger_scratch" <<'SQL'
ALTER SCHEMA public OWNER TO monitor_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO monitor_app;
SQL
ALTER SCHEMA public OWNER TO monitor_owner;
ALTER SCHEMA
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE
GRANT USAGE ON SCHEMA public TO monitor_app;
GRANT
[exit code 0]

$ export DATABASE_URL=postgresql+psycopg://monitor_app:monitor_app@localhost:5432/ledger_scratch
$ export DATABASE_ADMIN_URL=postgresql+psycopg://monitor_owner:monitor_owner@localhost:5432/ledger_scratch

$ .venv/bin/alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, STUB for Part 4: platform tables and database roles.
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, Ledger: ledger_entry table, permission grants and revokes, UPDATE/DELETE-blocking trigger.
[exit code 0]

$ psql -X -a "postgresql://monitor_app:monitor_app@localhost:5432/ledger_scratch" <<'SQL'
SELECT current_user;
INSERT INTO ledger_entry (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash) VALUES (1, 'SEED', 'system', NULL, '{"systems": ["sys-42"], "threshold_count": "2"}', '2026-09-27T10:00:00.000Z', repeat('0', 64), repeat('a', 64));
SELECT seq, entry_type, actor_id FROM ledger_entry;
UPDATE ledger_entry SET actor_id = 'intruder' WHERE seq = 1;
DELETE FROM ledger_entry WHERE seq = 1;
TRUNCATE ledger_entry;
SQL
SELECT current_user;
 current_user 
--------------
 monitor_app
(1 row)

INSERT INTO ledger_entry (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash) VALUES (1, 'SEED', 'system', NULL, '{"systems": ["sys-42"], "threshold_count": "2"}', '2026-09-27T10:00:00.000Z', repeat('0', 64), repeat('a', 64));
INSERT 0 1
SELECT seq, entry_type, actor_id FROM ledger_entry;
 seq | entry_type | actor_id 
-----+------------+----------
   1 | SEED       | system
(1 row)

UPDATE ledger_entry SET actor_id = 'intruder' WHERE seq = 1;
ERROR:  permission denied for table ledger_entry
DELETE FROM ledger_entry WHERE seq = 1;
ERROR:  permission denied for table ledger_entry
TRUNCATE ledger_entry;
ERROR:  permission denied for table ledger_entry
[exit code 0]

$ psql -X -a "postgresql://monitor_owner:monitor_owner@localhost:5432/ledger_scratch" <<'SQL'
SELECT current_user;
UPDATE ledger_entry SET actor_id = 'intruder' WHERE seq = 1;
DELETE FROM ledger_entry WHERE seq = 1;
SELECT count(*) FROM ledger_entry;
TRUNCATE ledger_entry;
SELECT count(*) FROM ledger_entry;
SQL
SELECT current_user;
 current_user  
---------------
 monitor_owner
(1 row)

UPDATE ledger_entry SET actor_id = 'intruder' WHERE seq = 1;
ERROR:  ledger_entry is append-only: UPDATE is not allowed
CONTEXT:  PL/pgSQL function ledger_entry_block_update_delete_fn() line 3 at RAISE
DELETE FROM ledger_entry WHERE seq = 1;
ERROR:  ledger_entry is append-only: DELETE is not allowed
CONTEXT:  PL/pgSQL function ledger_entry_block_update_delete_fn() line 3 at RAISE
SELECT count(*) FROM ledger_entry;
 count 
-------
     1
(1 row)

TRUNCATE ledger_entry;
TRUNCATE TABLE
SELECT count(*) FROM ledger_entry;
 count 
-------
     0
(1 row)

[exit code 0]

$ psql -X -a "postgresql://postgres:postgres@localhost:5432/postgres" <<'SQL'
DROP DATABASE ledger_scratch;
SELECT datname FROM pg_database WHERE datname = 'ledger_scratch';
SQL
DROP DATABASE ledger_scratch;
DROP DATABASE
SELECT datname FROM pg_database WHERE datname = 'ledger_scratch';
 datname 
---------
(0 rows)

[exit code 0]
```

### 3.2 Native

```text
$ psql -X -a "postgresql://postgres:postgres@localhost:5433/postgres" <<'SQL'
CREATE DATABASE ledger_scratch OWNER monitor_owner;
SQL
CREATE DATABASE ledger_scratch OWNER monitor_owner;
CREATE DATABASE
[exit code 0]

$ psql -X -a "postgresql://postgres:postgres@localhost:5433/ledger_scratch" <<'SQL'
ALTER SCHEMA public OWNER TO monitor_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO monitor_app;
SQL
ALTER SCHEMA public OWNER TO monitor_owner;
ALTER SCHEMA
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE
GRANT USAGE ON SCHEMA public TO monitor_app;
GRANT
[exit code 0]

$ export DATABASE_URL=postgresql+psycopg://monitor_app:monitor_app@localhost:5433/ledger_scratch
$ export DATABASE_ADMIN_URL=postgresql+psycopg://monitor_owner:monitor_owner@localhost:5433/ledger_scratch

$ .venv/bin/alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, STUB for Part 4: platform tables and database roles.
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, Ledger: ledger_entry table, permission grants and revokes, UPDATE/DELETE-blocking trigger.
[exit code 0]

$ psql -X -a "postgresql://monitor_app:monitor_app@localhost:5433/ledger_scratch" <<'SQL'
SELECT current_user;
INSERT INTO ledger_entry (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash) VALUES (1, 'SEED', 'system', NULL, '{"systems": ["sys-42"], "threshold_count": "2"}', '2026-09-27T10:00:00.000Z', repeat('0', 64), repeat('a', 64));
SELECT seq, entry_type, actor_id FROM ledger_entry;
UPDATE ledger_entry SET actor_id = 'intruder' WHERE seq = 1;
DELETE FROM ledger_entry WHERE seq = 1;
TRUNCATE ledger_entry;
SQL
SELECT current_user;
 current_user 
--------------
 monitor_app
(1 row)

INSERT INTO ledger_entry (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash) VALUES (1, 'SEED', 'system', NULL, '{"systems": ["sys-42"], "threshold_count": "2"}', '2026-09-27T10:00:00.000Z', repeat('0', 64), repeat('a', 64));
INSERT 0 1
SELECT seq, entry_type, actor_id FROM ledger_entry;
 seq | entry_type | actor_id 
-----+------------+----------
   1 | SEED       | system
(1 row)

UPDATE ledger_entry SET actor_id = 'intruder' WHERE seq = 1;
ERROR:  permission denied for table ledger_entry
DELETE FROM ledger_entry WHERE seq = 1;
ERROR:  permission denied for table ledger_entry
TRUNCATE ledger_entry;
ERROR:  permission denied for table ledger_entry
[exit code 0]

$ psql -X -a "postgresql://monitor_owner:monitor_owner@localhost:5433/ledger_scratch" <<'SQL'
SELECT current_user;
UPDATE ledger_entry SET actor_id = 'intruder' WHERE seq = 1;
DELETE FROM ledger_entry WHERE seq = 1;
SELECT count(*) FROM ledger_entry;
TRUNCATE ledger_entry;
SELECT count(*) FROM ledger_entry;
SQL
SELECT current_user;
 current_user  
---------------
 monitor_owner
(1 row)

UPDATE ledger_entry SET actor_id = 'intruder' WHERE seq = 1;
ERROR:  ledger_entry is append-only: UPDATE is not allowed
CONTEXT:  PL/pgSQL function ledger_entry_block_update_delete_fn() line 3 at RAISE
DELETE FROM ledger_entry WHERE seq = 1;
ERROR:  ledger_entry is append-only: DELETE is not allowed
CONTEXT:  PL/pgSQL function ledger_entry_block_update_delete_fn() line 3 at RAISE
SELECT count(*) FROM ledger_entry;
 count 
-------
     1
(1 row)

TRUNCATE ledger_entry;
TRUNCATE TABLE
SELECT count(*) FROM ledger_entry;
 count 
-------
     0
(1 row)

[exit code 0]

$ psql -X -a "postgresql://postgres:postgres@localhost:5433/postgres" <<'SQL'
DROP DATABASE ledger_scratch;
SELECT datname FROM pg_database WHERE datname = 'ledger_scratch';
SQL
DROP DATABASE ledger_scratch;
DROP DATABASE
SELECT datname FROM pg_database WHERE datname = 'ledger_scratch';
 datname 
---------
(0 rows)

[exit code 0]
```

## 4. How to read it, and what it proves

**Privilege string.** In `\dp`, each line of "Access privileges" reads `grantee=privileges/grantor`.
The PostgreSQL 16 letters are:

| Letter | Privilege |
|---|---|
| a | INSERT ("append") |
| r | SELECT ("read") |
| w | UPDATE ("write") |
| d | DELETE |
| D | TRUNCATE |
| x | REFERENCES |
| t | TRIGGER |

So `monitor_app=ar/monitor_owner` means the application role holds exactly INSERT and SELECT,
granted by the owner. `monitor_owner=arwdDxt/monitor_owner` is the owner's full set. There is no
line without a name before `=`, so PUBLIC has nothing.

**What the evidence proves:**

- The application role (the user in `DATABASE_URL`) can only insert and read ledger rows. It
  cannot update, delete or truncate them: the demonstration shows "permission denied" for each.
- The trigger `ledger_entry_block_update_delete` is `BEFORE DELETE OR UPDATE ... FOR EACH
  STATEMENT`. It refuses UPDATE and DELETE for every role, the owner included. The demonstration
  shows the owner's UPDATE and DELETE rejected with "ledger_entry is append-only".
- Neither role is a superuser, so neither bypasses these grants. In the role output,
  `rolsuper = f` for both, and `\du` lists no attributes.

**What it does not prove:**

- **The owner can TRUNCATE the ledger.** There is deliberately no TRUNCATE trigger, because test
  reset truncates as the owner (addendum rule 1), and the demonstration shows it succeeding.
  Removing every entry, or entries from the end, is not detected by the chain alone until
  Phase 5 (decision 16; addendum rule 12). Protecting the owner credentials is outside this
  evidence.
- **Edits to other tables, such as Part 1's event table,** are not covered by the ledger's
  grants or chain (addendum rule 12).
- **Tamper detection itself.** These grants make tampering hard, but showing that a direct edit
  is detected is the job of the hash-chain tests.

## 5. The automated tests assert the same facts

`tests/test_schema_permissions.py` runs on every test run and checks the same facts as the
captured output:

| Captured fact | Test |
|---|---|
| `monitor_app=ar`: INSERT and SELECT only | `test_app_role_has_insert_and_select_only`, `test_app_role_can_insert_and_select` |
| No PUBLIC privileges | `test_public_has_no_privileges` |
| The application role's UPDATE, DELETE and TRUNCATE are refused with a permission error | `test_app_role_cannot_update_delete_or_truncate` |
| The owner's UPDATE and DELETE are refused by the trigger ("ledger_entry is append-only") | `test_owner_update_and_delete_fail_while_trigger_enabled` |
| One trigger, `BEFORE DELETE OR UPDATE ON public.ledger_entry FOR EACH STATEMENT`, no TRUNCATE, enabled | `test_trigger_definition_blocks_update_and_delete_but_not_truncate` |
| The owner can TRUNCATE | `test_owner_can_truncate` |
| Neither role is a superuser | `migrated_db` in `tests/conftest.py`, which refuses to run otherwise |

The hash-chain half of AC-06 (a direct payload edit is caught by verify) is
`tests/test_verify.py::test_ac06_payload_tamper_is_detected` and
`tests/api/test_export_verify.py::test_ac06_payload_tamper_through_the_api`.
