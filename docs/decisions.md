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

76. **System-test choices.** `tests/api/test_ledger_system.py` makes these choices where the
    M6 instructions leave a detail open:
    - The clock is patched to fixed instants for the seed, the typed entries and the audit
      and view calls, so every recorded_at and hash is written down before the run. Each
      instant has sub-millisecond digits (for example 09:31:00.999999), so truncation is
      checked too (decision 7).
    - No entry type in the spec's table other than SEED has a null subject. The null
      subject_id case through `ledger.append` is a tenth appended entry, a GAP_CLOSED with no
      subject, which append accepts (decision 11). It also shows that an entry without a
      subject is in no audit trail. It is not a usage pattern for Part 3.
    - Steps a to j share one ledger and run in file order. The module overrides the shared
      pre-test truncate, rebuilds the schema with `alembic downgrade base` and
      `alembic upgrade head` before step a, and truncates after step j.
    - The restart in step j clears the cached application engine (`get_engine.cache_clear()`)
      and builds a new app, as a new process would.

    Reason: the scenario must be checkable against values derived from the spec, not against
    the output it checks.

77. **The test count in the README and integration_notes.md.** Both now say 532, the count
    after M6's two new files (13 tests, and 15 including decision 79's case).
    integration_notes.md said 504, which became wrong when M6 added tests, so it was corrected
    under the rule that allows fixing a wrong fact there. Reason: the stated count must match
    the latest run.

78. **README fixes from the fresh-clone walkthrough.** Following the README in a fresh clone
    showed one failing step: right after `docker run`, `psql` failed with "server closed the
    connection unexpectedly", because the container was still initializing. The README now
    waits with `pg_isready -h localhost` inside the container after `docker run` and after
    `docker start`. It also says that a second run of the role bootstrap prints harmless
    "already exists" errors. Reason: every README command must work as written on a new
    machine; the code did not change.

79. **A sixth tamper case in the system test.** Before running the M6 mutation checks, each
    mutant's first failing test was predicted. For "remove the first-must-be-1 check in
    `app/ledger/verify.py`", none of the five tamper cases in step g would fail: none touches
    entry 1, and the earlier tests that catch it are pre-M6 tests. Step g now has a sixth case:
    entry 1 deleted and entry 2 re-linked to the 64 zeros with a consistent hash. Expected
    `{false, 1, "2"}` with check `seq`; without the rule, verify passes entry 2 and fails at
    entry 3 instead. Reason: every M6 mutant must make at least one new test fail.

80. **The concurrent steps hold the append lock until every caller waits.** In the first M6
    mutation run, "ensure_seed skips the lock" was caught, but a repeat run let it pass once in
    four: with thread timing alone, the first caller sometimes committed its SEED before the
    others checked for an empty ledger. Steps a and c of the system test now take the append
    lock as the owner first, start the callers, wait until `pg_locks` shows every caller
    waiting for an advisory lock, and only then release it. That mutant now appends 20 SEEDs
    in every run (5 of 5). Without the lock in append, no caller waits, so step c fails at
    "0 of 50 callers reached the append lock" (5 of 5). Reason: a concurrency test must not
    depend on thread timing to catch a missing lock.

## Part 3 decisions (worker: gap sweeper and notifications)

Part 3 is built from the requirements (REQ-01, REQ-02, REQ-04, REQ-11), the Pod H API contract
of 5 October 2026, and what `integration_notes.md` sections 2, 4 and 8 already pin. The spec's
sections '3.4 Gap sweeper (REQ-02)', '3.5 Notifications (REQ-04)' and 'Configuration
(environment variables)' are not in this repository, so every choice below that they might
settle is an **ASSUMPTION** to check against them.

81. **Configuration.** Seven optional environment variables, read on every call so tests can
    change them: `SAMPLING_INTERVAL_SECONDS` (300), `GAP_TIMEOUT_INTERVALS` (3),
    `NOTIFY_MAX_DELAY_SECONDS` (900), `SWEEP_INTERVAL_SECONDS` (60), `DELIVERY_POLL_SECONDS`
    (5), `WEBHOOK_TIMEOUT_SECONDS` (5) and `DELIVERY_MAX_ATTEMPTS` (5) (`app/config.py`). Each
    must be a whole number of at least 1. The gap timeout is a count of intervals because REQ-02
    defines its default that way. Reason: the defaults come from REQ-01, REQ-02 and REQ-04 and
    the "runs every minute" sweeper; only `NOTIFY_MAX_DELAY_SECONDS` is a name the notes cite.

82. **Stubs for Parts 1 and 4.** The 0001 stub now creates `monitored_system` (system_id,
    monitoring_started_at, last_received_at) and a minimal `event` table, and grants the
    application role SELECT, INSERT and UPDATE on both. `app/events.py` gains
    `create_gap_event` and `close_gap_event`, which write that table and which Part 1 replaces,
    keeping the signatures. `events.exists` is unchanged (decision 30). The 0001 downgrade uses
    `DROP TABLE IF EXISTS`, because databases upgraded by the earlier, empty 0001 have neither
    table. The system test's head check moves from "0002" to "0003". Reason: the sweeper must
    lock the system row and create the gap event (integration_notes.md 4.3), and neither table
    exists yet.

83. **When a gap opens.** A system is silent since `last_received_at`, or since
    `monitoring_started_at` if it has never sent a sample, so a seeded system gets a gap with no
    ingest at all (API contract: `GET /v1/events?type=monitoring_gap` without any ingest). A gap
    opens when the silence reaches the timeout (amended by decision 92; first written as
    strictly longer). The detection time is the sweep that notices.
    Each gap is opened in its own transaction, which locks the system row, then re-checks the
    silence and the open gap under the lock. A partial unique index allows one open gap per
    system, as a backstop. A failure for one system is rolled back, logged by type name and
    retried on the next sweep, and the other systems carry on.

84. **What a gap writes.** The event is type `monitoring_gap`, state FLAGGED, with null metric
    fields and all four harm fields present and null: an outage carries no harm evidence, and
    gap events never reach review (API contract; REQ-07). GAP_OPENED's `last_received_at` is
    the API time format (`YYYY-MM-DDTHH:MM:SSZ`, fraction only when present), or null for a
    system that has never sent a sample. Then `notifications.enqueue` appends
    NOTIFICATION_REQUESTED of type `flag_notification` (integration_notes.md 4.1). Actor
    `system` for both.

