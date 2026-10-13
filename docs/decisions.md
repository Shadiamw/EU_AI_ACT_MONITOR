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

## M5 decisions

52. **Output.** The script prints exactly one JSON object on stdout, with exactly the keys
    `valid` (bool), `entries_checked` (int) and `first_mismatch` (string or null). Nothing else
    goes to stdout. Diagnostics go to stderr only. Reason: the output must match the server's
    three fields (addendum rule 6) and be machine-readable.

53. **Exit codes.** 0 for valid, 1 for invalid, 2 for a command-line usage error (a missing or
    extra argument; a design choice, not from the spec). The one argument is the path to an
    export file. Reason: the spec fixes 0 and 1; 2 keeps usage errors apart from results.

54. **Unusable input.** If the file is missing, unreadable, not valid UTF-8, not valid JSON,
    not a JSON array, or an array with an element that is not an object, the script prints
    `{"valid": false, "entries_checked": 0, "first_mismatch": null}`, writes the reason to
    stderr and exits 1. An empty array is a valid empty ledger:
    `{"valid": true, "entries_checked": 0, "first_mismatch": null}`, exit 0. Reason: an
    export that cannot be read must never pass, and an empty ledger is valid (addendum
    rule 11).

55. **Independence.** The script imports nothing from app/ and shares no code with verify.py
    or canonical.py. It is written from the spec, the addendum and this file. If the
    cross-implementation tests disagree with the server, the disagreement is settled by
    reasoning from the spec and the addendum; if that does not settle it, work stops and the
    question goes to the user. Reason: two implementations that share code would share bugs.

56. **Number parsing.** Any JSON number that is not an integer (including 1.0, 1e3 and 1.5),
    and NaN, Infinity and -Infinity, anywhere in a row, makes that row uncanonicalizable.
    Through the database, jsonb turns 1e3 into the integer 1000, so there it is caught by the
    hash check instead. Both report the same seq (decision 22). Reason: addendum rule 2 allows
    integers only.

57. **Strings.** A string containing U+0000 or a lone surrogate, anywhere in a row, makes the
    row uncanonicalizable, the same as the server (decision 1). It must never crash the
    script. Reason: such a string has no canonical UTF-8 form.

58. **Stored-field rules.** The script recomputes each hash from the stored prev_hash. It sorts
    rows by stored seq before checking. A row missing any of the six content fields is
    uncanonicalizable. An unknown extra key is ignored. An explicit `"subject_id": null` is
    valid. The check order is seq, then prev_hash, then hash (decisions 18, 23 and 24).
    Reason: the script must match the server's checks exactly.

59. **Degenerate rows.** For rows with a missing, boolean or non-integer seq, the script must
    report invalid and never crash. The exact first_mismatch string for these rows is not part
    of the cross-check (decision 19). Reason: the database cannot hold such rows, so there is
    no server value to match.

60. **stderr format.** For a mismatch the script writes the line
    `ledger verify mismatch: seq=N check=NAME` to stderr, where NAME is one of seq, prev_hash,
    hash or uncanonicalizable, the same names the server logs (decision 21). It never writes a
    payload value or a stored hash. Reason: tests compare the script's check names with the
    server's.

61. **Standard library only.** The script runs on the Python standard library only. It is
    written to run on Python 3.9 or newer where this costs nothing, but it is tested on 3.12
    only, and reports must say so. Reason: the other team must be able to run it anywhere.

## M5

### Decided during M5 implementation

62. **Where "anywhere in a row" applies.** A non-integer number, NaN, Infinity, U+0000 or a
    lone surrogate makes a row uncanonicalizable when it is inside one of the six content
    fields. In prev_hash or hash, such a value fails that field's own check (check=prev_hash or
    check=hash). In an unknown extra key it is ignored. Reason: decisions 56 and 57 say
    "anywhere in a row", but decisions 24 and 58 say unknown extra keys are ignored, and the
    server only canonicalizes the six content fields. Settled by reasoning from the spec (the
    hash covers only the six content fields), as decision 55 directs. The cross-implementation
    and differential tests agree with the server on it.

