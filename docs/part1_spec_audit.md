# Part 1 spec audit

A clause-by-clause check of Part 1 (ingestion, detection, events, auth, error format) against
`Phase3_Implementation_Spec.docx` ("the spec"), the API contract and the Part 2 addendum.
Precedence: contract > Part 2 addendum > spec > Models 4 and 5.

This audit was written on branch `developed-part123merged`, at the merge commit that brings
`main` (atharej's Part 1, `5310ae8`) into `developed-part23merged` (`deb95c5`), **before any
fix**. The "Before" column describes the Part 1 code as merged: it was written without the
spec and against the pre-fix Part 3, and it does not run on this branch's schema (the 0003
stub has no `metric_sample`, `api_key` or `threshold_version`, and `event` has no
`harm_fields` column).

Plan items P1 to P17 refer to `part1-23-integration-plan.md`, kept outside this repository. A
finding the plan does not list is a **NEW FINDING** (N1, N2, ...). Every finding is fixed in the
stage named in the "Stage" column (2 schema, 3 code, 4 tests). The "Proof" column names the test
that shows the fixed behaviour, in `tests/part1/` unless another path is given. The Proof
column may be updated in stage 5 to the final test names; the Before and Status columns are
not changed after this commit.

Statuses:

- **MATCHES**: the merged code already does what the spec says. The proof test exists, or is
  added in stage 4.
- **FIXED BY PLAN ITEM Px**: the plan lists the difference.
- **NEW FINDING Nn**: the plan does not list it.
- **OUT OF SCOPE (Part 4)**: Part 4 work, not built here; its stub stays marked.

## 1. Shared rules (spec section 4)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| 4 Time | All code gets the time from `clock.now()`; UTC | Part 1 calls `clock.now()` only; `tools/seed_part1.py` uses a fixed `datetime(2000, 1, 1)` for threshold validity | MATCHES | 4 | `tests/worker/test_clock_rule.py::test_no_direct_system_time_calls_outside_the_clock_module` |
| 4 Timestamp format, input | ISO-8601 UTC, `Z` or `+00:00`; any other offset is 400 | As specified (pydantic parse, then offset check); naive timestamps 400 | MATCHES | 4 | `test_ingestion.py::test_validation_matrix` |
| 4 Timestamp format, output | `YYYY-MM-DDTHH:MM:SSZ`, fraction only if present | `formats.api_time` | MATCHES | 4 | `test_events_api.py::test_detail_is_the_contract_event_shape_and_records_a_view` |
| 4 IDs | Prefix + UUID: `evt-`, `smp-` | `formats.new_id` | MATCHES | 4 | `test_ingestion.py::test_contract_example_1_flags_and_detail_shows_the_breach` |
| 4 Errors | Every error, unknown routes and 500s included, is `{error_code, message}` | Part 2's `app/errors.py` handlers, kept | MATCHES | 4 | `tests/api/test_errors_roles.py` |
| 4 Error codes | Only MALFORMED_BODY, MISSING_OR_INVALID_KEY, FORBIDDEN_ROLE, SYSTEM_MISMATCH, NOT_FOUND, WEBHOOK_UNVERIFIED, NOT_IMPLEMENTED, INTERNAL_ERROR | Ingest also returns 409 `DUPLICATE_CONFLICT` and 422 `THRESHOLD_NOT_CONFIGURED` | FIXED BY PLAN ITEMS P5, P6 | 3 | `test_ingestion.py::test_duplicate_with_a_changed_value_is_still_a_duplicate`, `test_ingestion.py::test_no_threshold_stores_the_sample_and_returns_201` |
| 4 Auth header | `Authorization: Bearer <key>` | `X-API-Key` header; Bearer is 401 | FIXED BY PLAN ITEM P1 | 3 | `test_auth.py::test_bearer_key_is_accepted`, `test_auth.py::test_x_api_key_header_is_not_accepted` |
| 4 Auth storage | Keys stored only as SHA-256 hashes | As specified | MATCHES | 4 | `test_auth.py::test_keys_are_stored_only_as_sha256_digests` |
| 4 Auth compare | Compared in constant time | Equality lookup on the digest in SQL | NEW FINDING N1 | 3 | `test_auth.py::test_keys_are_compared_with_compare_digest_against_every_stored_key` |
| 4 Role matrix | Ingest SYSTEM only; list any role; disposition REVIEWER only; everything else REVIEWER or VIEWER; 403 FORBIDDEN_ROLE; 401 for missing or unknown key | As specified for ingest, list and detail; disposition and report missing | FIXED BY PLAN ITEM P11 (stubs) | 3 | `test_auth.py::test_role_matrix` |
| 4 Order of checks | 401, then 403, then 400, then SYSTEM_MISMATCH | As specified | MATCHES | 4 | `test_auth.py::test_check_order_401_403_400_mismatch` |
| 4 Actor ids | rev-1, viewer-1, sys-42, `system` | From `api_key.actor_id`; seed uses the spec ids | MATCHES | 4 | `test_auth.py::test_principals_have_the_seeded_actor_ids` |
| 4 Seed fixtures | sys-42 300 s / 3 intervals; `accuracy_drift` drift gt 0.30, `accuracy` performance lt 0.70, version 1; no webhooks; clock offset 0 | Accuracy limit 0.80; `monitored_system.monitoring_started_at` (old Part 3 column); no interval columns; thresholds `thr-1`/`thr-2` with `effective_at` | FIXED BY PLAN ITEMS P3, P13 | 3 | `test_seed.py::test_seed_installs_the_spec_fixtures_from_the_environment`, `test_ingestion.py::test_performance_boundary` |
| 4 Stub shape, breach | disposition null, deadline_utc null, revisions [], four harm fields PREFILLED_PENDING_CONFIRMATION with null evidence | Evidence holds a description string; disposition and revisions read from new JSONB columns | FIXED BY PLAN ITEM P8; NEW FINDING N2 (columns not in spec 1.7) | 2, 3 | `test_events_api.py::test_detail_is_the_contract_event_shape_and_records_a_view` |
| 4 Stub shape, gap | Four harm fields NOT_APPLICABLE, null evidence and confirmer | Reads `event.harm_fields`, which no longer exists; legacy nulls mapped to PREFILLED | NEW FINDING N3 | 3 | `test_events_api.py::test_gap_events_in_list_and_detail` |