85. **Closing a gap.** `gaps.close_if_open(session, system_id)` runs in Part 1's ingest
    transaction at step 6. It locks the system row (a no-op when the ingest already holds it),
    then the open gap row. It sets closed_at and gap_seconds, moves the event to CLOSED, and
    appends GAP_CLOSED with actor `system` (open item 6). `gap_seconds` is the whole silent time,
    from the last sample (or monitoring start) to the new sample, rounded down to an integer. It
    returns the gap's event id, or None without writing anything. It never commits. Reason:
    CLOSED is one of the contract's four states, and a gap event has no review to go through.

86. **Notifications.** `notifications.enqueue(session, event_id, actor_id,
    notification_type="flag_notification")` records the notification, creates one PENDING
    delivery for each webhook whose `event_types` include the type, and appends
    NOTIFICATION_REQUESTED last, with the caller's actor (`sys-42` in ingest, `system` in the
    sweeper). It never commits. Deliveries are created at that moment, so a webhook registered
    later does not get earlier notifications, and with no webhook nothing is sent. The body a
    webhook receives is `{type, notification_id, webhook_id, event_id, requested_at, attempt}`;
    the receiver fetches the event if it needs more. Reason: the contract leaves the payload to
    Part 3, and a minimal body keeps event data out of a third-party request.

87. **Webhook registration.** `POST /v1/webhooks` accepts REVIEWER or VIEWER (integration_notes.md
    2.5); SYSTEM gets 403. The body is exactly `{url, event_types}`, where `event_types` is 1 to
    10 of `flag_notification` and `reminder`, and repeats are stored once in first-seen order.
    The URL must be absolute http or https, at most 2048 characters, with no user name or
    password, no query string and no fragment: export shows every URL to any VIEWER and can
    never remove it (decision 32). A bad body or URL is 400 MALFORMED_BODY, with no ping. The
    ping `{type: "webhook_verification", webhook_id}` runs before any write, and only a 2xx
    answer (no redirects followed) stores the webhook. Otherwise the result is 422
    WEBHOOK_VERIFICATION_FAILED and nothing is written. The webhook row and WEBHOOK_REGISTERED
    (actor: the registering principal) share one transaction, committed before return (decision
    25). Destinations are not restricted, so a private address can be registered. That is
    acceptable for Phase 3 test use; production would need an allow-list.

88. **The delivery pass.** As in integration_notes.md 4.3:
    1. Claim due rows with `FOR UPDATE SKIP LOCKED`, mark them IN_FLIGHT with a lease
       (`WEBHOOK_TIMEOUT_SECONDS` + 60), and commit.
    2. Call each webhook with no transaction open.
    3. In a new transaction, lock the row, record the result and append last.

    Delivered means a 2xx answer within the timeout, with redirects not followed.
    NOTIFICATION_DELIVERED's `latency_seconds` is the whole seconds from `requested_at` (the
    detection moment) to the answer, and `within_bound` is
    `latency_seconds <= NOTIFY_MAX_DELAY_SECONDS`. A failed attempt n is retried after
    `min(30 × 2^(n-1), 240)` seconds, so with five attempts every try starts by 450 s, inside
    the 900 s bound. Only the last failure appends NOTIFICATION_FAILED (`last_status` is the
    HTTP status, or null for no response); earlier ones append nothing. If recording fails (for
    example a 55P03 lock timeout) it is rolled back and logged by type name; the row stays
    IN_FLIGHT and is claimed again when the lease runs out. A late result from an expired claim
    is ignored. Delivery is therefore at least once. Leases and retries use `clock.now()`, like
    everything else.

89. **Out of Phase 3, or left to other parts.** Reminders (REQ-11) are a valid webhook type but
    are not generated, because they need the deadlines of REQ-10. The worker does not write
    `sweeper_heartbeat` (a Part 4 table not yet defined). It does not call `ledger.ensure_seed`
    at start-up, because the SEED payload is Part 4's configuration (integration_notes.md 5.4).
    Part 4 adds that call when it wires the worker process.

90. **How the Part 3 tests observe the database.** As the owner role, `pg_stat_activity` hides
    other roles' `xact_start` and `wait_event_type` (checked in the container), so the tests use
    `pg_locks`. An open transaction holds a granted `virtualxid` lock, and a waiter holds an
    ungranted lock. The concurrent-sweep test holds the system row as the owner until every
    sweeper waits on it, then releases it, as in decision 80. Two mutants were run by hand and
    each was caught:
    - removing the open-gap re-check under the row lock (the unique index then rejects the
      duplicates, and the test fails on the logged sweep failure);
    - making the webhook call while a transaction is open.

91. **Test count.** 615 (614 when first written): the 532 Part 2 tests plus 83 in
    `tests/worker/`. All pass on the Docker
    setup. The worker process was also run for a few seconds against the container. It opened a
    gap for a system silent for 16 minutes and none for a fresh one.

92. **A gap opens at exactly the timeout (AC-02).** REQ-02 says "longer than a configurable
    timeout", but AC-02 sets up "withhold for exactly 3 intervals (15 min)" and expects a gap
    event, with "withholding 2 intervals must NOT trigger" as the boundary. The sweeper now
    opens a gap when the silence is 900 seconds or more: at 600 s and 899 s nothing happens, and
    at 900 s the gap opens (`tests/worker/test_gaps.py`, `tests/worker/test_worker_system.py`).
    At 900 s the third expected sample is due and has not arrived, so the third interval has
    been missed. Reason: AC-02 is what the system is accepted against, and the one-second
    difference cannot make a live system with samples every 300 s open a gap any earlier than
    the third missed sample.

## Part 3 integration decisions (aligned with the spec)

Branch `developed-part23merged` integrates Part 2 (`development-ledger`) and Part 3
(`development-gap-sweeper`) and brings Part 3 in line with `Phase3_Implementation_Spec.docx`
("the spec"), whose Part 3 sections were not available when decisions 81 to 92 were written.
Precedence: API contract, then the Part 2 addendum, then the spec. The clause-by-clause check
that drove these changes is `docs/part3_spec_audit.md`. Decisions 81 to 92 are kept as written,
as the record of what was built first; where an entry below supersedes one of them, it says so,
and the newer entry wins.

