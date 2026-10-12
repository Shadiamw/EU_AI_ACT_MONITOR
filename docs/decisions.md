# Decisions

Settled decisions for Part 2 (ledger and audit). They are not to be reopened. Each records
something the artifacts (pod contract, addendum, spec) leave open, or how a settled rule is
applied.

## M1: canonical form and hashing

1. **U+0000 and lone surrogates are rejected.** A string anywhere in an entry (payload values,
   payload keys, actor_id, subject_id) that contains U+0000 or a lone surrogate raises
   `LedgerError`. Reason: PostgreSQL cannot store U+0000 in jsonb or text, and a lone surrogate
   has no UTF-8 encoding, so the pinned hash cannot be computed.

2. **Uncanonicalizable stored rows are a mismatch.** If a stored row cannot be canonicalized
   (for example, the owner put a float in a payload), verify treats it as a mismatch at that
   seq. Verify must never crash or return a 500 because of it. Implemented in M3.

3. **The standalone verifier agrees on that case.** `tools/verify_ledger.py` (M5) must give the
   same result as the server for a row that cannot be canonicalized. M5's
   cross-implementation tests include this case.

4. **Tamper tests respect the CHECK constraints.** M3's tamper tests for recorded_at and
   actor_id use values that pass the table's CHECK constraints, or disable the constraint the
   same way the tests disable the trigger.

5. **No empty identifiers.** Empty actor_id and empty subject_id are rejected. Callers pass
   null (None) for "no subject", never "".

6. **Large integers are accepted.** Integers above 2**53 are accepted in payloads. The M6
   README must note that a JavaScript re-verifier would lose precision on them.

7. **recorded_at handling.** recorded_at truncates microseconds to milliseconds (never rounds),
   and an aware non-UTC datetime is converted to UTC. A naive datetime is rejected.

Decisions 1, 5, 6 and 7 are also stated in the `app/ledger/canonical.py` docstring, and
decision 2 is noted there because it concerns how `LedgerError` from canonicalization is
handled.

## M2: append, ensure_seed and record_view

8. **append takes an ORM Session.** `ledger.append`, `ledger.ensure_seed` and
   `audit.record_view` take a SQLAlchemy 2.x ORM `Session`. Other parts must pass an ORM
   Session when they integrate.

9. **ensure_seed and record_view return None.** Only `append` returns the entry.

10. **No lock timeout in the ledger code.** append waits for the advisory lock without a
    timeout. The M6 README must tell Part 4 to set `lock_timeout` or `statement_timeout` at
    connection level, because one stuck transaction would block every append.

11. **No per-entry-type payload shape check.** The spec only requires a JSON object with the
    allowed value types, so for example a SEED payload without `systems` is accepted.

12. **Sandbox Docker daemon.** The sandbox's Docker daemon sometimes stops between sessions.
    This is not a code issue. The M6 README must say how to restart it.

## M3: verify core and tamper detection

13. **Check order.** Entries are read in ascending seq. For each entry: (1) seq: the first
    entry must be seq 1 and every later entry the previous seq + 1; (2) prev_hash equals the
    previous entry's hash (64 zeros for the first); (3) the hash recomputed from the stored
    fields, including the stored prev_hash, equals the stored hash. Verify stops at the first
    failure; entries_checked counts the entries examined including the bad one.

14. **first_mismatch is a string.** The result has exactly `valid`, `entries_checked` and
    `first_mismatch`, where first_mismatch is the seq as a string, or None when valid. The
    failed check is logged (WARNING on the `ledger` logger) but never part of the result.

15. **Uncanonicalizable rows.** A stored row that cannot be canonicalized (for example a jsonb
    float such as 1.5 or 1.0) fails check 3 as a mismatch at that seq and never raises
    (decision 2). jsonb stores 1e3 as the integer 1000, so that edit canonicalizes and is caught
    by the ordinary hash recompute instead.

16. **Known limits are tests.** `test_known_limit_deleting_the_last_row_is_not_detected` and
    `test_known_limit_forged_last_row_with_consistent_hash_is_not_detected` document addendum
    rule 12: both report valid by design until Phase 5.

