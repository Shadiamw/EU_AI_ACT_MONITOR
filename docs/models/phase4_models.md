# Phase 4 model confirmations

Built on branch `phase4-part4-platform` from base commit `4d1487e`. Full confirmation against
Models 1, 3 and 4 needs the integrated Parts 1–3 (spec 4.12). This file records what Part 4 can
assert alone and what waits for integration.

## Model 1 (states)

| Stored state | How Part 4 exercises it today |
| --- | --- |
| `FLAGGED` | Ingest + smoke / demo before a reviewer opens the event |
| `UNDER_REVIEW` | Console load-as-reviewer; full transition needs Part 1's `start_if_reviewer` |
| `DISPOSITIONED` / `CLOSED` | Console disposition submit; needs Part 2 |

D2 edges (DISPOSITIONED ↔ CLOSED by revision) are Part 2 integration tests.

## Model 3 (deadlines and reminders)

| Rule | Owner | Part 4 role |
| --- | --- | --- |
| Absolute deadline from assignment | Part 2 | Fixtures assert `assigned_at + Nd` over HTTP |
| Stricter tier keeps the anchor | Part 2 | Model 5 Ex-1 |
| Looser tier needs a rationale | Part 2 / Addendum rule 1 | Model 5 Ex-1 revision path |
| Reminders at 50/80/100 only | Part 3 | Clock advance calls `fire_due`; console shows the same formula |

## Model 4 (invariants)

| INV | Status on this branch |
| --- | --- |
| INV-1 (report 409 half) | Fixture Ex-2 / Ex-4 expect 501 or 409 once Part 2's guard is present |
| INV-7, INV-10, INV-11 | Phase 5 |
| INV-8 | Differs: addendum rule 9 (Part 2 ledger logging) |
| INV-13 | Differs for future-dated samples (Part 1 limitation) |

Revisit this document on `phase4-integration` with the exact test node ids from each part's
report.