| Earlier decision | Now |
|---|---|
| 81 (configuration) | Superseded by decision 100 |
| 82 (stubs) | Superseded by decision 95 |
| 83 (when a gap opens) | Superseded in part by decision 101 (silence source, row skipping); the ≥ comparison of decision 92 stands |
| 84 (what a gap writes) | Superseded in part by decisions 101 and 102 (harm fields, `received_at`, `retain_until`) |
| 85 (closing a gap) | Superseded in part by decision 101 (`gap_seconds` from the gap's `received_at`; no `closed_at`) |
| 86 (notifications) | Superseded by decisions 93 and 94 |
| 87 (webhook registration) | Superseded by decisions 98, 99 and 103 |
| 88 (the delivery pass) | Superseded by decision 94 |
| 89 (out of Phase 3) | Stands, except that the worker now writes `sweeper_heartbeat` (decision 101) |
| 90, 91, 92 | Stand (the test count in 91 is historical) |

93. **NOTIFICATION_REQUESTED actor is `system`.** `notifications.enqueue(session, event_id,
    notification_type="flag_notification") -> str` is the spec's pinned signature (spec 3.5 and
    'Shared interfaces between parts (Pinned)'), with no actor argument. Its entry uses actor
    `system`, for breaches requested by Part 1's ingest as well as for gaps, because spec 3.2
    says the actor id "for everything the worker does is system" and the notification module is
    Part 3's (plan item B1). An unknown `notification_type` raises ValueError before anything
    is written. enqueue never commits. Reason: the signature is pinned, and a caller-supplied
    actor would need an argument the spec does not have.

94. **The delivery pass follows spec 3.5.** `notifications.deliver_due(engine) -> int` (the
    number of attempts made):
    1. Route: each notification with no delivery rows gets one PENDING row per webhook whose
       `event_types` include its type, `next_attempt_at` = now. With no such webhook it stays
       unrouted and is retried every pass, so the first webhook registered later receives it.
       Routing is in the pass, not in enqueue (plan item A5).
    2. Claim: PENDING rows with `next_attempt_at <= now` and no live lease, `FOR UPDATE OF d
       SKIP LOCKED`, at most 50 per pass. A claim sets `lease_until` = now + 65 s (the 5 s
       timeout plus 60 s) and adds one to `attempt_count`, then commits before any network call.
       There is no IN_FLIGHT status: a lease is `lease_until` on a PENDING row (plan item C5).
       The pair (`attempt_count`, `lease_until`) identifies a claim; a result recorded for a
       claim that is no longer current is dropped.
    3. Send the spec body `{notification_type, notification_id, event_id, system_id, type,
       detected_at, sent_at}` with a 5-second timeout and no transaction open. `type` is the
       event type; both times use the API format (plan item A2). Redirects are not followed.
    4. Any 2xx: DELIVERED, `delivered_at`, NOTIFICATION_DELIVERED, in a new transaction.
    5. Anything else: retry after 10, 30, 60, 120 and 300 seconds; the sixth failed attempt
       marks FAILED and appends NOTIFICATION_FAILED (plan item A4). Earlier failures append
       nothing.

    `latency_seconds` is the delivery time minus the event's `received_at`, in whole seconds
    on `clock.now()` (plan item A8). `within_bound` is `latency_seconds <=
    NOTIFY_MAX_DELAY_SECONDS`. A late delivery is logged as a warning and counted in
    `notifications.late_delivery_count()` (audit N5). Every attempt is logged with the
    notification id, webhook id, attempt, status and duration (audit N8). An exception while
    recording one result is rolled back and logged by type name, and the pass goes on to the
    next claim (audit N3); that row is claimed again when its lease runs out. Leases are on the
    simulated clock, like everything else the worker decides. Reason: spec 3.5 pins the steps,
    the body, the delays and the six attempts, and leaves the lease representation open.

95. **Migration layout (spec 4.4).** The chain is 0001 Platform (Part 4), 0002 Ledger (Part 2),
    0003 Detection (Part 1), 0004 Notifications (Part 3), head `0004` (plan item C1).
    - `0001_platform_stub.py` is a STUB for Part 4 holding only `monitored_system`
      (`system_id`, `sampling_interval_s`, `gap_timeout_intervals`, `last_received_at`, all
      NOT NULL) and `sweeper_heartbeat`. The spec names only `last_ran_at` for the heartbeat;
      a boolean primary key `singleton` (default true, check `singleton`) keeps it to one row.
    - `0003_detection_stub.py` is a STUB for Part 1 holding only `event` and `harm_field`, with
      the spec 1.7 names: index `ix_event_detected_at_event_id` on `(detected_at DESC,
      event_id)`, check `ck_event_state`, harm_field primary key `(event_id, field_key)` with
      check `ck_harm_field_status`, and the partial unique index
      `uq_event_one_open_gap_per_system` on `event(system_id)` where `type = 'monitoring_gap'`
      and `state = 'FLAGGED'`. No `metric_sample` or `breach_series`, which nothing here uses.
      `harm_field` has no foreign key, because spec 1.7 does not name one; Part 1 decides.
    - `0004_notifications.py` (Part 3) creates `webhook`, `notification` (one row per event,
      foreign key to `event`) and `notification_delivery` (index `(status, next_attempt_at)`),
      each with exactly the spec 3.7 columns. `webhook.registered_by` is dropped: the
      registering principal is the actor of WEBHOOK_REGISTERED.
      The separate `monitoring_gap` table is gone: the open gap is the FLAGGED gap event (plan
      item C4).
    - Every non-ledger table grants SELECT, INSERT, UPDATE and DELETE to the application role
      and nothing to PUBLIC, because spec 4.4 gives it "normal read/write on all tables except
      the ledger" (audit N10).
    - The only Part 2 test change is the head assertion in `tests/api/test_ledger_system.py`
      (`"0003"` to `"0004"`).

    Editing 0001 and 0003 in place is allowed because nothing has been deployed. A database
    built from `development-gap-sweeper` is at the old revision `0003` (`0003_worker.py`) and
    must be dropped and rebuilt (README section 5.5). Reason: spec 4.4 fixes the order and the
    owners, and the real Part 1 and Part 4 migrations must be able to replace the stubs file
    for file.

96. **`notification_delivery.last_status` is text.** Spec 3.7 types the column as text, so it
    stores the HTTP status as a string, or NULL when there was no response. The
    NOTIFICATION_FAILED payload keeps `last_status` as an integer or null, as decided before
    (integration_notes.md 4.1), because ledger payloads keep integers as integers (addendum
    rule 2). Reason: the column type is the spec's; the payload type was already settled.

