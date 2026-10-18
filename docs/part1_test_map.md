# Part 1 test map

Where each Part 1 test case of the spec is proven, and what happened to the Part 1 tests that
were on `main` (5310ae8). Test ids are pytest node ids under `tests/`. Every expected value in
these tests is taken from the spec or the contract (or from a numbered decision where the spec
is open), and was written before the test first ran.

## Spec 1.9 "Tests you write"

| Spec 1.9 case | Expected (spec) | Test |
|---|---|---|
| Contract Example 1 | 201 with sample_id and flagged_event_id; detail shows threshold_breach, FLAGGED, threshold 0.30 (AC-03) | `part1/test_ingestion.py::test_contract_example_1_flags_and_detail_shows_the_breach` |
| Boundary: drift = 0.30 exactly | 201 {sample_id} only, no event | `part1/test_ingestion.py::test_drift_boundary[0.30-False]` (also `[0.3-False]`) |
| Boundary: drift = 0.31 | Flagged | `part1/test_ingestion.py::test_drift_boundary[0.31-True]` (also `[0.30000000000000000001-True]`, Decimal only) |
| Performance: accuracy = 0.70 / 0.69 | 0.70 no flag; 0.69 flagged (lt) | `part1/test_ingestion.py::test_performance_boundary[0.70-False]`, `[0.69-True]` (also `[0.69999999999999999999-True]`) |
| Sustained breach | Second breach: 201 with the same event id; list shows one event; no second notification | `part1/test_ingestion.py::test_sustained_breach_joins_the_open_series` |
| Series reset | Breach, non-breach, breach creates a new event | `part1/test_ingestion.py::test_series_reset_creates_a_new_event` |
| Duplicate sample | Second call 200, same ids, one sample row, one ledger entry | `part1/test_ingestion.py::test_duplicate_returns_200_with_the_original_ids`, `::test_duplicate_of_a_breach_returns_the_original_flagged_event_id` |
| Cadence (AC-01) | Twelve samples 300 s apart: 201 each, no gap event | `part1/test_ingestion.py::test_ac01_cadence_twelve_samples_no_gap` |
| Validation matrix | Missing field, bad metric_type, "abc", NaN, boolean, bad timestamp, non-UTC offset: 400 MALFORMED_BODY | `part1/test_ingestion.py::test_validation_matrix[*]` (24 cases) |
| Auth matrix | No key 401; wrong key 401; reviewer on ingest 403 FORBIDDEN_ROLE; sys-42 with another system_id 403 SYSTEM_MISMATCH | `part1/test_auth.py::test_auth_matrix_on_ingest` |
| Detail and list | Unknown id 404; filters by system, state, type, since; newest first; limit cap 200 | `part1/test_events_api.py::test_unknown_event_is_404_and_writes_nothing`, `::test_list_filters`, `::test_list_returns_summaries_newest_first`, `::test_list_limit_and_offset` |
| Stubs | Disposition and report: 501 for the right roles, 403 for the wrong ones | `part1/test_events_api.py::test_disposition_and_report_stubs[*]`, `part1/test_auth.py::test_role_matrix[*]` |

## Other Part 1 rules with their own tests