63. **A usage error prints nothing on stdout.** With no argument or more than one, the script
    writes the usage line to stderr and exits 2, with an empty stdout. Reason: decision 53 keeps
    usage errors apart from results, and a result line would report a check that never ran.

64. **Rows without a usable seq sort first.** Before checking, rows are sorted by stored seq.
    A row whose seq is missing, a boolean, a string, a non-integer number or null sorts before
    every integer seq, in file order, so verification stops at it with check=seq and
    entries_checked 1. Reason: sorting must never crash on such rows, and decision 59 requires
    only that they are reported invalid.

65. **JSON parsing limits.** The script parses with Python's `json` module.
    - For a duplicated key, the last value wins.
    - These count as unusable input (decision 54), with the reason on stderr:
      - a UTF-8 byte order mark;
      - an integer literal longer than Python's 4300-digit conversion limit;
      - nesting too deep to parse.
    - A row nested too deep to canonicalize is uncanonicalizable.
    - `--help` and `-h` are treated as file paths. The script has no options.

    Reason: unusable input must never pass or crash (decision 54). The script must stay on the
    standard library (decision 61).

66. **tools/__init__.py.** An empty `tools/__init__.py` lets the tests import the script as
    `tools.verify_ledger`. The script itself needs no package, and runs as a file under
    `python -I -S` from any directory. Reason: the in-process tests call `main()` directly, and
    the subprocess tests run the file in isolated mode.

67. **Differential test inputs.** `tests/test_verify_differential.py` uses seed 20261012 and
    300 random chains.
    - The server's `verify_entries` gets the rows sorted by stored seq, mirroring the database's
      `ORDER BY seq` (decision 23). The script gets them shuffled.
    - The test compares the full result and the failed check's name.
    - For cases with a degenerate seq, it compares only `valid` (decision 59).

    Reason: the server's pure function expects rows in seq order, while the script must sort
    them itself (decision 58).

68. **Mutation checks.** All 17 mutations listed for STEP 4 were run against the three M5 test
    files, with the script restored byte for byte after each (sha256 prefix 06e89a9b32cec1eb).
    - 16 made at least one new test fail.
    - "Recompute the hash from the previous row's hash instead of the stored prev_hash" made no
      test fail. It is an equivalent mutant. Check 2 returns prev_hash unless the stored
      prev_hash equals the previous row's hash, so by the time check 3 runs, the two values are
      the same string. No input can tell the mutant from the original.
    - A higher-order mutant was added to show that the property is still guarded. It removes
      the prev_hash link check AND recomputes from the previous row's hash, and it makes 4
      tests fail.
    - How the equivalent mutant is handled is settled by decision 70.

69. **ruff formats Python inside Markdown.** ruff 0.17 checks Python code blocks in `.md`
    files, so `ruff format --check .` covers `integration_notes.md`. Its example was formatted
    in a separate commit. Reason: pass criterion 4 (ruff clean) covers the whole repository.