97. **Health values.** `app.worker.health_values(engine) -> HealthValues` is one read-only
    function for Part 4's `/healthz`, with the three field names of spec 4.6:
    - `sweeper_last_ran_seconds_ago`: whole seconds from `sweeper_heartbeat.last_ran_at` to
      `clock.wall_now()` (real time, as spec 3.3 and 4.6 say), or None before the first sweep;
    - `pending_deliveries`: the number of PENDING `notification_delivery` rows;
    - `oldest_pending_delivery_seconds`: whole seconds from the oldest PENDING row's
      notification `created_at` to `clock.now()`, or None when nothing is pending. Delivery rows
      have no creation time in spec 3.7, so the age is the notification's.

    Unrouted notifications (no webhook yet) have no delivery rows and are not counted. Part 4
    decides the 503 rule and the response body. Reason: spec 3.8 asks for these values and
    spec 4.6 names them; the clocks follow the heartbeat rule.

98. **Webhook URLs follow spec 3.6 only (supersedes the URL rules of decision 87).**
    - The URL must be an http or https URL with a host. Anything else, such as
      `ftp://example.com`, a URL with no host, or text that is not a URL, is 400
      MALFORMED_BODY, and nothing is pinged or stored.
    - When ENVIRONMENT is not `test`: https only, and private and loopback addresses are
      refused (decision 99), also with 400 MALFORMED_BODY. The spec names no code for this;
      MALFORMED_BODY is the code spec 3.6 gives for a URL it does not accept.
    - Otherwise any URL is accepted, including one with a query string, a user name and
      password, or a fragment. It is stored exactly as received, WEBHOOK_REGISTERED carries it
      exactly as received, and deliveries go to it.
    - The 2048-character cap and the 10-entry cap on `event_types` are removed, because the
      spec has neither (audit N6).

    Because `GET /v1/ledger/export` shows every URL to any REVIEWER or VIEWER (decision 32), it
    is recommended, not enforced, that webhook URLs carry no secrets (integration_notes.md
    4.4; README section 6). Reason: spec 3.6 states the URL rules. Decided during the Part 2 +
    Part 3 integration, 15 October 2026.
    Logs never carry webhook URLs (decision 106).

99. **Private-address detection outside the test environment.** A host is refused when:
    - it is `localhost` or ends in `.localhost` (a trailing dot is ignored);
    - it is an IP literal that is not a public address;
    - it resolves (`socket.getaddrinfo`) to any address that is not public.

    "Public" means Python's `ipaddress` `is_global` and not multicast, after unwrapping an
    IPv4-mapped IPv6 address and dropping an IPv6 zone id. That refuses loopback (127.0.0.0/8,
    ::1), RFC 1918 (10/8, 172.16/12, 192.168/16), link-local (169.254/16, including
    169.254.169.254, and fe80::/10), unique-local fc00::/7, shared 100.64/10, unspecified and
    reserved ranges. A host that does not resolve is not refused here; its ping fails and
    registration is 422 WEBHOOK_UNVERIFIED. The check runs at registration only: the address is
    not re-checked at delivery time, so DNS that changes after registration (rebinding) is not
    caught. Reason: spec 3.6 asks to "refuse private or loopback addresses" without listing
    them; these are the standard non-public ranges.

100. **Configuration (supersedes decision 81).** Part 3 reads, on every call:
     - `ENVIRONMENT` (required): `test` allows any webhook address (decision 98);
     - `NOTIFY_MAX_DELAY_SECONDS` (900), `SWEEP_INTERVAL_SECONDS` (60) and
       `DELIVERY_POLL_SECONDS` (5), each a whole number of at least 1;
     - `LOG_LEVEL` (INFO), used by `python -m app.worker` (audit N2).

     `SAMPLING_INTERVAL_SECONDS` (300) and `GAP_TIMEOUT_INTERVALS` (3) only seed sys-42's
     `monitored_system` row (spec 5). `app/config.py` keeps readers for them so Part 4's seed
     can use them, but the sweeper never reads them (plan item D1). `WEBHOOK_TIMEOUT_SECONDS`
     and `DELIVERY_MAX_ATTEMPTS` are removed: the timeout is the constant 5 seconds
     (`webhooks.TIMEOUT_SECONDS`) and the attempts the constant 6
     (`notifications.MAX_ATTEMPTS`) (plan item D3). Reason: spec 5 lists every variable.

101. **The gap sweeper follows spec 3.4.** `gaps.sweep(engine) -> list[str]` (the new gap
     event ids):
     - Each system is handled in its own transaction. Its `monitored_system` row is locked
       with `FOR UPDATE SKIP LOCKED`, so a row another process holds is skipped until the next
       sweep (plan item D2).
     - The threshold is the row's `gap_timeout_intervals × sampling_interval_s`, and silence is
       `clock.now() − last_received_at` (plan items D1 and C3). A gap opens when silence ≥
       threshold (decision 92) and the system has no FLAGGED gap event.
     - The gap event has `received_at` = `detected_at` = now, `retain_until` (decision 102),
       null metric and evidence fields, and four `harm_field` rows with status NOT_APPLICABLE
       and null evidence, confirmer and confirmation time (plan item A6). Then GAP_OPENED, then
       `notifications.enqueue`, then commit.
     - Any exception for one system is rolled back and logged by type name, and the sweep goes
       on (audit N3). Each sweep logs `systems_examined` and `gaps_opened` (audit N8).
     - After every sweep it upserts `sweeper_heartbeat.last_ran_at` with `clock.wall_now()`,
       the real time (spec 3.3; plan item C7). `wall_now()` is a second function in the clock
       stub, and the only real-time read outside `clock.now()`.

     `gaps.close_if_open(session, system_id) -> str | None` locks the system row, then the
     open gap event, sets state CLOSED, and appends GAP_CLOSED whose `gap_seconds` runs from the
     gap's `received_at` to now in whole seconds (spec 3.4; plan item A7). It never commits.
     Reason: spec 3.4 pins the steps and the gap event fields.

102. **`retain_until`.** Spec 1.5 says "detected time plus six calendar months (at least 183
     days)". `app.formats.retain_until(detected_at)` returns the later of detected_at plus six
     calendar months (the day clamped to the end of a shorter month) and detected_at plus 183
     days. So 1 September gives 3 March (six months is 1 March, 181 days later), and 1 October
     gives 1 April (182 days, so the 183-day floor gives 2 April). Part 1 can reuse it for
     breach events (plan item D4). Reason: this is the only reading under which both parts of
     the sentence always hold.