| Rule | Test |
|---|---|
| Bearer accepted, X-API-Key not (spec 4 Auth) | `part1/test_auth.py::test_bearer_key_is_accepted[*]`, `::test_x_api_key_header_is_not_accepted[*]` |
| Missing, unknown or malformed Authorization is 401 (spec 1.3) | `part1/test_auth.py::test_missing_unknown_or_malformed_authorization_is_401[*]` |
| Order of checks 401, 403, 400, SYSTEM_MISMATCH (spec 4) | `part1/test_auth.py::test_check_order_401_403_400_mismatch` |
| Role matrix (spec 4) | `part1/test_auth.py::test_role_matrix[*]` (8 routes x 4 callers) |
| Actor ids sys-42, rev-1, viewer-1 (spec 4) | `part1/test_auth.py::test_principals_have_the_seeded_actor_ids` |
| Keys stored only as SHA-256; constant-time compare (spec 1.3; decision 123) | `part1/test_auth.py::test_keys_are_stored_only_as_sha256_digests`, `::test_keys_are_compared_with_compare_digest_against_every_stored_key[*]` |
| Keys never logged (spec 1.3) | `part1/test_auth.py::test_keys_never_appear_in_logs`, `part1/test_observability.py::test_request_log_never_contains_the_authorization_header` |
| Duplicate identity is (system_id, metric_name, timestamp); changed value is still a duplicate (decision 116) | `part1/test_ingestion.py::test_duplicate_with_a_changed_value_is_still_a_duplicate` |
| A duplicate does not close a gap (decision 116) | `part1/test_ingestion.py::test_duplicate_does_not_close_an_open_gap` |
| Concurrent duplicates and breaches | `part1/test_ingestion.py::test_concurrent_duplicates_create_one_sample_event_and_notification`, `::test_concurrent_breaches_create_one_event` |
| Both UTC forms accepted (spec 4 Timestamp format) | `part1/test_ingestion.py::test_both_utc_forms_are_accepted[*]` |
| Unknown extra fields ignored (spec 1.4) | `part1/test_ingestion.py::test_unknown_extra_field_is_ignored` |
| No length rule on names (decision 121) | `part1/test_ingestion.py::test_long_and_empty_names_are_accepted[*]` |
| No threshold: 201 {sample_id} (spec 1.4 step 7) | `part1/test_ingestion.py::test_no_threshold_stores_the_sample_and_returns_201` |
| Unknown system after a valid key: 500 (spec 1.4 step 4) | `part1/test_ingestion.py::test_unknown_system_after_a_valid_key_is_500` |
| A healthy sample closes the series whatever its timestamp (decision 129) | `part1/test_ingestion.py::test_any_healthy_sample_closes_the_series` |
| New event content (spec 1.5) | `part1/test_ingestion.py::test_new_event_content` |
| Evidence bundle and its SHA-256 (spec 1.5; decision 119) | `part1/test_ingestion.py::test_evidence_bundle_window_and_hash_reproduce` |
| Newest threshold version (spec 1.4 step 7; decision 120) | `part1/test_ingestion.py::test_newest_threshold_version_is_used` |
| EVENT_FLAGGED then NOTIFICATION_REQUESTED, exact payloads (spec 1.4 step 11, 2.4) | `part1/test_ingestion.py::test_ledger_entries_for_a_new_breach_in_order` |
| Atomicity: failure after enqueue rolls everything back (spec 7) | `part1/test_ingestion.py::test_failure_after_enqueue_rolls_back_everything` |
| Lock order: series row before the ledger lock (spec 7; decision 122) | `part1/test_ingestion.py::test_open_series_is_locked_before_the_ledger_lock` |
| Ingest closes a gap, then flags and enqueues (spec 1.4 step 6) | `part1/test_ingestion.py::test_ingestion_closes_gap_then_flags_and_enqueues` |
| A breach reaches a real registered webhook | `part1/test_ingestion.py::test_ingestion_notification_reaches_a_real_registered_webhook` |
| Detail is the contract Event, records EVENT_VIEWED (spec 1.6, REQ-14) | `part1/test_events_api.py::test_detail_is_the_contract_event_shape_and_records_a_view` |
| Exact decimals as JSON numbers (spec 1.6; decision 124) | `part1/test_events_api.py::test_detail_decimals_are_exact_json_numbers` |
| Bad filters are 400 (spec 1.6; decision 118) | `part1/test_events_api.py::test_list_bad_filters_are_400[*]` |
| List: any role, writes nothing, no count header (spec 1.6; decision 117) | `part1/test_events_api.py::test_list_accepts_every_role_and_writes_nothing[*]`, `::test_list_without_a_key_is_401` |
| Gap events in list and detail, Phase 3 stub shape (spec 4) | `part1/test_events_api.py::test_gap_events_in_list_and_detail` |
| Request log line, counters, debug evaluation line (spec 1.8; decision 126) | `part1/test_observability.py::*` |
| Seed fixtures (spec 4, 4.4, 5; decision 128) | `part1/test_seed.py::*` |
| Constraints and indexes of spec 1.7; migration chain of spec 4.4 (decision 127) | `part1/test_schema.py::*`; column types in `worker/test_schema.py::test_stub_and_part3_tables_have_the_spec_columns[*]` |

## Spec 3.9 rows that needed Part 1's ingest