17. **Verify takes no lock.** Verify reads with one ordinary SELECT in seq order as the
    application role. It writes nothing, takes no advisory lock and never waits for appends;
    an uncommitted append is simply not visible to it.

## M3 refinements

18. **Check 3 uses the stored prev_hash.** The hash recompute uses the row's stored prev_hash
    (with the other stored fields), so it is independent of the prev_hash link check. The M5
    standalone script must do the same.

19. **Degenerate rows.** For a non-integer or missing seq in a hand-edited export, the
    server's pure function reports the string form of what is in the file (for example
    `"True"` or `"None"`). The M5 script is not required to match that string exactly; it must
    never crash and must report the export invalid. Exact strings for degenerate rows are not
    part of the M5 cross-check.

20. **Verify memory.** Verify reads all rows into memory in one SELECT. This is fine for
    Phase 3; the M6 README gets a one-line scale note.

21. **The mismatch log format is relied on by tests.** M3 tests read which check failed from
    the WARNING text `ledger verify mismatch: seq=N check=...`. Later milestones must not
    change that format.

22. **jsonb 1e3.** jsonb stores 1e3 as the integer 1000, so that edit is caught by the hash
    check rather than as uncanonicalizable. Both give a mismatch at the same seq.

## M3 decisions A and B

23. **first_mismatch is the stored seq (decision A).** first_mismatch is the failing entry's
    stored seq as a string, as the spec says ("that entry's seq"). Removing the seq field from
    a row reports `"None"` (the degenerate case, decision 19). In the database, verify reads
    rows `ORDER BY` the stored seq. If row k's seq is edited to k+100, that row sorts to the
    end, so the first failure is the row after it: for a middle row k verify reports `"k+1"`
    (check=seq), and for the last row N edited to N+100 it reports `"N+100"` (check=seq).
    Both are detected. The in-memory matrix in `tests/test_verify_extra.py` keeps the rows in
    their original order and asserts the stored seq (`"k+100"`).

24. **A row missing any of the 8 fields is a mismatch (decision B).** A row missing any of
    seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash or hash is a
    mismatch at that seq. This includes a missing subject_id. The check order still applies,
    so the reported check is the first one that fails: a missing seq gives check=seq, a
    missing prev_hash gives check=prev_hash, a missing hash gives check=hash, and a missing
    content field (entry_type, actor_id, subject_id, payload or recorded_at) gives
    check=uncanonicalizable. A row with an explicit `"subject_id": null` stays valid. An
    unknown extra key in a row is ignored, because the hash covers only the six content
    fields.

## M4 decisions

25. **Commit inside the handler.** Each request handler commits explicitly before it returns,
    and rolls back on any exception. Commit is never done in a dependency's teardown code.
    Reason: teardown can run after the response is sent, so the next request could run before
    the previous AUDIT_VIEWED is committed.

26. **One transaction per request.** Each request uses one transaction at READ COMMITTED
    (isolation_level set on the engine), as monitor_app. Reason: append requires READ
    COMMITTED (addendum rule 5), and the endpoints must run with the application role's grants.

27. **Wrong method is 404.** A wrong HTTP method on a known route returns 404 NOT_FOUND with the
    contract error body. Reason: the spec has no code for 405 and no code is invented. Part 1
    should confirm this when it lands.

28. **Error mapping.** Unknown routes return 404 NOT_FOUND. FastAPI's default 422 is replaced
    by 400 MALFORMED_BODY. LedgerError and any unexpected exception return 500 INTERNAL_ERROR
    with a generic message. No stack trace, payload value or hash appears in a body or a log.
    Reason: shared rules (every error uses the contract body) and the ledger's no-leak rule.

29. **Auth stub.** The principal comes from one overridable resolver function whose default
    returns REVIEWER rev-1 (plan G3). No principal gives 401 MISSING_OR_INVALID_KEY. A role
    outside the allowed list gives 403 FORBIDDEN_ROLE. Check order is 401, then 403, then the
    404 lookup. Reason: the spec's check order, and tests need several actors.

