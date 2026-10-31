# Phase 4 Part 2 report: dispositions and deadlines

Owner: Arvind (Part 2). Report contents follow Phase 4 Addendum 9.8 and Integration Guide 3.
This file is committed on its own, after the code head below; it changes no code or tests.

## Branch and commits

| Item | Value |
| --- | --- |
| Branch | `phase4-part2-dispositions` |
| Head | `33fdcf01e269ffb39b11914a0533060977f7bf1c` |
| Starts from | Part 1's head `d4d8c1f` (`origin/phase4-part1-review`), at the owner's request |
| Base commit | `4d1487e` (`origin/phase4-base`, fifth base commit). Note: main is still at `60d8d08`; the base commits are not on main yet |
| Commits | `ff3bc64` (a) migration 0006 · `2fe4fdf` (b) deadline engine, settings and the start-up check · `847d952` (c) endpoint, summary, report guard and the Phase 3 stub tests · `33fdcf0` (d) README section 11 and decisions |

**Deviation from Addendum 9.2 (decision 174).** Part 2 is built on Part 1's branch, not on the
bare base commit, so it contains Part 1's commits up to `d4d8c1f`. At integration, merging Part 2
first brings those commits; merging Part 1 next adds only what Part 1 committed after `d4d8c1f`.
The order 2, 1, 3, 4 still works. Part 1's branch must not be rebased or force-pushed.

## Tests

PostgreSQL 16 in Docker (`postgres:16`, roles from `tests/sql/bootstrap_roles.sql`), Python 3.12.

| Run | Result |
| --- | --- |
| Baseline: Part 1's head `d4d8c1f`, whole suite | 1086 passed, 44 skipped (12 min 38 s) |
| This branch, `tests/part2` | 159 passed, 8 skipped |
| This branch, whole suite, no flag | 1247 passed, 52 skipped, 0 failed (27 min). That is 1130 + 167 Part 2 tests + 2 new parameters of the schema tests |
| This branch, `--run-integration -m integration` | 44 passed (all of Part 1's), 8 failed (all of Part 2's: `relation "escalation" does not exist`) |
| ruff 0.17.1 (the pinned version), ruff format --check, mypy strict | clean |
| Part 2 ledger gate: `git diff 2cea34f -- app/ledger app/audit.py tools/verify_ledger.py migrations/versions/0002_ledger.py` | only the four entry types in the two allowed files; Part 2 touched none of them |

The whole-suite flagged run was not repeated; it equals the unflagged run plus the 52
integration items above (1291 passed, 8 failed).

Each commit on its own (worktree at that commit; ruff 0.17.1, ruff format --check, mypy strict
all clean on every commit):

| Commit | Tests |
| --- | --- |
| `ff3bc64` (a) | whole suite: 1110 passed, 44 skipped |
| `2fe4fdf` (b) | whole suite: 1164 passed, 44 skipped |
| `847d952` (c) | same code as the head (only README and decisions differ): the four test files that read them, 112 passed |
| `33fdcf0` (d), head | whole suite: 1247 passed, 52 skipped (run above); the same four files, 112 passed |

Mutation checks (each restored byte-identical, sha256 before and after), all caught:

| Mutation | Caught by |
| --- | --- |
| Revision anchored on the revision time | `test_ac11_revisions` |
| Rule 1 reads the last row (like `prior_tier`), so a reopen needs no rationale | `test_addendum_rule_1` |
| Report open to viewers | `test_ac14_non_reviewers_are_denied_on_both_actions` |
| Ledger append before `escalations.plan` | `test_plan_gets_the_anchor_and_the_deadline_between_the_event_update_and_the_append` |

## Acceptance criteria: exact test ids for REQUIRED (Addendum 9.5)

In `tools/check_acceptance.py` form (`module::function`; parameter suffixes match). Each was
checked against this branch's JUnit file with the tool's own matching rule.

| AC | Test ids |
| --- | --- |
| AC-08 | `tests.part2.test_disposition_api::test_ac08_a_report_without_a_disposition_is_409_and_changes_nothing` |
| AC-09 | `tests.part2.test_disposition_api::test_ac09_not_serious_without_a_rationale_is_contract_example_3`, `…::test_ac09_not_serious_with_a_rationale_closes_with_no_deadline`, `…::test_ac09_each_of_the_four_tiers_is_accepted`, `…::test_ac09_any_other_tier_is_malformed`; at integration also `tests.part2.test_disposition_integration::test_ac09_a_not_serious_first_disposition_has_no_escalation_rows` |
| AC-10 | `tests.part2.test_disposition_api::test_ac10_a_revision_appends_a_row_and_keeps_the_original`, `tests.part2.test_disposition_schema::test_the_table_is_append_only` |
| AC-11 | `tests.part2.test_disposition_api::test_ac11_the_deadline_runs_from_the_assignment_not_the_detection`, `…::test_ac11_revisions`, `tests.part2.test_deadlines::test_calendar_days_worked_examples`, `…::test_working_days_from_a_sunday`, `…::test_working_days_with_holidays` |
| AC-14 | `tests.part2.test_disposition_api::test_ac14_non_reviewers_are_denied_on_both_actions`, `…::test_ac14_the_reviewer_succeeds_on_both_actions` (report half is access control only, D1) |
| AC-15 (edit step) | `tests.part2.test_disposition_api::test_ac15_a_view_by_the_viewer_and_an_edit_by_the_reviewer_are_both_audited` |

`…` stands for the same module as the previous id.

## Tests marked integration (Addendum rule 10) and why

All in `tests/part2/test_disposition_integration.py`; all need Part 3's `escalation` table
(migration 0007) and real `escalations.plan`. Clock: FakeClock.

