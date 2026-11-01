# Phase 4 Part 3 report: escalations and reminders

Report contents follow Phase 4 Addendum 9.8 and Integration Guide 3.

**Authorship.** Part 3's assigned owner is Arnav. Arvind (Part 2's owner) asked for this
implementation on 2026-11-01, when no Part 3 branch was on GitHub. If Arnav has unpushed work, the
two need comparing before either is merged. This branch has not been pushed unless the team says
otherwise.

## Branch and commits

| Item | Value |
| --- | --- |
| Branch | `phase4-part3-escalations` |
| Starts from | `4580d28` (`origin/phase4-integration`: Parts 1 and 2 merged) |
| Base commit | `4d1487e` (`origin/phase4-base`, fifth base commit) |
| Commits | `97d87bd` (a) migration 0007 and the Phase 3 tests that pin the old notification shape · `e28cd12` (b) plan, fire_due, reminder body, worker pass, health, setting and tests · then the docs commit and this report |

**Deviation from Addendum 9.2 (decision 194).** The branch starts at `phase4-integration`, not at
the bare base commit, because Part 3 is next in the merge order 2, 1, 3, 4. Merging it into
`phase4-integration` adds only Part 3's commits. `phase4-integration` must not be rebased.

## Tests

PostgreSQL 16 in Docker, Python 3.12, Windows 11. ruff 0.17.1 (the pinned version), ruff format
--check and mypy strict are clean.

| Run | Result |
| --- | --- |
| `tests/part3` | 81 passed, 6 skipped |
| This branch, whole suite, no flag | 1330 passed, 58 skipped, 0 failed |
| Commit (a) alone, whole suite | 1249 passed, 52 skipped, 0 failed |
| Trial merge with `origin/phase4-part4-platform` (819550d), `--run-integration --ignore=tests/acceptance` | 1513 passed, 3 failed: see below |
| Same trial merge: `tests/part3` integration tests and Part 2's eight integration tests | 14 passed |
| Same trial merge, live stack (API, worker, sink): `tests/acceptance` over HTTP | 133 passed |
| Same trial merge, live stack: `tools/run_model5.py` | Ex-1, Ex-2, Ex-4 passed |
| Same trial merge, live stack: `tools/demo_phase4.py` | all 10 steps passed; both verifiers valid |

The trial merge was automatic, with no conflicts, and was not committed anywhere.

**The 3 failures are not Part 3's.** They are in Part 4's `tests/api/test_console_static.py`
(`test_elapsed_time_uses_the_simulated_now_and_only_on_an_event_load`,
`test_the_timeline_uses_the_part3_formula`,
`test_api_errors_are_shown_with_error_code_and_message`). Those tests search `app/static/app.js`
for text containing `\n`, and a Windows checkout with `core.autocrlf=true` has CRLF there. With
the static files converted to LF, all 12 tests in that file pass. CI runs on Linux, so I expect no
failure there; that was not run. For Bhanu: reading the file with universal newlines would fix it
on Windows.

**`tools/check_acceptance.py`** exited 0 on the merged run but says the Phase 4 ACs are not yet
wired into REQUIRED. The ids below go there at integration.

Mutation checks, each restored byte-identical (sha256 before and after), all caught:

| Mutation | Caught by |
| --- | --- |
| Fired one second early | `test_one_second_early_fires_nothing_and_the_instant_fires` |
| No due check under the lock | `test_the_due_check_is_repeated_under_the_lock` |
| A reminder sent twice (no status check under the lock) | `test_a_row_already_sent_or_gone_is_skipped` |
| A SENT row is re-planned | `test_a_stricter_revision_after_50_sent` |
| NOT_SERIOUS leaves unsent rows pending | `test_not_serious_cancels_only_unsent_rows_and_a_reopen_does_not_resend` |
| A cancelled row is not re-planned | the same test |
| Latency from received_at | `test_a_receiver_failing_twice_gets_it_on_the_third_attempt_with_latency_from_due_at` |
| Worker delivers before firing | `test_the_worker_runs_the_sweep_then_reminders_then_deliveries` |
| Overdue with no 60 s grace | `test_overdue_escalations_counts_pending_rows_more_than_60_seconds_past_due` |

## Acceptance criteria: exact test ids for REQUIRED (Addendum 9.5)

