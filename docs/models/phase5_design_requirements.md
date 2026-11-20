# Phase 5 design requirements and their tests (model coverage)

The cross-team testing interface asks, from Phase 5, for `coverage.model` in a tester's suite
report when the tester and the target belong to the same team: `total` is the number of design
requirements the team lists for the milestone, `covered` the number the suite evaluated, and
`basis` names the design version and links here.

**Design version:** Phase 5 Implementation Specification (Pod H), approved 2 Nov 2026. Its
scope table (section 1.1) lists 26 requirements, numbered here DR-01 to DR-26 in table order.
`app/harness_model.py` holds the same list (`DESIGN_REQUIREMENTS`) and the probe mapping
(`MODEL_TESTS`); `tests/platform5/test_harness_model_coverage.py` checks that this table and the
code agree.

**How `covered` is counted:** a requirement counts once if at least one tester probe mapped to
it ended `pass` or `fail` (an `error` or `skip` did not evaluate it). The probes M-01 to M-12
run only in a same-team session, after T-01 to T-07 and before T-08; a cross-team report has
neither the probes nor `coverage.model`. The other columns are the in-process and HTTP tests
that prove each requirement in CI; they are not counted in `covered`.

| DR | Requirement (spec 1.1) | Spec | Tester probes (counted) | In-process and HTTP tests |
| --- | --- | --- | --- | --- |
| DR-01 | REQ-12 report assembly | 3 | M-03 | `test_ac13_report_matches_the_schema`, `test_ac13_three_timestamps_are_non_null_and_distinct`, `test_report_bytes_are_served_exactly_as_stored`, `test_report_hashes_are_in_the_ledger` |
| DR-02 | JSON and PDF, hashed and stored as produced | 3.4 | M-04 | `test_pdf_and_json_are_produced_once_and_never_regenerated`, `test_pdf_contains_every_required_field`, `test_tampered_report_bytes_are_found_by_the_nightly_check`, HTTP `test_ac13_pdf_download_matches_its_hash` |
| DR-03 | Mark a report as submitted | 3.7 | M-06, M-07 | `test_submission_records_reviewer_time_and_hash`, `test_only_the_current_report_can_be_submitted`, `test_a_report_is_submitted_at_most_once`, `test_submission_after_a_revision_is_a_follow_up`, `test_submission_report_type_is_one_of_four_template_values`, `test_closure_report_cannot_be_submitted` |
| DR-04 | AC-13 schema check | 3.3, 10 | M-03 (required keys) | `test_ac13_report_matches_the_schema`, HTTP `test_ac13_report_over_http_matches_the_schema`, `test_report_carries_the_article73_template_mapping`, `test_closure_report_is_marked_not_for_submission` |
| DR-05 | AC-13 usability review (manual) | 10.6 | — | Manual check M3 |
| DR-06 | AC-14 report half | 3.6 | M-05 | `test_ac14_the_reviewer_gets_the_report_and_others_are_denied`, `test_submission_roles`, HTTP `test_role_matrix` (Δ routes) |
| DR-07 | Phase 4 report guard (409 then 501) replaced | 3.6 | M-02 | `test_report_after_a_disposition_is_200_not_501` |
| DR-08 | REQ-05 retention half; AC-05 | 5 | M-08 | `test_ac05_retrievable_at_5mo29d`, `test_ac05_at_6mo1d_presence_or_absence_is_accepted`, `test_nothing_is_erased_before_retain_until`, HTTP `test_ac05_over_http_retrievable_then_accepted` |
| DR-09 | REQ-16; AC-18 | 4 | M-09 | `test_ac18_personal_data_is_unreadable_after_erasure`, `test_ac18_chain_positions_and_hashes_are_unchanged_and_verify`, `test_ac18_standalone_verifier_agrees_after_erasure`, `test_erasure_before_retain_until_is_409_and_changes_nothing`, `test_report_bytes_are_sealed_at_rest`, HTTP `test_ac18_over_http_erasure_keeps_the_chain` |
| DR-10 | REQ-06 hardening: nightly verification | 6.1 | — (worker schedule, in process) | `test_nightly_verify_records_a_valid_run`, `test_model5_ex5_nightly_verify_finds_a_payload_edit`, `test_nightly_verify_runs_once_per_utc_day` |
| DR-11 | REQ-06 hardening: chain head outside the database | 6.2 | — (CLI `--anchor`) | `test_anchor_detects_truncation_below_the_anchor`, `test_anchor_detects_a_forged_anchored_entry`, `test_anchor_accepts_a_longer_valid_chain` |
| DR-12 | Event-table edits not covered by the chain | 6.3 | M-10 | `test_projection_check_finds_an_edited_event_state`, `test_projection_check_finds_an_edited_deadline`, `test_projection_check_is_clean_after_the_ac16_trace` |
| DR-13 | Health dashboard; operations note | 7 | M-11 | `test_dashboard_roles`, `test_dashboard_shape`, `test_alert_deadline_passed_without_submission`; `docs/operations.md` |
| DR-14 | Deployed-version traceability in `/healthz` | 6.5 | M-12 | `test_healthz_reports_the_app_version` |
| DR-15 | AC-16 scripted trace, both paths | 10.3 | — (needs a webhook receiver) | `test_ac16_in_process_trace_both_paths`, HTTP `test_ac16_breach_to_submission_trace`, `test_ac16_not_serious_trace_has_no_deadline`, `tools/demo_phase5.py` |
| DR-16 | AC-02 real delivery (manual) | 10.6 | — | Manual check M1 |
| DR-17 | AC-09 screen review (manual) | 10.6 | — | Manual check M2 |
| DR-18 | AC-07 manual review of the confirmation record | 10.6 | — | Manual check M4 |
| DR-19 | AC-18 manual chain-integrity review | 10.6 | — | Manual check M5 |
| DR-20 | Model 1 REPORT_READY, SUBMITTED | 3.2, 8 | M-06 | `test_report_status_is_ready_then_submitted` |
| DR-21 | Model 2 `report`, `record_key` | 3.5, 4.3, 8 | — (schema, in process) | `test_phase5_tables_and_grants` |
| DR-22 | Model 5 Ex-5 (nightly verifier) | 6.1 | — (owner role, in process) | `test_model5_ex5_nightly_verify_finds_a_payload_edit` |
| DR-23 | INV-1, 7, 9, 10, 11 | 8.4 | — | The tests listed in spec 8.4 |
| DR-24 | LIM-01 to LIM-15 | 1.2 | — | The handling listed in spec 1.2 |
| DR-25 | Contract delta | 9 | M-01 | HTTP `test_contract_delta_routes_exist` |
| DR-26 | All earlier regression tests | 10.5 | — | The HTTP runner, the unflagged and flagged pytest runs, Model 5, `tools/demo_phase4.py` |