| Spec 3.9 case | Expected | Test |
|---|---|---|
| Duplicate resets silence | A duplicate still updates last_received_at | `worker/test_gaps_via_ingest.py::test_duplicate_resets_silence` |
| Wrong sender clock | Old or future timestamps neither stop nor fake a gap | `worker/test_gaps_via_ingest.py::test_wrong_sender_clock_neither_stops_nor_fakes_a_gap[*]` |
| Recovery (again, through POST /v1/metrics) | The sample closes the gap, GAP_CLOSED; later silence opens a new gap | `worker/test_gaps_via_ingest.py::test_recovery_through_real_ingest_closes_the_gap` |

No Part 3 test was replaced: `worker/test_gaps.py` keeps its in-process `ingest_sample` helper,
which exercises `gaps.close_if_open` on its own, and every Part 3 test id still passes.

## Acceptance criteria

| AC | Tests |
|---|---|
| AC-01 (receiving side) | `part1/test_ingestion.py::test_ac01_cadence_twelve_samples_no_gap` |
| AC-03 | `part1/test_ingestion.py::test_contract_example_1_flags_and_detail_shows_the_breach`, `::test_drift_boundary[*]`, `::test_performance_boundary[*]`, `::test_new_event_content` |
| AC-02, AC-04 | Part 3's tests (unchanged), plus steps 2 and 4 of the smoke test below |
| AC-06, AC-15 | Part 2's tests (unchanged), plus step 5 of the smoke test and `part1/test_events_api.py::test_detail_is_the_contract_event_shape_and_records_a_view` |

## Spec section 9 smoke test

`part1/test_smoke.py::test_spec_section_9_smoke_in_process` runs all five steps in process.

| Step | Status |
|---|---|
| 1. POST /v1/test/reset returns 204; verify is valid with one entry (SEED) | Covered in process; the endpoint is PENDING (Part 4). The fixture truncates and runs the seed stand-in. |
| 2. Register a webhook; Example 1 returns 201 with flagged_event_id; one flag_notification | Covered through the real API |
| 3. Detail is a FLAGGED breach; audit lists EVENT_FLAGGED, NOTIFICATION_REQUESTED, NOTIFICATION_DELIVERED, EVENT_VIEWED | Covered through the real API |
| 4. POST /v1/test/clock/advance 900 s: a monitoring_gap appears and a second webhook call arrives | Covered in process; the endpoint is PENDING (Part 4). The fake clock moves 900 s, then gaps.sweep and notifications.deliver_due run once each (spec 4.6). |
| 5. Export, change one payload offline, the standalone verifier reports the first bad seq | Covered; the verifier runs as a separate process |

## What happened to the Part 1 tests on main (5310ae8)

All 34 ids were in `tests/part1/test_ingestion.py` (plus one in `tests/api/test_errors_roles.py`).
They were rewritten to the spec; none was dropped without a replacement.