| AC | Test ids |
| --- | --- |
| AC-12 | `tests.part3.test_escalations::test_ac12_fires_at_50_80_100_and_not_at_49_79_99`, `…::test_one_second_early_fires_nothing_and_the_instant_fires`, `…::test_a_big_jump_fires_every_percentage_once_in_order`, `…::test_no_duplicates_after_further_passes`, `…::test_two_callers_across_the_50_instant_fire_one_reminder_each_time`; at integration also `tests.part3.test_escalations_integration::test_no_duplicates_through_the_clock_advance_endpoint`, `…::test_a_worker_pass_and_a_clock_advance_across_the_50_instant_50_times` |

`…` stands for the same module as the previous id.

## Tests marked integration (Addendum rule 10) and why

All are in `tests/part3/test_escalations_integration.py`. All need Part 2's real endpoint (already
on this branch) and Part 4's clock-advance endpoint, which requires a key and fires due reminders,
so **they fail on this branch until Part 4 merges**. Clock: the database offset, never FakeClock.

| Test | Checks |
| --- | --- |
| `test_model_5_ex_1_a_stricter_revision_through_the_api` | Ex-1 timing |
| `test_a_looser_revision_with_a_rationale_through_the_api` | 15-day instants |
| `test_not_serious_then_reopen_through_the_api` | D2, no resend |
| `test_no_duplicates_through_the_clock_advance_endpoint` | ten further advances and passes |
| `test_a_worker_pass_and_a_clock_advance_across_the_50_instant_50_times` | one reminder each time |
| `test_healthz_shows_a_real_overdue_row` | /healthz field |

## Stubs relied on

No stub remains in Part 3's files. Part 3's branch tests replace `dispositions.summary` as Addendum
rule 3 says (`Disposer` in `tests/part3/conftest.py`). Its answer has `deadline_utc: null`, because
fire_due reads the deadline from the locked event row (decision 198).

## Files touched outside the ownership row (Addendum 9.6)

| File | Change | Why |
| --- | --- | --- |
| `app/main.py` | one line in `create_app` | a bad ESCALATION_PERCENTAGES stops start-up (decision 196) |
| `tests/worker/test_notifications.py` | one expected key | exact `SELECT *` (decision 206) |
| `tests/part3/` (new) | Part 3's tests | no owner row names a Part 3 test directory (decision 195) |

Shared slots used: Part 3 slot in `app/config.py`, Part 3 slot in `tests/worker/test_schema.py`,
README section 12, decisions block 194 to 213 (used 194 to 206).

## Phase 3 tests changed (Addendum 9.10)

| Test | Change |
| --- | --- |
| `tests/worker/test_schema.py::test_notification_references_the_event_once` | `uq_notification_flag` replaces `uq_notification_event` |
| `tests/worker/test_notifications.py::test_enqueue_records_the_request_and_routes_nothing` | gains `escalation_pct: None`; **not listed in 9.10** |
| `tests/worker/test_schema.py` Part 3 slot | `escalation` and `notification.escalation_pct` |

None was weakened.

## Decisions

194 to 206 in `docs/decisions.md`. The ones the other team can observe: the strict setting (196),
the claim order (204) and the reminder body as the spec pins it.

## UNCERTAINTIES

1. **Duplicate work.** See Authorship. Arnav was not consulted from this session.
2. **One call attempts each row once (200).** Spec 3.4 step 7 says repeat until the listing is
   empty; taken literally, a row that keeps failing never ends the call. A row a concurrent revision
   moves earlier during a walk waits for the next pass, at most one worker period later.
3. **Stopping start-up on a bad percentage list (196).** Spec 8 states this only for
   DEADLINE_CALENDAR.
4. **Separate late-reminder counter (202).** "Count it like a late delivery" could mean the same
   number. Nothing reads either counter on /healthz.
5. **Placeholder migrations on shared databases (199).** A database upgraded while 0007 was the
   empty placeholder is at head with no `escalation` table and never runs the real upgrade. It must
   be downgraded to 0006 or rebuilt. Not checked on Fly.
6. **Rollback.** Downgrading 0007 deletes reminder notifications and their delivery rows; the ledger
   entries stay. `tools/verify_ledger.py` is unaffected, but the ledger then names notifications that
   no longer exist.
7. **Not verified here:** anything on Fly, the Docker image and compose stack (the live checks ran
   the processes directly on Windows), native PostgreSQL, and CI on Linux.

## Known limitations

No reminders while machines are stopped (they fire once, late, on wake); webhooks only; at-least-once
delivery; a triggered reminder is still delivered after a revision to NOT_SERIOUS; late routing to a
webhook registered afterwards; a retry can reorder reminders (204).