With every probe evaluated, a same-team report shows `covered` 13 and `total` 26.

## The probes

| Probe | Calls | Pass when |
| --- | --- | --- |
| M-01 | Each Δ route of spec 9.2 without a key | 401 on all eight |
| M-02 | Breach on sys-42 (event A), reviewer view, `GET …/report` | 409 `NO_DISPOSITION` |
| M-03 | +4 h, OTHER_SERIOUS on A, +1 h, `GET …/report` | 200 with every top-level key of `report.schema.json`, `schema_version` `pod-h-report-1`, `detected_at < determined_at < assembled_at` |
| M-04 | JSON and PDF, twice each | SHA-256 of each body = `X-Report-Sha256`; the second download is byte-identical |
| M-05 | `GET …/report` as viewer and as sys-42 | 403 for both |
| M-06 | Submission of A's report; `GET …/reports` | 201 `INITIAL`; status `SUBMITTED` |
| M-07 | Drift breach (event B), NOT_SERIOUS with a rationale, report, submission | Report `NOT_SERIOUS_CLOSURE`; submission 409 `NOT_SUBMITTABLE` |
| M-08 | Erasure of A | 409 `RETENTION_NOT_EXPIRED` |
| M-09 | +186 days, erasure of A, report, event, verify | 200; report 410; every evidence null; ledger valid |
| M-10 | `POST /v1/ledger/verifications` | 201, `valid` true, no projection mismatch |
| M-11 | `GET /v1/ops/dashboard` (viewer) | 200 with `version`, `worker`, `ledger` |
| M-12 | `GET /healthz` | Body has `version`, `last_verification`, `reset_count` |

The probes change the target's state (T-07 resets it first, and M-09 moves the test clock by
186 days), which is why they run only against our own system. The next reset puts the clock
back.