70. **Mutant 6 is equivalent; the hash check now receives only the row.**
    - **Proof of equivalence.** In the pipeline as first written, the mutant "recompute the
      hash from the previous row's hash instead of the stored prev_hash" is equivalent. Check 2
      returns prev_hash unless the row's stored prev_hash equals the previous row's hash, so
      whenever check 3 runs, the two are the same string. No input can tell the mutant from the
      original.
    - **Higher-order mutant.** Removing the link check AND recomputing from the previous row's
      hash made 4 tests fail. This shows the stored-prev_hash rule matters as soon as the link
      check is gone.
    - **The refactor.** Chosen during M5 review: the recompute moved into its own function,
      `hash_check(row)` in `tools/verify_ledger.py`.
      - It receives only the row and uses the row's own stored prev_hash. The check order seq,
        then prev_hash, then hash is unchanged (decision 58).
      - Every output, exit code and stderr line is byte-identical to the previous version. A
        throwaway comparison of the old and new scripts on 425 inputs found 0 differences.
      - The mutant is now structurally inexpressible: `previous_hash` is not in scope inside
        `hash_check`. Written as before, it fails ruff (F821, undefined name) and crashes with
        a NameError (61 tests fail).
    - **The new direct tests.** These tests in `tests/test_verify_script.py` call `hash_check`
      on single rows and guard the stored-prev_hash rule:
      - (a) A row consistent with its own stored prev_hash passes, even when that prev_hash is
        not the real predecessor's hash.
      - (b) Changing only the stored prev_hash flips the result.
      - (c) A missing content field, an uncanonicalizable value, or an unusable prev_hash is a
        mismatch.
      - A signature test pins the single `row` parameter.
      - The closest expressible form of the mutant passes the previous row's hash into
        `hash_check`. It is caught by these new tests only (6 failed). Recomputing from the
        genesis value or from the stored hash is caught too.
    - **Pass criterion 3 (M5 STEP 6) now reads:** "every non-equivalent mutant is caught;
      equivalent mutants are documented with proof".

    Reason: chosen during M5 review, to restructure and to keep the equivalence proof on record.

### M5 notes

- `integration_notes.md` (repository root) is the hand-off for the Part 1, 3 and 4 owners. Its
  citations were checked with a throwaway script: every cited path, test, decision number,
  spec section and addendum rule exists.
- `lock_timeout` applies to the ledger's advisory lock. In the sandbox, a transaction with
  `lock_timeout = '500ms'` failed after 0.5 s with SQLSTATE 55P03 while another held key
  1279607879. This was a one-off check; there is no test for it.
- The script was checked by hand under Python 3.12 (the supported test version, decision 61).
  It was also run once under the system Python 3.13 with the same results. That run is not a
  support claim.

### Open items for integration (M5)

These are also section 9 of `integration_notes.md`.

- The actor_id for GAP_CLOSED, NOTIFICATION_REQUESTED and WEBHOOK_REGISTERED is not pinned by
  the spec. Owners: Parts 1 and 3.
- GAP_CLOSED is appended at ingest step 6, so the ledger lock is held for the rest of the
  ingest transaction. Owners: Parts 1 and 3.
- Who creates the database roles, given that migrations run as a role without CREATEROLE.
  Owner: Part 4.
- Whether the real `clock.now()` opens a second connection inside `append`, which holds the
  ledger lock. Owner: Part 4.
- The Part 2 endpoint tests depend on the auth override, the ledger-based event stub and the
  exact error messages, so they need small updates when Part 1 lands. Owner: Part 1.

## Carry-forward checklist

M5 (standalone verifier):
- [x] The script recomputes the hash from the stored prev_hash (decision 18; mutation 5 in
      decision 68).
- [x] The script never crashes on malformed rows (`tests/test_verify_script.py`).
- [x] The jsonb float edit and the 1e3 edit give the same mismatch seq on the script and the
      server (`tests/api/test_cross_verify.py`).
- [x] The script and the server agree on every M3 tamper case (`tests/api/test_cross_verify.py`).
- [x] The script sorts rows by stored seq before checking, so it matches the server's
      `ORDER BY seq` (decision 23).
- [x] A row missing any of the 8 fields is a mismatch at that seq, including a missing
      subject_id; a missing content field reports as uncanonicalizable (decision 24).
- [x] An explicit `"subject_id": null` is valid (decision 24).
- [x] An unknown extra key in a row is ignored (decision 24).
- [x] The script reads the M4 export format (M4 notes): an array of 8-field entries in seq
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

## Carry-forward for M6

Branch: all M6 work, including the final push and the draft PR, goes on `development-ledger`
(decision 46).

README items added in M5:
- [ ] The standalone verifier: usage, the output line, exit codes 0, 1 and 2, and the stderr
      formats (decisions 52 to 54 and 60).
- [ ] The hash definition and the three-entry worked example (addendum rule 7), as in
      `integration_notes.md` section 6.