## 2. Configuration (spec section 5)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| INGEST_KEY_SYS_42, REVIEWER_KEY, VIEWER_KEY | Required; the seeded credentials | Hard-coded in `tools/seed_part1.py` | FIXED BY PLAN ITEM P2 | 3 | `test_seed.py::test_seed_installs_the_spec_fixtures_from_the_environment` |
| SAMPLING_INTERVAL_SECONDS, GAP_TIMEOUT_INTERVALS | Seeded into sys-42 | Not read | FIXED BY PLAN ITEM P13 | 3 | `test_seed.py::test_seed_installs_the_spec_fixtures_from_the_environment` |
| LOG_LEVEL | Default INFO | Part 1 writes no logs | FIXED BY PLAN ITEM P17 | 3 | `test_observability.py::test_one_json_log_line_per_request` |
| ENVIRONMENT | `test` enables test-only behaviour | The seed refuses outside `test` | MATCHES | 4 | `test_seed.py::test_seed_refuses_outside_the_test_environment` |
| DATABASE_URL, DATABASE_ADMIN_URL | App role and owner role | App uses DATABASE_URL; seed uses DATABASE_ADMIN_URL | MATCHES | 4 | `test_ingestion.py::test_application_cannot_provision_or_mutate_credentials_and_thresholds` |