103. **Webhook registration (supersedes the rest of decision 87).** `POST /v1/webhooks`:
     - checks 401, then 403 (REVIEWER or VIEWER), then the body (400), then the URL rules
       (400), then pings (audit N1);
     - the body is exactly `{url, event_types}`; extra keys or wrong types are 400
       MALFORMED_BODY; repeated `event_types` are stored once, in first-seen order;
     - the ping body is exactly `{"notification_type": "verification_ping", "sent_at": ...}`,
       5-second timeout, no redirects followed, sent before any write (plan item A3);
     - any non-2xx answer, timeout or connection error is 422 WEBHOOK_UNVERIFIED (the contract
       and spec code; plan item A1), and nothing is stored;
     - on success the webhook row and WEBHOOK_REGISTERED (actor: the registering principal,
       subject: the webhook id) commit together before the 201 `{webhook_id}`.

     Reason: spec 3.6 and the contract's error codes.

104. **Worker loop and test reset.** One loop runs the sweeper every `SWEEP_INTERVAL_SECONDS`
     and the delivery pass every `DELIVERY_POLL_SECONDS`, both on `time.monotonic` (the spec
     leaves the structure open). Every iteration catches every exception and logs its type
     name. A test reset that empties the tables mid-run is tolerated: a sweep finds no systems,
     a pass finds no rows, and a result whose delivery row has vanished is dropped
     (`tests/worker/test_worker_system.py::test_the_loop_survives_a_reset_that_empties_the_tables`;
     audit N4). Reason: spec 3.3.

105. **Contract and spec agree for Part 3.** No conflict was found between the API contract and
     the spec on Part 3: the contract's `POST /v1/webhooks` (201 `{webhook_id}`, 422 when the
     ping fails) and its error code WEBHOOK_UNVERIFIED match spec 3.6. "Duplicate resets
     silence" and "Wrong sender clock" (spec 3.9) need Part 1's ingest and stay deferred.

106. **Logs never carry webhook URLs.** `app/webhooks.py`, where the webhook HTTP client is used,
     holds the `httpx` and `httpcore` loggers at WARNING. httpx otherwise logs every request's
     full URL at INFO, user name, password, query string and fragment included, and httpcore
     traces connections at DEBUG. Setting it there covers both the worker's deliveries and the
     web process's verification ping, whatever LOG_LEVEL says. Part 3's own log lines name a
     webhook only by its `webhook_id`; the spec 3.8 attempt line keeps the notification id,
     webhook id, attempt, status and duration. Errors are logged by exception type name only
     (decision 36), so an exception text holding the URL is never logged
     (`tests/worker/test_log_privacy.py`). Reason: the export already shows a URL to any VIEWER
     (decision 98), but logs go to a different and longer-lived audience, and project logs carry
     no payload values (decision 28). Decided during the Part 2 + Part 3 integration close-out,
     15 October 2026.

107. **A webhook URL must be one the HTTP client can use.** `check_url` parses the URL with
     `httpx.URL`, the parser `httpx.post` uses, so the check and the call cannot disagree:
     anything it refuses (a control character such as a newline, tab, NUL or DEL, an invalid IPv4
     address such as 999.1.1.1, a malformed IDNA label, a URL over 65,536 characters) is 400
     MALFORMED_BODY, with nothing pinged, stored or appended. Two more checks use the client's own
     machinery instead of a list of characters: httpx must read the same host as `urlsplit`
     (ignoring case), because httpx percent-encodes characters that cannot be in a host, such as
     a space or `<`, instead of refusing them; and a host name must pass the `idna` codec that the
     socket layer applies when it connects, which refuses an empty or over-63-character label.
     Every decision-98 rule is unchanged: any URL the client can use is accepted and stored and
     ledgered exactly as received, query, credentials and fragment included. As defence in depth,
     `post_json` treats `httpx.InvalidURL` and `UnicodeError` like a transport failure, so a URL
     that still slips through is a failed ping (422 WEBHOOK_UNVERIFIED, nothing stored), never a
     500. Only the exception's type name is logged (decisions 36 and 106)
     (`tests/worker/test_webhook_urls.py`). Reason: spec 3.6 makes anything that is not an
     acceptable http or https URL a 400; before this, such URLs passed `urlsplit`, reached the
     ping and became 500 INTERNAL_ERROR. Decided during the Part 2 + Part 3 integration,
     15 October 2026.

108. **CI runs the full suite and names every Phase 3 acceptance criterion.**
     `.github/workflows/ci.yml` runs on every push and pull request. The test job starts
     PostgreSQL 16, runs `tests/sql/bootstrap_roles.sql`, then `ruff check`, `ruff format
     --check`, `mypy` and `pytest`. `tools/check_acceptance.py` reads that run's JUnit file and
     requires the AC-02, AC-04, AC-06 and AC-15 tests to have passed. AC-12 is not required
     (decision 89). A second job builds the image and starts `docker compose` until
     `GET /healthz` returns 200. Reason: integration checklist item 8, and the deployment
     owner's job is to support every acceptance criterion by running it.

109. **The deployable unit is one image and two processes.** `Dockerfile` is Python 3.12, runs
     as a non-root user, and includes uvicorn (section 5.7 of integration_notes.md, which
     decision 50 had left out). `python -m app.runtime prepare` waits for PostgreSQL, runs
     `alembic upgrade head` as the owner and calls `ensure_seed` with the addendum rule 7
     payload `{"systems": ["sys-42"], "threshold_count": "2"}`. `api` and `worker` wait until
     the migrated revision is the script head, seed again (a second call appends nothing) and
     then exec uvicorn or the worker. `docker-compose.yml` is the local and single-host
     shape: postgres:16 with `max_connections=200`, one API and one worker. Roles are created
     by `tests/sql/bootstrap_roles.sql` as the superuser, once, because `monitor_owner` has no
     CREATEROLE (open item 8). Those passwords are the README's local and CI passwords, not
     production secrets. Reason: one image can be the API or the worker, and a migration role
     without CREATEROLE cannot create the roles spec 4.4 puts in 0001.

110. **`LOCK_TIMEOUT` is the application connection's lock_timeout.** When it is set,
     `app/db.py` passes it as libpq `options=-c lock_timeout=...`. The value must be a
     positive integer plus `ms`, `s` or `min`, so it cannot carry a second setting. Compose
     sets `5s`. The test suite does not set it: the concurrency tests hold the append lock
     until every caller is waiting, which can take longer than 5 seconds (decision 80). A
     wait that runs out is SQLSTATE 55P03 and, in the API, 500 INTERNAL_ERROR (decision 36).
     Reason: decision 10, without making the lock-wait tests time out.