| Test | Checks |
| --- | --- |
| `test_ac09_a_not_serious_first_disposition_has_no_escalation_rows` | AC-09: NOT_SERIOUS first disposition plans nothing |
| `test_d2_revisions_cancel_and_replan_the_reminders` | D2: NOT_SERIOUS cancels PENDING rows; a reopen puts them back to PENDING at the 15-day instants from the first anchor |
| `test_a_stricter_revision_moves_the_unsent_reminders` | OTHER_SERIOUS then DEATH re-plans the PENDING rows |
| `test_addendum_rule_1_reopens_and_the_reminders` (4 cases) | Rule 1 reopen rows: a 409 leaves rows CANCELLED; successes re-plan them |
| `test_a_looser_revision_without_a_rationale_leaves_the_reminders_alone` | Rule 1: a 409 leaves the DEATH instants |

The expected rows assume the default `ESCALATION_PERCENTAGES` (50, 80, 100), due_at =
`escalations.instant(anchor, deadline, pct)`, and that a later plan returns CANCELLED rows to
PENDING (spec 3.3).

## Stubs relied on

| Stub | Owner | How Part 2 uses it |
| --- | --- | --- |
| `escalations.plan` (base commit: does nothing) | Part 3 | called at step 14 with `(session, event_id, anchor, deadline_utc)`; looked up on the module at call time |

Part 1's functions (`parse_harm_fields`, `require_complete`, `apply_harm_fields`,
`start_if_reviewer`, `events.detail`) are Part 1's real code on this branch, not stubs. Stubs
Part 2 replaced: `dispositions.summary`, `deadlines.compute`, `deadlines.current_calendar`, the
0006 placeholder, `app/api/stub_routes.py` (deleted). No STUB marker remains in Part 2's files.

## Files touched outside the ownership row (Addendum 9.6)

| File | Change | Why |
| --- | --- | --- |
| `app/config.py` | `import re` and `from datetime import date` at the top | the slot's functions need them; ruff E402 forbids imports inside the slot (decision 189) |
| `app/main.py` | two lines in `create_app` calling `deadlines.current_calendar()` | spec 8: a bad setting stops start-up (decision 176). The router lines are Part 2's own |
| `tests/part2/` (new) | Part 2's tests | no owner row names a Part 2 test directory |

Shared slots used: Part 2 slot in `app/config.py`, Part 2 slot in `tests/worker/test_schema.py`,
README section 11, decisions block 174 to 193 (used 174 to 190).

## Phase 3 tests changed (Addendum 9.10)

| Test | Change |
| --- | --- |
| `tests/part1/test_auth.py::test_role_matrix` (ROUTES, STUBS) | report REVIEWER only; allowed role gets 400 (disposition, no tier) and 409 NO_DISPOSITION (report) instead of 501 |
| `tests/part1/test_events_api.py::test_disposition_and_report_stubs` → renamed `test_disposition_and_report_check_the_key_and_role_first` | 400, 409 or 404, 403, 401; still asserts nothing is appended |
| `tests/part1/test_observability.py::test_a_stub_501_is_logged_at_info_and_a_500_at_error` (501 half only) | the 501 comes from the report guard after a recorded disposition |
| `tests/worker/test_schema.py` Part 2 slot | `disposition` columns |

None was weakened; each expected value comes from spec 2.3, 2.8 or Addendum rule 6.

## Decisions

174 to 190 in `docs/decisions.md` (Part 2 block). The ones the other team can observe:
EVENT_NOT_REVIEWABLE's message (180), the request shape rules (175, 178), the strict holiday
setting (176), working days on UTC dates (177).

## UNCERTAINTIES

1. **Branching from Part 1 (174).** Taken at the owner's request; it departs from Addendum 9.2.
   If the team wants strictly independent branches, Part 2 can be rebased onto `4d1487e`; it
   then needs the section 11 stubs for Part 1's functions, and its API tests would run against
   the stub harm-field writes.
2. **Placeholder migrations on shared databases (190).** A database upgraded while 0006 was the
   empty placeholder is at head with no `disposition` table and will never run the real 0006
   upgrade. The downgrade now tolerates this; the upgrade cannot. Any such database (local test
   databases used across part branches; Fly if an image with the placeholders was ever
   deployed) must be downgraded to 0005 or rebuilt. I did not check the Fly database.
3. **Self foreign key on `supersedes_id` and the extra checks (179).** Not in spec 2.9 or rule 5;
   they only reject rows the spec already forbids. Reset truncates `disposition` in one
   statement with the other tables, so the self reference does not block it.
4. **Same-tier revision without a rationale clears `disposition.rationale`.** Spec 2.7 takes
   rationale from the latest row, so a same-tier revision with no rationale shows null even if
   the first had one. Implemented as written.
5. **The report's 501 message** reuses the Phase 3 stub's text; the spec names only the code.
6. **AC-11's 409 case returns before the rationale check when harm fields are incomplete**
   (pinned order: HARM_FIELDS_UNCONFIRMED before DEADLINE_EXTENSION_REQUIRES_RATIONALE). This
   follows spec 7 and is tested.
7. **ruff version.** Checked with the pinned 0.17.1 (run through uvx); the local venv still has 0.17.0.
8. **Not verified here:** the HTTP acceptance runner and demo (Part 4), anything on Fly, native
   (non-Docker) PostgreSQL. Per-commit results are in the section below.

## Known limitations

Working days are UTC days (177); the report is a guard until Phase 5 (D1); only rev-1 is
seeded, so the first-row reviewer and a revision's reviewer cannot differ through the API (the
summary test inserts rows as the owner); the branch includes Part 1's commits (174).