## 3. Shared interfaces (spec section 6, Pinned)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| `auth.require(*roles) -> Principal(role, actor_id, system_id)` | A framework dependency on every endpoint | As specified; `resolve_principal` overridable | MATCHES | 4 | `test_auth.py::test_role_matrix` |
| `errors.ApiError(status, error_code, message)` plus handlers | Everyone raises it | As specified (Part 2's module) | MATCHES | 4 | `tests/api/test_errors_roles.py` |
| `notifications.enqueue(session, event_id, notification_type)` | Part 1 calls it for breach events in the ingest transaction | Called as `enqueue(session, event_id, principal.actor_id)`: the actor becomes the notification type, so it raises ValueError | FIXED BY PLAN ITEM P13 | 3 | `test_ingestion.py::test_ledger_entries_for_a_new_breach_in_order` |
| `gaps.close_if_open(session, system_id)` | Called in ingest after the system row is locked | As specified | MATCHES | 4 | `test_ingestion.py::test_ingestion_closes_gap_then_flags_and_enqueues` |
| `audit.record_view(session, principal, event_id)` | Called by event detail, same transaction | As specified | MATCHES | 4 | `test_events_api.py::test_detail_is_the_contract_event_shape_and_records_a_view` |
| `ledger.append(...)` | Inside the caller's transaction | As specified; payload values are strings | MATCHES | 4 | `test_ingestion.py::test_ledger_entries_for_a_new_breach_in_order` |
| `events.exists` (Part 2's audit lookup) | An event exists when it is in Part 1's event table | Looks up `event` | MATCHES | 4 | `tests/api/test_audit.py` |

## 4. Transactions and lock order (spec section 7, Pinned)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| One transaction | Every change and its ledger entries commit together or not at all | As specified; commit before the response | MATCHES | 4 | `test_ingestion.py::test_failure_after_enqueue_rolls_back_everything` |
| Lock order | system row, then event/notification rows, then the ledger lock | System row first. But `close_if_open` (step 6) can append GAP_CLOSED, taking the ledger lock, before the open series row is locked (`FOR UPDATE` on `event`) | NEW FINDING N4 | 3 | `test_ingestion.py::test_open_series_is_locked_before_the_ledger_lock` |
| Append late | `ledger.append` as late as possible | EVENT_FLAGGED after the event and sample writes; enqueue last | MATCHES | 4 | `test_ingestion.py::test_ledger_entries_for_a_new_breach_in_order` |
| No network call in a transaction | | Ingest makes none; auth uses its own autocommit connection (decision 114) | MATCHES | 4 | `test_ingestion.py::test_ingestion_notification_reaches_a_real_registered_webhook` |

## 5. Stubs (spec section 8)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| `clock.now()` | Part 4; real UTC until then | Part 2's stub, kept | OUT OF SCOPE (Part 4) | n/a | stub marked in `app/clock.py` |
| `auth.require` stub | Replaced by Part 1 | Replaced (X-API-Key) | FIXED BY PLAN ITEM P1 (header) | 3 | `test_auth.py::test_bearer_key_is_accepted` |

## 6. Spec 1.3 Authentication and errors

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Lookup | SHA-256 of the bearer key, looked up in `api_key` (Part 4 seeds it) | SHA-256 of `X-API-Key`; table from migration 0004 with an extra `enabled` column | FIXED BY PLAN ITEMS P1, P16 | 2, 3 | `test_auth.py::test_bearer_key_is_accepted` |
| 401 cases | No key, unknown key, malformed header | Missing or unknown key 401; there is no header format to be malformed | FIXED BY PLAN ITEM P1 | 3 | `test_auth.py::test_missing_unknown_or_malformed_authorization_is_401` |
| Principal | SYSTEM actor id = system id; rev-1; viewer-1 | From the row | MATCHES | 4 | `test_auth.py::test_principals_have_the_seeded_actor_ids` |
| 403 | Role not in the list | As specified | MATCHES | 4 | `test_auth.py::test_role_matrix` |
| Handlers | Validation 400 MALFORMED_BODY; unknown route 404; unhandled 500 generic, logged, no stack trace | As specified (Part 2's module) | MATCHES | 4 | `tests/api/test_errors_roles.py` |
| Keys never logged | Not even partly | Nothing is logged | MATCHES (re-checked once request logging exists) | 4 | `test_auth.py::test_keys_never_appear_in_logs` |

## 7. Spec 1.4 POST /v1/metrics

| Step | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Request | Five required fields; unknown extra fields ignored | Extra fields are 400 (`extra="forbid"`) | FIXED BY PLAN ITEM P4 | 3 | `test_ingestion.py::test_unknown_extra_field_is_ignored` |
| Request, field limits | Missing or wrong type is 400 | Also 400 for empty or over-256-character ids and names | NEW FINDING N5 | 3 | `test_ingestion.py::test_long_and_empty_names_are_accepted` |
| 1 | No or bad key 401; not SYSTEM 403 | As specified | MATCHES | 4 | `test_auth.py::test_role_matrix` |
| 2 | 400: not JSON, missing field, wrong type, bad metric_type, NaN, Infinity, strings, booleans, bad or non-UTC timestamp; numbers as Decimal | As specified | MATCHES | 4 | `test_ingestion.py::test_validation_matrix` |
| 3 | Body system_id differs from the key's: 403 SYSTEM_MISMATCH | As specified, after validation | MATCHES | 4 | `test_auth.py::test_check_order_401_403_400_mismatch` |
| 4 | Lock the monitored_system row; unknown system after a valid key is 500 | Locks it; unknown system is 404 | FIXED BY PLAN ITEM P12 | 3 | `test_ingestion.py::test_unknown_system_after_a_valid_key_is_500` |
| 5 | Insert-or-ignore on `(system_id, metric_name, timestamp)`; duplicate: set last_received_at, commit, 200 with the original ids; nothing else | Key includes metric_type; 201; last_received_at not set; changed value 409 | FIXED BY PLAN ITEM P5 | 3 | `test_ingestion.py::test_duplicate_returns_200_with_the_original_ids`, `test_ingestion.py::test_duplicate_of_a_breach_returns_the_original_flagged_event_id`, `test_ingestion.py::test_duplicate_with_a_changed_value_is_still_a_duplicate` |
| 5, gaps | Step 5 returns before step 6 | Duplicate returns before `close_if_open` | MATCHES (decision 1 of the integration prompt) | 4 | `test_ingestion.py::test_duplicate_does_not_close_an_open_gap` |
| 6 | last_received_at = clock.now(); `gaps.close_if_open` | As specified | MATCHES | 4 | `test_ingestion.py::test_ingestion_closes_gap_then_flags_and_enqueues` |
| 7 | Newest threshold_version for (system_id, metric_name); none: commit, 201 `{sample_id}` | Newest by `effective_at <= sample timestamp`, keyed also by metric_type; none is 422 | FIXED BY PLAN ITEMS P6, P15 | 3 | `test_ingestion.py::test_no_threshold_stores_the_sample_and_returns_201`, `test_ingestion.py::test_newest_threshold_version_is_used` |
| 8 | Comparator in Decimal; strict | As specified | MATCHES | 4 | `test_ingestion.py::test_drift_boundary`, `test_ingestion.py::test_performance_boundary` |
| 9 | No breach: close the open series (closed_at); 201 `{sample_id}` | Closes only when the sample is newer than every stored one; `series_open` flag on event | FIXED BY PLAN ITEM P15 | 3 | `test_ingestion.py::test_series_reset_creates_a_new_event`, `test_ingestion.py::test_any_healthy_sample_closes_the_series` |
| 10 | Breach, open series: occurrence_count + 1; sample.event_id = series event; nothing else; 201 with the existing id | Joins, no occurrence count | FIXED BY PLAN ITEM P15 | 3 | `test_ingestion.py::test_sustained_breach_joins_the_open_series` |
| 11 | Event, four harm_field rows, breach_series row, EVENT_FLAGGED, enqueue; sample.event_id; 201 | Event row with JSONB harm fields and `series_open`; enqueue with the wrong arguments | FIXED BY PLAN ITEMS P13, P15 | 3 | `test_ingestion.py::test_ledger_entries_for_a_new_breach_in_order` |
| Example 1 | 0.34 against 0.30: 201 `{sample_id, flagged_event_id}` | As specified (on the old schema) | MATCHES | 4 | `test_ingestion.py::test_contract_example_1_flags_and_detail_shows_the_breach` |
| Status codes | 201 created, 200 duplicate | Always 201 | FIXED BY PLAN ITEM P5 | 3 | `test_ingestion.py::test_duplicate_returns_200_with_the_original_ids` |

## 8. Spec 1.5 What a new breach event contains

| Field | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| type, state | threshold_breach, FLAGGED | As specified | MATCHES | 4 | `test_ingestion.py::test_new_event_content` |
| detected_at | The request's timestamp | `clock.now()` | FIXED BY PLAN ITEM P7 | 3 | `test_ingestion.py::test_new_event_content` |
| received_at | `clock.now()` | No column | FIXED BY PLAN ITEM P14 | 3 | `test_ingestion.py::test_new_event_content` |
| observed_value, threshold_value | Sample value and the version's limit | As specified | MATCHES | 4 | `test_ingestion.py::test_new_event_content` |
| threshold_version_id | Version in force | As specified | MATCHES | 4 | `test_ingestion.py::test_new_event_content` |
| evidence_bundle, evidence_sha256 | `{window, samples:[{metric_name, value, timestamp}]}` for this metric, 60 minutes up to and including the trigger; SHA-256 of its canonical JSON | A list of every metric's samples, with extra keys; no hash | FIXED BY PLAN ITEM P14 | 3 | `test_ingestion.py::test_evidence_bundle_window_and_hash_reproduce` |
| retain_until | detected_at + 6 calendar months, at least 183 days | No column | FIXED BY PLAN ITEM P14 | 3 | `test_ingestion.py::test_new_event_content` |
| Harm fields | Four keyed rows, PREFILLED_PENDING_CONFIRMATION, null evidence, confirmer, time | JSONB with a description as evidence | FIXED BY PLAN ITEM P8 | 3 | `test_ingestion.py::test_new_event_content` |

## 9. Spec 1.6 Read endpoints

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| List auth | Any authenticated role, system key included | As specified | MATCHES | 4 | `test_events_api.py::test_list_accepts_every_role_and_writes_nothing` |
| List scoping | No system scoping in the spec | System key limited to its own system; another system_id is 403 | FIXED BY PLAN ITEM P10 | 3 | `test_events_api.py::test_list_accepts_every_role_and_writes_nothing` |
| List filters | system_id, state, type, since (detected_at >= since) | As specified | MATCHES | 4 | `test_events_api.py::test_list_filters` |
| Bad filters | Unknown state or type, bad since: 400 | As specified | MATCHES | 4 | `test_events_api.py::test_list_bad_filters_are_400` |
| limit, offset | Default 50, max 200; offset default 0 | limit above 200 is 400 | NEW FINDING N6 (decision 2: cap at 200) | 3 | `test_events_api.py::test_list_limit_and_offset` |
| List body | Summaries `{event_id, system_id, type, metric_name, detected_at, state}` | Full Event objects | FIXED BY PLAN ITEM P9 | 3 | `test_events_api.py::test_list_returns_summaries_newest_first` |
| List order | detected_at descending, ties by event_id | Ascending | FIXED BY PLAN ITEM P9 | 3 | `test_events_api.py::test_list_returns_summaries_newest_first` |
| List ledger | No entry | As specified | MATCHES | 4 | `test_events_api.py::test_list_accepts_every_role_and_writes_nothing` |
| Detail auth | REVIEWER or VIEWER | As specified | MATCHES | 4 | `test_auth.py::test_role_matrix` |
| Detail body | The contract Event object (Example 2 shape) | Shape right for breaches; gap harm fields wrong (N3) | MATCHES for breaches; N3 | 3 | `test_events_api.py::test_detail_is_the_contract_event_shape_and_records_a_view` |
| Detail numbers | Decimal values as JSON numbers | Through `float()`, which rounds long decimals | NEW FINDING N7 | 3 | `test_events_api.py::test_detail_decimals_are_exact_json_numbers` |
| Detail side effect | `record_view` in the same transaction | As specified | MATCHES | 4 | `test_events_api.py::test_detail_is_the_contract_event_shape_and_records_a_view` |
| Detail 404 | Unknown id 404, no ledger entry | As specified | MATCHES | 4 | `test_events_api.py::test_unknown_event_is_404_and_writes_nothing` |
| Stubs | Disposition REVIEWER only, report REVIEWER or VIEWER, then 501 whether or not the event exists | Routes missing: 404 | FIXED BY PLAN ITEM P11 | 3 | `test_events_api.py::test_disposition_and_report_stubs` |

## 10. Spec 1.7 Data you own, and the Part 1 rows of spec 4.4

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Migration order | 0001 Platform (api_key, threshold_version), 0003 Detection (metric_sample, breach_series, event, harm_field), 0004 Notifications | Part 1 is a second revision `"0004"` that alters the old `event` table | FIXED BY PLAN ITEM P16 | 0, 2 | `test_schema.py::test_alembic_head_is_0004_after_a_full_rebuild` |
| metric_sample | sample_id PK; system_id, metric_name, metric_type, value numeric, timestamp, received_at, event_id null; unique (system_id, metric_name, timestamp) | Unique key includes metric_type; extra NOT NULL `threshold_version_id` | FIXED BY PLAN ITEMS P5, P16 | 2 | `test_schema.py::test_part1_tables_have_the_spec_columns` |
| breach_series | series_id PK; system_id, metric_name, event_id; opened_at, closed_at; occurrence_count from 1; one open series per (system, metric) | Not built (`event.series_open`) | FIXED BY PLAN ITEM P15 | 2 | `test_schema.py::test_part1_tables_have_the_spec_columns` |
| event | spec 1.7 columns; index (detected_at desc, event_id); one open gap per system | The 0003 stub already has them; main's 0004 added `metric_type`, `series_open`, `disposition`, `revisions` | MATCHES (stub kept); N2 for the extra columns | 2 | `tests/worker/test_schema.py` |
| harm_field | (event_id, field_key) PK; status, evidence, confirmed_by, confirmed_at | The 0003 stub has it; Part 1 code does not use it | FIXED BY PLAN ITEM P13 | 3 | `test_ingestion.py::test_new_event_content` |
| api_key | key_hash PK, role, actor_id, system_id null | Has an extra `enabled` column | FIXED BY PLAN ITEM P16 | 2 | `test_schema.py::test_part1_tables_have_the_spec_columns` |
| threshold_version | threshold_version_id PK, system_id, metric_name, metric_type, comparator gt/lt, limit_value, version, valid_from | `threshold_value`, `effective_at`, no `version` | FIXED BY PLAN ITEM P15 | 2 | `test_schema.py::test_part1_tables_have_the_spec_columns` |
| Grants | App role: normal read and write except the ledger | api_key and threshold_version SELECT only; metric_sample no DELETE | MATCHES (stricter is kept for keys and thresholds) | 4 | `test_ingestion.py::test_application_cannot_provision_or_mutate_credentials_and_thresholds` |

## 11. Spec 1.8 Observability

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Request log | One JSON line per request: ts, level, request_id, actor_id, method, path, status, duration_ms, event_id when relevant | None | FIXED BY PLAN ITEM P17 | 3 | `test_observability.py::test_one_json_log_line_per_request` |
| Counters | Samples accepted, duplicates, breaches, series joins, 4xx by code | None | FIXED BY PLAN ITEM P17 | 3 | `test_observability.py::test_counters` |
| Threshold debug log | Every evaluation at debug, with the version id | None | FIXED BY PLAN ITEM P17 | 3 | `test_observability.py::test_threshold_evaluation_debug_line` |
| No secrets | Keys never logged; webhook URLs never logged (decisions 106, 107) | Nothing logged | MATCHES (re-checked) | 4 | `test_auth.py::test_keys_never_appear_in_logs` |

## 12. Spec 1.9 test table

| Case | Before | Status | Proof |
|---|---|---|---|
| Contract Example 1 | Tested on the old schema | MATCHES | `test_ingestion.py::test_contract_example_1_flags_and_detail_shows_the_breach` |
| Drift 0.30 / 0.31 | Tested | MATCHES | `test_ingestion.py::test_drift_boundary` |
| Accuracy 0.70 / 0.69 | Not tested; 0.70 flags with the 0.80 limit | FIXED BY PLAN ITEM P3 | `test_ingestion.py::test_performance_boundary` |
| Sustained breach | Tested | MATCHES | `test_ingestion.py::test_sustained_breach_joins_the_open_series` |
| Series reset | Tested | MATCHES | `test_ingestion.py::test_series_reset_creates_a_new_event` |
| Duplicate sample | Tested as 201 | FIXED BY PLAN ITEM P5 | `test_ingestion.py::test_duplicate_returns_200_with_the_original_ids` |
| Cadence (AC-01) | Not tested | NEW FINDING N8 | `test_ingestion.py::test_ac01_cadence_twelve_samples_no_gap` |
| Validation matrix | Tested | MATCHES | `test_ingestion.py::test_validation_matrix` |
| Auth matrix | Tested with X-API-Key | FIXED BY PLAN ITEM P1 | `test_auth.py::test_auth_matrix_on_ingest` |
| Detail and list | Partly; ordering ascending | FIXED BY PLAN ITEM P9 | `test_events_api.py::test_list_returns_summaries_newest_first`, `test_events_api.py::test_list_filters`, `test_events_api.py::test_list_limit_and_offset`, `test_events_api.py::test_unknown_event_is_404_and_writes_nothing` |
| Stubs | Missing | FIXED BY PLAN ITEM P11 | `test_events_api.py::test_disposition_and_report_stubs` |

## 13. The three contract examples

| Example | Contract | Before | Status | Proof |
|---|---|---|---|---|
| 1, POST /v1/metrics | 201 `{"sample_id": "smp-…", "flagged_event_id": "evt-…"}` | As specified | MATCHES | `test_ingestion.py::test_contract_example_1_flags_and_detail_shows_the_breach` |
| 2, GET /v1/events/evt-7f3a | Event object keys, HarmField objects, numbers as JSON numbers | Shape right, numbers through float | N7 | `test_events_api.py::test_detail_is_the_contract_event_shape_and_records_a_view` |
| 3, disposition NOT_SERIOUS without rationale | 400 RATIONALE_REQUIRED | Route missing | Phase 4. In Phase 3 the spec's phase table makes disposition a stub returning 501 after the role check (decision 125) | `test_events_api.py::test_disposition_and_report_stubs` |

## 14. Appendix B, decisions to confirm with the other team

| Item | Chosen | Proof |
|---|---|---|
| Duplicate sample returns 200 with the original ids | Yes (decision 116) | `test_ingestion.py::test_duplicate_returns_200_with_the_original_ids` |
| Repeated breach returns the existing event id, no new event or notification | Yes | `test_ingestion.py::test_sustained_breach_joins_the_open_series` |
| detected_at equals the sample's own timestamp | Yes | `test_ingestion.py::test_new_event_content` |
| Gap at exactly three missed intervals, from server receive time | Yes (Part 3, decision 92) | `tests/worker/test_gaps_via_ingest.py::test_wrong_sender_clock_neither_stops_nor_fakes_a_gap` |
| Gap notifications reuse flag_notification with type monitoring_gap | Yes (Part 3) | `tests/worker/test_notifications.py` |
| GET /v1/events is a bare array with limit and offset; the system key may list | Yes (decision 117) | `test_events_api.py::test_list_accepts_every_role_and_writes_nothing` |
| Webhook payload and "delivered" = any 2xx within 5 s | Yes (Part 3) | `tests/worker/test_notifications.py` |
| Disposition and report return 501 | Yes | `test_events_api.py::test_disposition_and_report_stubs` |
| Reset clears the ledger, chain restarts at 1 | Part 4 (`ledger.ensure_seed` exists) | OUT OF SCOPE (Part 4) |

## 15. How Part 1 reads Part 3's gap events

| Clause | Spec requires | Before | Status | Proof |
|---|---|---|---|---|
| List | Gap events appear in GET /v1/events with metric_name null | The list query is generic | MATCHES (re-checked) | `test_events_api.py::test_gap_events_in_list_and_detail` |
| Detail | Event object with metric_name, observed_value, threshold_value null and four NOT_APPLICABLE harm fields with null evidence and confirmer | Reads the removed `harm_fields` column | NEW FINDING N3 | `test_events_api.py::test_gap_events_in_list_and_detail` |
| Closed gaps | state CLOSED after recovery | Generic | MATCHES | `test_events_api.py::test_gap_events_in_list_and_detail` |

## 16. New findings, summarised

| # | Finding | Fix |
|---|---|---|
| N1 | Keys matched by SQL equality, not a constant-time compare | Compare the digest with `hmac.compare_digest` against every stored digest |
| N2 | `event.metric_type`, `series_open`, `disposition`, `revisions` columns are not in spec 1.7 | Not created; disposition null and revisions [] come from the Phase 3 stub shape |
| N3 | Gap event detail reads a removed column and would map gap harm fields to PREFILLED | Read the `harm_field` rows |
| N4 | Lock order: GAP_CLOSED can take the ledger lock before the open series row is locked | Lock the open series row right after the system row, before `close_if_open` |
| N5 | Extra 400s for empty or over-256-character ids and names | Removed: the spec rejects only missing fields and wrong types. NUL and lone surrogates stay 400, because PostgreSQL text cannot store them (decision 121) |
| N6 | limit above 200 is 400 | Capped at 200 (decision 118) |
| N7 | Decimals through `float()` | Exact JSON numbers from the stored NUMERIC |
| N8 | No AC-01 cadence test | Added |