111. **`GET /healthz`.** No authentication. The body is exactly
     `sweeper_last_ran_seconds_ago`, `pending_deliveries` and
     `oldest_pending_delivery_seconds` from `worker.health_values` (decision 97). Status 200
     when the sweeper has not run yet, or last ran within `3 * SWEEP_INTERVAL_SECONDS`.
     Status 503, with the same body, when the last sweep is older than that. A database
     error is 500 INTERNAL_ERROR, like any other exception. Reason: spec 4.6 names the fields
     and leaves the 503 rule to Part 4. Three missed sweep intervals is the threshold, and a
     fresh process is not failed before its first sweep.

112. **AWS and the Evidently format are recorded, not invented.**
     `docs/phase3_uncertainties.md`. This repository creates no cloud resources. The image
     and the compose file are what RDS PostgreSQL 16 and two processes would run. There is
     no Evidently parser: ingest is Part 1, and the only metric document the ledger already
     stores is the EVENT_FLAGGED payload in the README worked example, with non-integer
     numbers as decimal strings. CI does not accept an Evidently report as input. Reason:
     the deployment part owns these Phase 3 uncertainties, and neither was decided by Parts
     2 or 3.

## Part 1 implementation decisions

Written on `main` (commit `5310ae8`) as decisions 93 to 95, before Part 3's integration
decisions 93 to 107 existed. Renumbered 113 to 115 in the merge into
`developed-part123merged`, because 93 to 107 are Part 3's and 108 to 112 are used on the
Part 4 branch `development-deploy`. The text is otherwise unchanged. The Part 1 integration
decisions further down supersede parts of 113 and 115.

113. **Part 1 proceeds with explicit assumptions.** The user supplied the project discussion
    confirming ingestion, versioned threshold detection, breach occurrences, evidence capture,
    harm-field prefilling, and atomic ledger/notification writes as the Phase 3 API scope,
    then authorized implementation despite the missing exact-algorithm specification.
    [part1_api.md](part1_api.md) records the implemented choices and rationale: natural-key
    replay without changing the agreed request body; strict threshold comparison selected
    by sample time; series ending on newer healthy data; preceding-hour evidence; list
    pagination and role scope. These choices remain provisional for team confirmation.

114. **Credential lookup ends before handler work.** Auth reads key digests through a short
    autocommit connection, closed before the handler. This prevents a key lookup from
    leaving an application transaction open during webhook verification. Business writes
    still use the request Session and commit explicitly before response.

115. **Additive persistence and explicit demo provisioning.** Revision 0004 extends the
    existing event table and creates samples plus the minimal key/threshold contracts
    needed before Part 4 arrives. It grants the application read-only access to keys and
    thresholds. `python -m tools.seed_part1` provisions published demo fixtures only in
    ENVIRONMENT=test. Normal startup and migrations never seed those credentials.

## Part 1 integration decisions (aligned with the spec)