- [ ] How another team re-verifies an export (`integration_notes.md` section 6).
- [ ] The script is standard library only, written for Python 3.9 or newer, and tested on 3.12
      only (decision 61).
- [ ] The script will not detect end-of-ledger removal, and will not give exact strings for
      degenerate seq rows (decisions 16 and 59).
- [ ] A link to `integration_notes.md` for the Part 1, 3 and 4 owners.

Consolidated README checklist (every README item so far):
- [ ] The global append lock serializes every view and audit call (M4 notes).
- [ ] Export shows every entry to any VIEWER, so webhook URLs must not carry secrets.
- [ ] Size the application engine pool against PostgreSQL's max_connections (decision 38).
- [ ] Part 4 sets `lock_timeout` or `statement_timeout` at connection level (decision 10).
- [ ] How to restart the Docker daemon (decision 12).
- [ ] The 2**53 JavaScript precision note (decision 6).
- [ ] The verify memory scale note (decision 20).
- [ ] The end-of-ledger truncation limit and the forged-last-row limit (decision 16).
- [ ] The ORM Session handle type (decision 8).
- [ ] No per-entry-type payload shape check (decision 11).
- [ ] The verify check order (decision 13).
- [ ] The M5 README items above.

Other M6 work:
- [ ] `docs/ac06_storage_evidence.md`: the table grants and the trigger definition, for the
      manual half of AC-06 (addendum rule 1).

## M6 decisions

71. **README structure.** The existing README keeps its development setup, and gains a
    "Ledger" part with, in order:
    - what the ledger is and where it lives;
    - the hash definition;
    - the three-entry worked example;
    - the standalone verifier;
    - setup, migrations, tests and static checks;
    - known limits and operating notes, each with its decision number;
    - the REQ and AC ids the spec names for Part 2.

    Per-caller rules (what Parts 1, 3 and 4 must do) stay in `integration_notes.md`, and the
    reasons stay in this file. The README links to both instead of copying them. Reason: one
    source for each fact, so the documents cannot drift apart.

72. **How the AC-06 storage evidence is produced.** `docs/ac06_storage_evidence.md` holds raw,
    unedited psql output from both setups (the postgres:16 container and the native PostgreSQL
    16.15 server), each after `alembic downgrade base` and `alembic upgrade head`. It shows:
    - the server version;
    - `\dp ledger_entry`;
    - every non-internal trigger definition;
    - the attributes of the two roles.

    A labelled demonstration runs in a scratch database that is dropped afterwards. It proves
    three things: the application role can only insert and read; the trigger blocks UPDATE and
    DELETE for every role, the owner included; and neither role is a superuser. It does not
    prove that removal from the end of the ledger is detected (decision 16), and it shows that
    the owner can TRUNCATE. Reason: addendum rule 1 asks for the grants and the trigger
    definition as the manual half of AC-06, and real output cannot drift from the database.

73. **Python versions in the README.** The project requires Python 3.12 (`pyproject.toml`).
    The README says the standalone verifier is written for Python 3.9 or newer but tested on
    3.12 only. Reason: decision 61. No other version was tested.

74. **Two new committed tests.**
    - `tests/test_readme_example.py` parses the hash definition's worked example, the genesis
      value and the verifier usage line out of the README, and checks them against hashlib, the
      fixture and the script, so the README cannot drift from the code.
    - `tests/api/test_ledger_system.py` follows one scenario through migrations, append, seed,
      concurrency, rollback, audit, export, the script, tampering, roles, errors and a restart,
      to show the parts work together.

    Reason: the README's example is pinned and re-verified by another team (spec, 'Ledger entry
    format'), and the earlier tests check each part on its own.

75. **M5 pushed first; M6 held locally.** At the start of M6, the 10 M5 commits were pushed to
    `origin/development-ledger`, a fast-forward from e5b6973 to 0c87aa3. All M6 commits stay
    local, with no pull request, until the M6 report is reviewed and approved. Reason: the M6
    instructions make the M5 push the only push.