30. **Event lookup stub (plan G1).** `events.exists` is true when the ledger holds an
    EVENT_FLAGGED or GAP_OPENED entry whose subject_id is that id. An unknown event gives 404
    and nothing is written. Reason: addendum rule 1 ("until Part 1 exists, use an entry created
    by append").

31. **Audit reads before it appends.** Audit builds its response before it appends
    AUDIT_VIEWED, so the response never contains its own view. The append and the commit
    happen before the response is returned. Reason: spec ("the next call does").

32. **Export and verify are read-only.** They write no ledger entry, take no advisory lock and
    never block appends. Reason: spec (no entry for an export) and decision 17.

33. **Verify response.** Verify returns exactly {valid, entries_checked, first_mismatch}, with
    first_mismatch a string or null. No head_seq or head_hash. Reason: addendum rule 6.

34. **The G2 route stays in tests.** The G2 test-only route lives only in tests/ and is never
    mounted by app/main.py. Reason: GET /v1/events/{id} belongs to Part 1.

35. **New read helpers in a new file.** New read helpers live in `app/ledger/read.py`.
    Existing M0 to M3 code and tests are not modified. Reason: keeps the verified M1 to M3 code
    unchanged.

### Decided during M4 implementation

36. **How errors are produced.** ApiError has its own handler. Starlette's 404 and 405 both
    map to 404 NOT_FOUND. Any other HTTP exception maps to 500 INTERNAL_ERROR; Part 2 raises
    none, and no code is invented for one. Every other exception, LedgerError included, is
    caught by a small ASGI middleware inside Starlette's ServerErrorMiddleware. It returns the
    500 body and logs only the exception's type name at ERROR on the `api` logger: never its
    message and never a traceback. Reason: one place for the 500 body, and nothing is
    re-raised to a server log with a traceback.

37. **Fixed error messages.** NOT_FOUND "resource not found", MISSING_OR_INVALID_KEY "missing
    or invalid API key", FORBIDDEN_ROLE "this role may not call this endpoint", MALFORMED_BODY
    "the request is malformed", INTERNAL_ERROR "an internal error occurred". Reason: tests
    compare exact bodies, and no message carries request or ledger data.

38. **Engine pool.** The application engine uses `pool_size=10`, `max_overflow=40` and
    `pool_pre_ping=True`. Reason: up to 40 requests run at once in FastAPI's default thread
    pool, and each holds one connection for its transaction. Part 4 sizes this against
    PostgreSQL's `max_connections`.

39. **Plain JSON responses.** The three endpoints return a `JSONResponse` built from plain
    dicts and lists, with no response model. Reason: no coercion of first_mismatch or of
    payload values. The stub app also turns off `/docs`, `/redoc` and `/openapi.json`, so the
    only routes are the three Part 2 endpoints.

40. **Read-only handlers also commit.** Export and verify end their (read-only) transaction
    with a commit before returning, the same as audit (decision 25). Reason: one rule for every
    handler.

41. **auth.py was extended, not rewritten.** The M4 scope adds `resolve_principal` and
    `require(*roles)` to the `app/auth.py` stub. `Principal` is unchanged. This is the one
    existing file M4 changes. Reason: scope item 2.

42. **Test client.** The endpoint tests call the app through httpx's ASGI transport, from a
    small synchronous wrapper around an anyio blocking portal. Reason: the installed Starlette
    deprecates its TestClient with httpx. Sync handlers still run in the thread pool, as under
    a real server. Tests pick the caller with an `X-Test-Actor` header, read by a test-only
    override of `resolve_principal`.

43. **Commit-before-return is tested at the ASGI layer.** A wrapper counts committed
    AUDIT_VIEWED rows on a separate owner connection when each audit response starts. Both a
    commit moved into the session dependency's teardown and a commit in a background task fail
    that test. Reason: an in-process client returns only after the whole app call, so the
    response body alone could not show a late commit.

44. **A view-only subject is not an event.** For the events stub, a subject_id with only
    EVENT_VIEWED or AUDIT_VIEWED entries, and no EVENT_FLAGGED or GAP_OPENED entry, gets 404.
    Reason: decision 30.

45. **Commit boundary.** Commit 1440749 (stubs) adds `app/main.py`, which imports
    `app.api.ledger_routes`; that module arrives in the next commit (968a603). Only 1440749 on
    its own cannot import `app.main`. No earlier commit was rewritten to fix this.

### M4 notes

- For the M6 README: the global append lock serializes every view (EVENT_VIEWED) and every
  audit call (AUDIT_VIEWED) with all other appends. Export shows every ledger entry to any
  VIEWER, including WEBHOOK_REGISTERED URLs, so webhook URLs must not carry secrets.
- For M5, the export format the script reads: a JSON array (empty ledger: `[]`) of every entry
  in ascending seq, each with exactly eight fields: `seq` (integer), `entry_type` (string),
  `actor_id` (string), `subject_id` (string or null), `payload` (object), `recorded_at`
  (string, `YYYY-MM-DDTHH:MM:SS.mmmZ`), `prev_hash` and `hash` (64 lowercase hex characters).
- Fix iteration 1 (a test bug). The mutation "verify writes a ledger entry" hung the run
  instead of failing. `test_export_and_verify_do_not_wait_for_a_held_append_lock` waited for
  the requests with `asyncio.wait_for` inside `asyncio.run`. On a timeout, `asyncio.run` still
  waited for the blocked worker thread, so the `finally` that rolls back the lock holder never
  ran. The test now runs the requests in a separate thread, waits up to 10 s, then always
  rolls back the holder and asserts that both requests finished while the lock was held. With
  the fix, that mutant fails 5 tests, and a new mutant (export takes the append lock) fails
  the test as well. No implementation change was needed.

### Stubs to replace when Part 1 lands

- `app/errors.py`: ApiError and the global error handlers.
- `app/auth.py`: the principal resolver and `require(*roles)`.
- `app/events.py`: `exists(session, event_id)`.
- `app/db.py`: engine and per-request session handling.
- `app/main.py`: the app factory, which mounts only the Part 2 router.
- The G2 test-only route in tests/.

## M4 close-out

46. **One branch for all work.** All Part 2 work, including M6's final push and its draft PR,
    uses the branch `development-ledger`. The plan's earlier branch name
    `claude/project-thread-3jnz6o` is superseded.

47. **M4 had 18 mutation checks.** The M4 prompt listed 16 mutations. The "audit commits after
    returning" mutation was run as two variants, and one extra mutation was added, for 18 in
    total. Every one made at least one new test fail, and each file was restored byte for
    byte. (The M4 report's phrase "the 17 you listed" counted the split commit mutation as two
    and should read "the 16 listed, with the commit mutation split in two".)
    1. Audit appends AUDIT_VIEWED before reading.
    2. Audit skips the AUDIT_VIEWED append.
    3. Audit does not filter by subject_id.
    4. Audit orders by seq descending.
    5. The 404 path still appends (and commits) AUDIT_VIEWED.
    6. Audit commits after returning: the commit moved into the session dependency's teardown.
    7. Audit commits after returning: the commit run as a background task.
    8. Export drops the hash field.
    9. Export orders by something other than seq (hash).
    10. Verify writes a ledger entry.
    11. Verify returns first_mismatch as an int.
    12. Verify adds an extra key (head_seq).
    13. The role check allows SYSTEM.
    14. 403 and 404 swapped: the event lookup runs before the role check.
    15. 401 and 403 swapped: a missing principal is treated as a wrong role.
    16. The unknown-route (404/405) handler is removed.
    17. The LedgerError mapping is removed (LedgerError is re-raised).
    18. Extra: export takes the append lock.

48. **Commit 1440749 does not import on its own.** It adds `app/main.py`, which imports
    `app.api`; that package only arrives in the next commit, 968a603. It was not rewritten. A
    squash merge of the final PR would remove the issue.

49. **The commit-before-return guard.** `test_each_audit_view_is_committed_before_the_response`
    in `tests/api/test_audit.py` is the only test that catches a commit made after the
    response has started (decisions 25 and 43). It carries a comment saying it must not be
    deleted or weakened. In the close-out, mutations 6 and 7 were each run 5 times, and the
    guard failed all 10 times. The check is deterministic: the row count runs inside the ASGI
    send call for the response start, before the response can proceed, so it does not depend
    on timing.

50. **Real-server check (close-out, not a test).** A throwaway script outside the repo started
    uvicorn 0.30.6 with `app.main:app` against the test database as monitor_app (default stub
    principal rev-1). Over real HTTP: 50 sequential audit calls each returned exactly n-1
    AUDIT_VIEWED items. 20 concurrent audit calls from threads all returned 200.
    `GET /v1/ledger/verify` returned `{"valid":true,"entries_checked":71,"first_mismatch":null}`
    (1 + 50 + 20 entries). `GET /v1/ledger/export` returned 71 entries with contiguous seqs,
    and every hash recomputed and linked. uvicorn was installed outside the project for this
    check only; it is not a project dependency yet.

51. **Index on subject_id.** Migration 0002 already creates `ix_ledger_entry_subject_seq` on
    `(subject_id, seq)`, so the audit lookup uses an index scan. With 20,000 rows (1,000 events
    of 20 entries each), `events.exists` took a median of 0.42 ms and `read.audit_trail` a
    median of 0.50 ms over 50 runs, measured once in the sandbox. This is report-only, with no
    timing assertion in any test. No scale note is needed for the audit lookup. The event
    lookup stub (decision 30) also filters on subject_id, so it uses the same index.

### Open items for integration

- A wrong HTTP method on a known route returns 404 NOT_FOUND (decision 27, item 3 of the M4
  decisions). Part 1 must confirm this.
- The stubs to replace when Part 1 lands are listed under "Stubs to replace when Part 1
  lands" above.
- Part 4 sets `lock_timeout` or `statement_timeout` at connection level (decision 10, the M2
  item on lock timeouts).
- Real-server result (decision 50): over real HTTP under uvicorn, the commit-before-return
  behavior, concurrency, verify and export all held.
- Index finding (decision 51): `(subject_id, seq)` is indexed, and the audit lookup takes about
  0.5 ms at 20,000 rows.

## Carry-forward checklist

M5 (standalone verifier):
- [ ] The script recomputes the hash from the stored prev_hash.
- [ ] The script never crashes on malformed rows.
- [ ] The jsonb float edit and the 1e3 edit give the same mismatch seq on the script and the
      server.
- [ ] The script and the server agree on every M3 tamper case.
- [ ] The script sorts rows by stored seq before checking, so it matches the server's
      `ORDER BY seq` (decision 23).
- [ ] A row missing any of the 8 fields is a mismatch at that seq, including a missing
      subject_id; a missing content field reports as uncanonicalizable (decision 24).
- [ ] An explicit `"subject_id": null` is valid (decision 24).
- [ ] An unknown extra key in a row is ignored (decision 24).
- [ ] The script reads the M4 export format (M4 notes): an array of 8-field entries in seq
      order.

M6 README:
- [ ] The global append lock serializes every view and audit call (M4 notes).
- [ ] Export shows every entry to any VIEWER, so webhook URLs must not carry secrets.
- [ ] Size the application engine pool against PostgreSQL's max_connections (decision 38).
- [ ] Part 4 sets `lock_timeout` or `statement_timeout` at connection level.
- [ ] How to restart the Docker daemon.
- [ ] The 2**53 JavaScript precision note.
- [ ] The verify memory scale note.
- [ ] The end-of-ledger truncation limit and the forged-last-row limit.
- [ ] The ORM Session handle type.
- [ ] No per-entry-type payload shape check.
- [ ] The verify check order.

M6 also owns:
- [ ] `docs/ac06_storage_evidence.md`.