| Old id (main) | Now | Why |
|---|---|---|
| `test_breach_detail_audit_and_independent_ledger_verification` | `test_contract_example_1_flags_and_detail_shows_the_breach`, `test_ledger_entries_for_a_new_breach_in_order`, `test_smoke.py::test_spec_section_9_smoke_in_process` | The old test expected harm-field evidence text and the old Example 1 time; spec 4 says evidence is null. |
| `test_threshold_direction_and_equality[*]` (6) | `test_drift_boundary[*]`, `test_performance_boundary[*]` | The old accuracy limit was 0.8; the spec seeds lt 0.70. |
| `test_invalid_value_is_malformed[*]` (6) | `test_validation_matrix[*]` | Same cases, now named after the spec row. |
| `test_invalid_body_writes_nothing[changes0..6]` (7) | `test_validation_matrix[*]`, `test_unknown_extra_field_is_ignored`, `test_long_and_empty_names_are_accepted[]` | The spec says unknown extra fields are ignored (the old test wanted 400) and sets no length rule, so "" is accepted (decision 121). |
| `test_authentication_authorization_and_system_binding` | `test_auth.py::test_auth_matrix_on_ingest`, `::test_role_matrix[*]`, `::test_check_order_401_403_400_mismatch` | The old test used X-API-Key; the spec says Bearer. |
| `test_disabled_key_and_hash_storage` | `test_auth.py::test_keys_are_stored_only_as_sha256_digests` | spec 4.4 has no `enabled` column. |
| `test_duplicate_replay_and_conflict_do_not_change_heartbeat_or_ledger` | `test_duplicate_*` (4 tests) | The spec: a duplicate sets last_received_at, and a changed value is still a duplicate (200, not 409; decision 116). |
| `test_concurrent_duplicate_requests_create_one_sample_event_and_notification` | `test_concurrent_duplicates_create_one_sample_event_and_notification` | Duplicates are now 200 after the first 201. |
| `test_series_occurrences_recovery_and_late_healthy_sample` | `test_sustained_breach_joins_the_open_series`, `test_series_reset_creates_a_new_event`, `test_any_healthy_sample_closes_the_series` | Split by spec row; a late healthy sample now closes the series (spec 1.4 step 9; decision 129). |
| `test_threshold_versions_are_selected_at_sample_time_and_pinned` | `test_newest_threshold_version_is_used` | The spec says the newest version, not the one valid at the sample's time (decision 120). |
| `test_missing_threshold_and_unknown_system` | `test_no_threshold_stores_the_sample_and_returns_201`, `test_unknown_system_after_a_valid_key_is_500` | The spec: no threshold is 201 (old: 422), unknown system is 500 (old: 404). |
| `test_evidence_snapshot_preserves_decimal_and_preceding_hour` | `test_evidence_bundle_window_and_hash_reproduce` | The spec's bundle is {window, samples}, not a bare list. |
| `test_ingestion_closes_gap_then_flags_and_enqueues` | same name | Gap harm fields are NOT_APPLICABLE (spec 4); the old test expected PREFILLED. |
| `test_failure_rolls_back_sample_gap_event_notification_and_heartbeat` | `test_failure_after_enqueue_rolls_back_everything` | Same rule on the spec's tables (no monitoring_gap table). |
| `test_listing_filters_pagination_and_role_scope` | `test_events_api.py::test_list_*` | The spec: any role sees every event; no X-Total-Count; limit above 200 is capped, limit=0 is allowed (decisions 117, 118). |
| `test_ingestion_notification_reaches_a_real_registered_webhook` | same name | Bearer keys. |
| `test_application_cannot_provision_or_mutate_credentials_and_thresholds[api_key, threshold_version]` | `worker/test_schema.py::test_application_role_has_normal_read_write_outside_the_ledger[api_key, threshold_version]` (Part 3, unchanged) | Spec 4.4 pins normal read/write for the application role on every table except the ledger, so the stricter grant was dropped (decision 127). |
| `api/test_errors_roles.py::test_real_auth_requires_a_key` (main only) | `test_auth.py::test_role_matrix[GET /v1/events/{id}/audit-None]`, `api/test_errors_roles.py::test_the_default_stub_principal_is_rev_1` | Part 2's test is kept and now uses a real Bearer key. |

## Part 2 and Part 3 tests changed because the stubs were replaced

These tests used the Phase 3 stubs that Part 1 replaces (the fixed REVIEWER principal of spec 8,
and Part 2's stand-in event route). Each still exists under the same id and passes.

| Test | Change |
|---|---|
| `api/test_errors_roles.py::test_the_default_stub_principal_is_rev_1` | Uses the seeded rev-1 Bearer key instead of the stub's fixed principal. |
| `api/test_errors_roles.py::test_the_real_app_does_not_mount_the_test_only_routes` | GET /v1/events/{id} is now Part 1's real route (200 with a key) instead of a test-only one. |
| `api/test_audit.py` (`view()` helper) | The detail body is now the contract Event object. |
| `api/test_ledger_system.py::test_e_audit_lists_the_event_and_a_view_appends_one_entry` | Inserts the event row the real detail route reads. |
| `api/conftest.py::flag_event`, `tests/api/g2_routes.py` | The event row is written as Part 1's ingest writes it; the stand-in detail route is removed. |
| `worker/test_webhooks.py::test_the_real_app_mounts_the_webhook_route` | Real Bearer key. |
| `worker/test_schema.py::test_stub_and_part3_tables_have_the_spec_columns[*]` | Adds Part 1's and Part 4's tables (8 new parametrized ids). |
| `worker/test_schema.py::test_the_schema_has_exactly_the_ledger_stub_and_part3_tables`, `::test_application_role_has_normal_read_write_outside_the_ledger[*]` | The table list includes api_key, threshold_version, metric_sample and breach_series. |
