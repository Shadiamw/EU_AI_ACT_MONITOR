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

M6 README:
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
