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
