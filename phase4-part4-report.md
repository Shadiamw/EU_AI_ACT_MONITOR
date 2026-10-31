# Phase 4 Part 4 report: platform and demo

Owner: Bhanu (Part 4). Report contents follow Phase 4 Addendum 9.8 and Integration Guide 3.
This file is committed after the code head below; later commits may extend it.

## Branch and commits

| Item | Value |
| --- | --- |
| Branch | `phase4-part4-platform` |
| Starts from | Base commit `4d1487e` (`origin/phase4-base`, fifth base commit) |
| Base on main | Main is still at `60d8d08` until the base commits are merged; Parts 1–3 already branched from `4d1487e` |
| Deviation | None from Addendum 9.2: Part 4 continues from the base, not from Part 1/2 |

## Tests

PostgreSQL 16 via docker compose (`localhost:5433`), Python 3.12.

| Run | Result |
| --- | --- |
| `tests/api/test_platform.py` | 6 passed |
| `tests/part1/test_smoke.py` | 1 passed |
| `tests/acceptance` without `--run-integration` | skipped (integration marker) |
| ruff / ruff format --check on touched files | clean |

Whole-suite and flagged integration runs wait until Parts 1–3 are mergeable; Part 3 is not on
origin yet as of this report.

## Acceptance criteria: exact test ids for REQUIRED (Addendum 9.5)

Part 4 owns the runner and the platform contract proofs. Phase 4 AC node ids from Parts 1–3
are listed in those owners' reports and are wired into `tools/check_acceptance.py` at
integration (decision 151 reserved). Platform contract ids on this branch:

| AC / surface | Test ids |
| --- | --- |
| Reset 204 + seed | `tests.api.test_platform::test_reset_leaves_a_valid_seed_and_zero_offset` |
| Clock `{simulated_time}`, gaps via events | `tests.api.test_platform::test_clock_advance_opens_a_gap_after_three_intervals`, `…::test_clock_advance_allows_zero_seconds`, `…::test_gap_events_are_listed_through_the_events_query` |
| 404 before key check | `tests.api.test_platform::test_test_routes_are_404_outside_the_test_environment` |
| Keys required | `tests.api.test_platform::test_test_routes_require_a_key` |
| Smoke (section 9) | `tests.part1.test_smoke::test_spec_section_9_smoke` |
| HTTP contract (integration) | `tests.acceptance.test_platform_contract::test_reset_is_204_with_a_reviewer_key`, `…::test_clock_advance_returns_only_simulated_time`, `…::test_suite_requires_reviewer` |

## Tests marked integration (Addendum rule 10) and why

| Test / artifact | Needs |
| --- | --- |
| `tests/acceptance/*` | Live `BASE_URL` and Parts 1–3 for full AC files |
| `tools/run_model5.py` / Model 5 JSON | Part 1 review start, Part 2 disposition/report, Part 3 reminders |
| `tools/demo_phase4.py` full path | Same; stub disposition exits early on this branch |
| Console reviewer panel (manual AC-09) | Part 2 disposition endpoint |
| `/healthz` real `overdue_escalations` | Part 3 counter |
| Seed `harm_context` row against real table | Part 1 migration 0005 |

## Stubs relied on

| Stub | Owner who replaces it |
| --- | --- |
| `escalations.fire_due` → `[]` | Part 3 |
| `escalations.plan` | Part 3 |
| Disposition / report routes | Part 2 |
| `review.start_if_reviewer` / prefill | Part 1 |
| `HealthValues.overdue_escalations = 0` | Part 3 |

## Files outside ownership that were touched

None beyond Addendum 9.10's assigned Phase 3 tests (`tests/api/test_platform.py`,
`tests/part1/test_smoke.py`).

## Phase 3 tests changed and why

| File | Why |
| --- | --- |
| `tests/api/test_platform.py` | Contract bodies, keys, zero seconds, gap listing |
| `tests/part1/test_smoke.py` | Same; deliveries asserted via webhook receiver, not response fields |

## Decisions used

134 (base) through 148 on this branch; 149–153 reserved for deploy/CI/model follow-ups.

## UNCERTAINTIES

1. Whether the Fly webhook receiver is a second Fly app name chosen by Part 4 or kept as a
   compose-only sink until the development-deploy merge lands (spec 4.9 Open item).
2. Exact REQUIRED node ids for AC-07…AC-17 once Parts 1–3 publish their reports; Part 4 will
   extend `tools/check_acceptance.py` in the reserved decision 151 commit.
3. Part 3 branch is not yet on origin; clock-advance reminder delivery is untested end-to-end
   on this branch.