Branch `developed-part123merged` brings `main` (atharej's Part 1, `5310ae8`) into
`developed-part23merged` with a real merge, then reworks Part 1 to
`Phase3_Implementation_Spec.docx` ("the spec"), which was not available when decisions 113 to
115 were written. Precedence: API contract, then the Part 2 addendum, then the spec, then
Models 4 and 5. The clause-by-clause check that drove the changes is `docs/part1_spec_audit.md`;
the tests that prove each rule are listed in `docs/part1_test_map.md`. Decisions 113 to 115 are
kept as written; where an entry below supersedes one, it says so, and the newer entry wins.
Decided 17 and 18 October 2026.

| Earlier decision | Now |
|---|---|
| 113 (Part 1 assumptions) | Superseded: each assumption is replaced by the spec's rule, or by decisions 116 to 131 where the spec is open |
| 114 (credential lookup ends before handler work) | Stands; decision 123 keeps the short autocommit connection |
| 115 (additive persistence, demo provisioning) | Superseded by decisions 127 (schema) and 128 (seed) |
| 29, 41 (auth stub) and 30, 44 (ledger-based event lookup) | Replaced by Part 1's real auth (decision 123) and the event table lookup; the entries stay as the record of the stubs |
| 34 (the G2 test-only route) | The stand-in `GET /v1/events/{event_id}` is removed from `tests/api/g2_routes.py`; the real route is Part 1's. The two `/_test/` routes stay in tests |

116. **A duplicate is 200 with the original ids and closes nothing.** Spec 1.4 step 5, as
     the user decided. The identity is exactly (system_id, metric_name, timestamp), where the
     timestamp is the instant, so `Z` and `+00:00` forms are the same sample; metric_type and
     value are not part of it. A duplicate sets `monitored_system.last_received_at` to
     `clock.now()`, commits and returns 200 with the original `sample_id`, plus the original
     `flagged_event_id` when that sample had one. A duplicate with a different value is still
     a duplicate: 200, no 409, and the stored value is kept. It returns before step 6, so it
     does not call `gaps.close_if_open`: an open gap stays FLAGGED until a new sample arrives.
     It creates nothing else and appends nothing. Reason: the contract lists only 201 for a
     created sample and says nothing about duplicates, so the spec decides, and step 5 returns
     before step 6. Risks: a client that insists on 201 for a duplicate (flipping the status is
     one line in `app/ingestion.py`); a sender that resends a changed value under an
     existing timestamp gets 200 and no sign the value was dropped; a sender that only replays
     old samples during an outage keeps last_received_at fresh, so the sweeper opens no new gap
     for that time, while an already open gap stays open. Spec Appendix B lists the 200 for
     confirmation with the other team. Supersedes the natural-key replay of decision 113
     (metric_type in the key, 409 on a changed value, last_received_at unchanged).

117. **GET /v1/events is a bare array of summaries that every role can read.** Each item is
     `{event_id, system_id, type, metric_name, detected_at, state}` (spec 1.6), ordered by
     detected_at descending, ties broken by event_id ascending (the spec names the tie key,
     not its direction). Any authenticated role, the SYSTEM key included, sees every event:
     the contract says "any authenticated role" and names no per-system scope. No
     `X-Total-Count` or other paging header is sent. The call writes no ledger entry. Spec 1.6
     and Appendix B say the bare array must be confirmed with the other team. Supersedes the
     role scope and count header of decision 113.

118. **List filters and paging.** `state` must be one of the four contract states and `type`
     one of `threshold_breach` or `monitoring_gap`; `since` is an ISO-8601 UTC time (`Z` or
     `+00:00`; a naive time or another offset is 400, as spec 4 says for every timestamp the
     API accepts) and selects detected_at ≥ since. `limit` defaults to 50 and `offset` to 0;
     each must be a whole number written in decimal digits. A negative or non-integer value is
     400 MALFORMED_BODY; a limit above 200 is capped at 200, not refused (spec 1.6 "max 200",
     and the user's decision); `limit=0` is allowed and returns `[]`. An unknown `system_id`
     returns `[]`. Unknown query parameters are ignored. Every refusal is 400 MALFORMED_BODY,
     the only 400 code the spec defines.

119. **The evidence bundle.** `{"window": {"start": ..., "end": ...}, "samples": [{"metric_name",
     "value", "timestamp"}, ...]}` (spec 1.5 names `window` and the sample fields, not the
     window's shape). `end` is the triggering sample's timestamp and `start` is 60 minutes
     earlier; both ends are inclusive ("up to and including"). Only this system's and this
     metric's samples, oldest first (ties by sample_id), as stored when the event is created;
     later arrivals are not added and later series joins do not change it. Values are plain
     decimal strings with the stored digits and timestamps use the API format, so the bundle
     holds no float or Decimal and is valid input for Part 2's canonical JSON, which allows
     integers only among numbers (addendum rule 2). `evidence_sha256` is the lowercase hex
     SHA-256 of the UTF-8 canonical JSON (keys sorted at every level, separators `,` and `:`,
     non-ASCII not escaped), so anyone can recompute it from the stored bundle. Supersedes the
     bare sample list of decision 113.

120. **The newest threshold version is the highest `version`.** Step 7 reads the
     threshold_version row for (system_id, metric_name) with the largest `version`;
     `valid_from` is stored but not consulted, and the sample's metric_type does not select
     the threshold (a mismatch is stored as sent). The event records the version id and limit
     it was created under and keeps them; a later sample that joins the series is evaluated
     against the newest version but joins the existing event. Reason: spec 1.4 step 7 says
     "newest"; Phase 3 has no API to add versions (spec 1.9, known limitations). Supersedes
     "selected by sample time" in decision 113.

121. **Body validation details.** Spec 1.4 step 2, plus these readings where it is silent: the
     body must be a JSON object; `system_id` and `metric_name` must be JSON strings, and a
     string containing U+0000 or a lone surrogate is 400 because PostgreSQL text cannot hold
     it; `value` must be a JSON number (not a string, boolean or null), finite, with at most
     100 significant digits and 50 decimal places, so that it is stored exactly; `timestamp`
     must be a string with a date and a time. Names have no length limit and may be empty
     (the spec sets none). Unknown extra fields are ignored (spec 1.4). Every failure is 400
     MALFORMED_BODY with the shared message. Supersedes the length limits and extra-field
     rejection of decision 113.

122. **Lock order inside the ingest.** The monitored_system row (step 4), then the open
     breach_series row for the metric, then `gaps.close_if_open` (step 6), which may lock the
     gap event and appends GAP_CLOSED, taking the ledger lock. Spec 7 puts event rows before the
     ledger lock, and the series is the ingest's event-side row, so it is locked before step 6
     rather than at steps 9 and 10. Spec 1.4's step order is otherwise kept: set
     last_received_at, close the gap, read the threshold, then the writes of steps 9 to 11,
     with EVENT_FLAGGED appended before `notifications.enqueue` (which appends
     NOTIFICATION_REQUESTED). The notification row enqueue inserts is new, so no other
     transaction can hold it. All ingests for a system already serialize on the system row;
     the series lock keeps spec 7's order explicit.

123. **Bearer auth with a constant-time compare over every stored digest.** The header is
     `Authorization: Bearer <key>`; the scheme name is case-insensitive (RFC 9110), and the key
     is one token with no whitespace, at most 4096 characters. Anything else, including
     `X-API-Key`, is 401 MISSING_OR_INVALID_KEY. The presented key's SHA-256 hex digest is
     compared with `hmac.compare_digest` against every row of `api_key`, with no early exit,
     on the short autocommit connection of decision 114, so the time taken does not depend on
     which key matched or how much of a digest did (spec 1.3, "compare in constant time").
     The principal is the row's role, actor_id and system_id. `require(*roles)` gives 401,
     then records the actor id for the request log, then 403 FORBIDDEN_ROLE. Supersedes the
     `X-API-Key` header and the `enabled` column of decisions 113 and 115.

124. **Exact decimals in responses.** observed_value and threshold_value are written as JSON
     numbers with exactly the stored digits (0.30 stays `0.30`; a 31-digit value keeps all
     its digits), by `app/jsonout.py`, never through a float; that writer refuses floats and
     non-finite numbers. Reason: spec 1.6 "Decimal values serialize as JSON numbers" and spec
     2 Numbers. A client that reads JSON numbers as binary floats will see 0.3; that is its own
     parsing. Supersedes the float conversion of decision 113.

125. **The disposition and report stubs.** `POST /v1/events/{event_id}/disposition` (REVIEWER
     only) and `GET /v1/events/{event_id}/report` (REVIEWER or VIEWER) authenticate, check the
     role, then return 501 NOT_IMPLEMENTED, without reading the body or looking the event up
     (spec 1.6). The contract's full behaviour for these endpoints (contract Example 3's
     RATIONALE_REQUIRED, 404 and 409) belongs to Phases 4 and 5 (spec section 3), so the 501
     is phasing, not a conflict with the contract.

126. **Observability.** One JSON line per request on the `api.request` logger: `ts`
     (`clock.now()` in the API format), `level`, `request_id` (a new UUID per request),
     `actor_id` (null before authentication), `method`, `path` (without the query string),
     `status`, `duration_ms` (an integer, from a monotonic timer, not the clock), plus
     `event_id` when the request is about one event, `outcome` for an ingest and `error_code`
     for an error. Level INFO, and ERROR for a 5xx except the stubs' 501, which is the
     expected Phase 3 answer and must not count as a failure. Nothing else from the request is
     logged: no header, body or query value, so never a key. Counters per process, in
     memory: samples accepted, duplicates, breaches, series joins, and 4xx by error code
     (`app/observability.py`). Every threshold evaluation is logged at DEBUG on
     `api.detection` with the version id, comparator, limit, value and outcome (spec 1.8).
     The web process's log handler is Part 4's; the `api` logger's level follows LOG_LEVEL.

127. **Part 1's schema.** metric_sample, breach_series, event and harm_field are in migration
     0003, renamed from the stub `0003_detection_stub.py` with the same revision ids (0003
     revises 0002), and api_key and threshold_version are added to Part 4's stub 0001, as spec
     4.4 orders them. Columns are exactly spec 1.7 and 4.4. Constraints: the unique
     (system_id, metric_name, timestamp) on metric_sample; the partial unique index of one open
     series per metric; occurrence_count ≥ 1; the event state and harm-field status checks;
     the index on event (detected_at DESC, event_id); the open-gap index; on api_key, a SYSTEM
     row has a system_id and no other row does; one threshold row per (system_id,
     metric_name, version). No foreign keys among Part 1's tables, because spec 1.7 names
     none (Part 3's notification still references event). The application role has normal
     read and write on every table except the ledger (spec 4.4, pinned), which drops the
     read-only grant on keys and thresholds of decision 115. main's revision 0004 is folded
     into 0001 and 0003. Editing migrations is allowed because nothing has been deployed:
     `development-deploy` creates no AWS resources (its docs/phase3_uncertainties.md says so), and
     spec 4.4 forbids editing only a deployed migration.

128. **The seed stand-in.** `tools/seed_part1.py`, marked STUB for Part 4, installs the spec's
     seed fixtures in ENVIRONMENT=test only: sys-42 with SAMPLING_INTERVAL_SECONDS (300) and
     GAP_TIMEOUT_INTERVALS (3) and last_received_at = now; exactly three api_key rows, the
     SHA-256 digests of INGEST_KEY_SYS_42, REVIEWER_KEY and VIEWER_KEY; thr-1 accuracy_drift
     drift gt 0.30 and thr-2 accuracy performance lt 0.70, version 1, valid_from = now; then
     `ledger.ensure_seed` with `{"systems": ["sys-42"], "threshold_count": "2"}`. It is
     idempotent and never truncates. Part 4's startup seed and reset replace it (spec 4.6).
     Supersedes decision 115's published fixed keys (keys now come from the environment, spec
     5), its 0.80 accuracy limit and its fixed validity date.

129. **A healthy sample closes the open series whatever its timestamp.** Spec 1.4 step 9 has no
     condition on the sample's timestamp, so a healthy sample timestamped before the breach
     still ends the series, and a breaching sample timestamped before the series opened still
     joins it. Risk: a late, out-of-order healthy sample ends a series that is still
     breaching, and the next breach opens a new event and a second notification. Supersedes
     "series ending on newer healthy data" in decision 113.

130. **Series times and ids.** breach_series.opened_at and closed_at are `clock.now()`, server
     time like received_at (spec 1.7 gives the column, not its source). The series id is
     `ser-` plus a UUID (spec 4 lists no prefix for it). occurrence_count counts every
     breaching sample of the series, the first included, and each joining sample's event_id
     is set to the series' event (spec 1.4 step 10).

131. **Part 1 adopts Part 2's errors and session modules.** `app/errors.py` (the contract error
     body, 404 for unknown routes and wrong methods, 400 for validation, 500 with nothing
     leaked) is Part 1's errors module as Part 2 wrote it; its STUB mark is removed. Part 2's
     message constants stay the contract messages. `app/db.py` is kept as it is and stays
     marked STUB for Part 4, which may add `lock_timeout` and pool settings. Decision 27's
     wrong-method 404 is confirmed for Part 1's routes.

132. **The `api` logs reach the server's output; counters are logged.** The final spec check
     (docs/final_spec_check_results.md, F2 to F4) started the app the README way and found
     that the spec 1.8 lines never left the process: the `api` logger had no handler, so
     Python printed only WARNING and above, and the counters were kept in memory only.
     `observability.configure_logging`, called by `create_app`, now gives the `api` logger one
     stream handler on stderr (JSON lines as they are; other lines as `LEVEL [logger]
     message`) and its LOG_LEVEL. Records still propagate, so a root handler sees them too.
     "Log counters (or metrics)" is read as a log line: each counter change writes one JSON
     line `{ts, level: "INFO", counters: {...}}` with the process's current totals on
     `api.counters`. Risk: if Part 4 adds a root handler without removing this one, each line
     is written twice; integration_notes.md 5.12 says to remove it. The same check found
     (F1) that POST /v1/webhooks read its body before the key and role check, so `{bad` got
     400 instead of 401 or 403; the body is now parsed in a dependency after `require`, as
     POST /v1/metrics does (spec 4, order of checks). That is a fix to the spec, not a new
     reading.

133. **Test reset, clock advance and clock_state.** Migration 0001 adds `clock_state`
     (`singleton` boolean PK, `offset_seconds` bigint). The application role may only SELECT;
     `POST /v1/test/reset` and `POST /v1/test/clock/advance` write as the owner. Outside
     `ENVIRONMENT=test` both routes are 404. Reset truncates the Part 1, 2 and 3 tables plus
     `clock_state`, reinstalls `tools.seed_part1.seed`, and sets the offset to 0. Advance
     adds whole seconds to the offset, then calls `gaps.sweep` and `notifications.deliver_due`
     once each (spec 4.6). `clock.now()` in test is wall time plus the offset, read on its
     own connection (open item 9); outside test it is wall time with no database read.
     `wall_now()` stays real UTC. Reason: the Pod showcase cannot wait 15 real minutes for a
     gap, and the smoke test of spec section 9 needs these two endpoints.

## Part 4 decisions (Phase 4 platform; numbers 134 to 153)

134. **CI baseline for Phase 4 (Phase 4 Addendum 9.1, commit 1; spec 4.3 step 2).** Main was
     red at 60d8d08 for two reasons (debrief LIM-05). `app/suite_runner.py` set
     `finished_at = time.time()`, which the clock-rule test
     (`tests/worker/test_clock_rule.py`) forbids outside `app/clock.py`; it now uses
     `clock.wall_now().timestamp()`. This widens the stated use of `clock.wall_now()`, whose
     docstring says "only for the sweeper heartbeat": it is now also the real time of a suite
     run's end, which must not follow the simulated clock either. The nested `if` in
     `_summarize` that ruff flags as SIM102 is merged into one condition with the same
     result. ruff is pinned to exactly 0.17.1 in `pyproject.toml` (the version in use on
     October 21, 2026; the file only required >=0.6), so a new lint rule in a later ruff
     cannot turn a branch red with no code change. `started_at`'s
     `default_factory=time.time` (`app/suite_runner.py:45`) is left as it is: the clock-rule
     test does not flag it and the addendum names only line 102.
