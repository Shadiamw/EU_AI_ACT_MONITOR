# Part 3 spec audit

A clause-by-clause check of Part 3 (worker: gap sweeper, notifications, webhooks) against
`Phase3_Implementation_Spec.docx` ("the spec"), the API contract and the Part 2 addendum.
Precedence: contract > Part 2 addendum > spec > Models 4 and 5.

This audit was written on branch `developed-part23merged`, starting from the tip of
`development-gap-sweeper` (`8fc874a`), **before any fix**. The "Before" column describes that
commit. Plan items A1 to D4 refer to `plans/part2-part3-integration-plan.md` in the project
library. A finding the plan does not list is a **NEW FINDING** (N1, N2, ...). Every finding is
fixed in the stage named in the "Stage" column. The "Proof" column names the test that shows the
fixed behaviour, in `tests/worker/` unless another path is given.

Statuses:

- **MATCHES**: the branch already does what the spec says. The proof test exists, or is added in
  stage 4.
- **FIXED BY PLAN ITEM x**: the plan lists the difference.
- **NEW FINDING Nn**: the plan does not list it.
- **OUT OF SCOPE**: Part 1 or Part 4 work, not built here.

## 1. Shared rules (spec section 4)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| 4 Time | All code gets the time from `clock.now()`; no direct "now" calls; UTC | Part 3 calls `clock.now()` only. `app/worker.py` uses `time.monotonic` for loop pacing, which is not a time-of-day read. Grep of `app/`, `tools/`, `migrations/` for `datetime.now`, `utcnow`, `time.time`, `time_ns`, `date.today`, `localtime`, `gmtime`: only `app/clock.py` | MATCHES | 4 | `test_clock_rule.py::test_no_direct_system_time_calls_outside_the_clock_module` |
| 4 Timestamp format | Responses: `YYYY-MM-DDTHH:MM:SSZ`, fraction only if present | `formats.api_time` does this, but the webhook body has no `detected_at` or `sent_at` (see 3.5) | MATCHES (format); body is A2 | 3 | `test_notifications.py::test_delivery_body_is_exactly_the_spec_shape` |
| 4 IDs | Prefix + UUID: `evt-`, `ntf-`, `wh-` | `formats.new_id` | MATCHES | 4 | `test_gaps.py::test_a_gap_event_has_the_spec_fields_and_four_not_applicable_harm_fields`, `test_webhooks.py::test_register_pings_then_stores_and_appends` |
| 4 Errors, contract body | Every error is `{error_code, message}` | Part 2's `app/errors.py` stub | MATCHES | 4 | `test_webhooks.py::test_a_malformed_body_is_400_and_nothing_is_pinged` |
| 4 Error codes | `WEBHOOK_UNVERIFIED (422)` | `WEBHOOK_VERIFICATION_FAILED` | FIXED BY PLAN ITEM A1 | 3 | `test_webhooks.py::test_a_non_2xx_ping_is_422_webhook_unverified_and_writes_nothing` |
| 4 Error codes | `MALFORMED_BODY 400`, `MISSING_OR_INVALID_KEY 401`, `FORBIDDEN_ROLE 403`, `NOT_FOUND 404`, `INTERNAL_ERROR 500` | As specified (Part 2 stub) | MATCHES | 4 | `test_webhooks.py::test_no_principal_is_401_and_a_system_key_is_403`, `test_webhooks.py::test_wrong_method_is_404` |
| 4 Role matrix | `POST /v1/webhooks`: REVIEWER or VIEWER; SYSTEM 403; no key 401 | As specified | MATCHES | 4 | `test_webhooks.py::test_no_principal_is_401_and_a_system_key_is_403` |
| 4 Order of checks | 401, then 403, then body validation (400), then the ping | Not tested with a bad body and a bad key together | NEW FINDING N1 (untested) | 4 | `test_webhooks.py::test_auth_is_checked_before_the_body` |
| 4 Actor ids | Background work is `system` | Worker entries use `system` | MATCHES | 4 | `test_gaps.py::test_gap_ledger_entries_have_exact_payload_keys_actor_and_subject` |
| 4 Seed fixtures | sys-42: interval 300 s, timeout 3 intervals; no webhooks | Part 4's seed. The sweeper reads the timeout from env, not from the system row | FIXED BY PLAN ITEM D1 | 2 | `test_gaps.py::test_timeout_comes_from_the_system_row` |
| 4 Stub shape | Gap event: four harm fields `NOT_APPLICABLE`, null evidence, null confirmer | All four harm fields `null` (decision 84) | FIXED BY PLAN ITEM A6 | 3 | `test_gaps.py::test_a_gap_event_has_the_spec_fields_and_four_not_applicable_harm_fields` |

## 2. Configuration (spec section 5)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| ENVIRONMENT | Required. `test` enables test-only behaviour | Not read by Part 3 | FIXED BY PLAN ITEM A9 | 3 | `test_webhooks.py::test_outside_test_refused_urls_are_400_and_never_pinged` |
| NOTIFY_MAX_DELAY_SECONDS | Default 900; judges lateness, never delays sending | As specified | MATCHES | 4 | `test_notifications.py::test_within_bound_compares_latency_with_the_configured_bound`, `test_config.py::test_part3_settings_and_defaults` |
| SWEEP_INTERVAL_SECONDS | Default 60 (real time) | As specified | MATCHES | 4 | `test_worker_system.py::test_the_loop_sweeps_every_minute_and_survives_a_failed_iteration` |
| DELIVERY_POLL_SECONDS | Default 5 (real time) | As specified | MATCHES | 4 | same as above |
| SAMPLING_INTERVAL_SECONDS, GAP_TIMEOUT_INTERVALS | Seeded into sys-42 only | The sweeper uses them for every system | FIXED BY PLAN ITEM D1 | 2 | `test_gaps.py::test_env_sampling_settings_do_not_change_the_sweeper` |
| LOG_LEVEL | Default INFO | The worker ignores it | NEW FINDING N2 | 2 | `test_worker_system.py::test_the_worker_uses_log_level` |
| No other variables | The table lists all variables | `WEBHOOK_TIMEOUT_SECONDS`, `DELIVERY_MAX_ATTEMPTS` | FIXED BY PLAN ITEM D3 | 2 | `test_config.py::test_part3_settings_and_defaults` |

## 3. Shared interfaces (spec section 6, Pinned)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| `notifications.enqueue` | `(session, event_id, notification_type="flag_notification") -> notification_id`; appends NOTIFICATION_REQUESTED itself, in the caller's transaction | Extra required `actor_id` argument | FIXED BY PLAN ITEM B1 | 2 | `test_notifications.py::test_enqueue_records_the_request_and_routes_nothing` |
| `gaps.close_if_open` | `(session, system_id) -> event_id or None`, inside ingest, after the system row is locked | As specified; never commits | MATCHES | 4 | `test_gaps.py::test_close_if_open_does_not_commit` |
| `gaps.sweep` | `(db) -> list of new gap event ids` | As specified | MATCHES | 4 | `test_gaps.py::test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do` |
| `notifications.deliver_due` | `(db) -> int` | As specified | MATCHES | 4 | `test_notifications.py::test_each_subscribed_webhook_gets_its_own_delivery` |
| `ledger.append` | Called last in the caller's transaction | As specified | MATCHES | 4 | `test_gaps.py::test_gap_ledger_entries_have_exact_payload_keys_actor_and_subject` |
| `clock.now()` | Part 4 owns it; the stub returns real UTC time | Part 2's stub | MATCHES | n/a | Part 2 tests |

## 4. Transactions and lock order (spec section 7, Pinned)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| One transaction | A state change and its ledger entries commit together | Gap: event, gap row, GAP_OPENED and the notification in one transaction | MATCHES | 4 | `test_gaps.py::test_atomicity_a_failure_after_enqueue_leaves_no_event_and_no_notification` |
| Lock order | System row, then event or notification rows, then the ledger lock | Sweeper and `close_if_open`: system row, gap row, ledger. Delivery: delivery row, ledger | MATCHES | 4 | `test_gaps.py::test_concurrent_sweeps_open_exactly_one_gap` |
| Append late | `ledger.append` as late as possible | As specified (spec 3.4 fixes GAP_OPENED before enqueue) | MATCHES | 4 | `test_gaps.py::test_gap_ledger_entries_have_exact_payload_keys_actor_and_subject` |
| No network in a transaction | No webhook call while a lock is held or after appending in an open transaction | Delivery: claim committed before the call. Registration: ping before the first write | MATCHES | 4 | `test_notifications.py::test_delivery_appends_delivered_with_integer_latency_and_boolean_bound`, `test_webhooks.py::test_register_pings_then_stores_and_appends` |

## 5. Stubs (spec section 8)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Stub table | `clock.now` real time; `ledger.append` dummy; `audit.record_view` nothing; `notifications.enqueue` random id; `gaps.close_if_open` None; `auth.require` fixed REVIEWER | Part 3 is real, so the enqueue and close_if_open stubs are gone; the Part 1 and Part 4 stubs remain | MATCHES | 5 | Stubs listed in `integration_notes.md` |
| Stub data | Part 3 needs Part 1's `event` and `harm_field` and Part 4's `monitored_system` and `sweeper_heartbeat` | One 0001 stub mixes Part 1 and Part 4 tables, with non-spec columns | FIXED BY PLAN ITEMS C1, C2, C3 | 1 | `tests/api/test_ledger_system.py` (head 0004), `test_schema.py::test_stub_and_part3_tables_have_the_spec_columns` |

## 6. Part 1 data that Part 3 reads and writes (spec 1.5, 1.7)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| 1.7 event columns | `event_id, system_id, type, metric_name, observed_value (numeric), threshold_value (numeric), threshold_version_id, detected_at, received_at, state, deadline_utc, evidence_bundle, evidence_sha256, retain_until` | Stub event: text values, `harm_fields` jsonb, `closed_at`; no `received_at`, `retain_until`, `threshold_version_id`, `deadline_utc`, `evidence_*` | FIXED BY PLAN ITEM C2 | 1 | `test_schema.py::test_stub_and_part3_tables_have_the_spec_columns` |
| 1.7 event index | `(detected_at desc, event_id)` | Missing | FIXED BY PLAN ITEM C2 | 1 | `test_schema.py::test_event_indexes_and_state_check` |
| 1.7 state check | FLAGGED, UNDER_REVIEW, DISPOSITIONED, CLOSED | Missing | FIXED BY PLAN ITEM C2 | 1 | `test_schema.py::test_event_indexes_and_state_check` |
| 1.7 harm_field | Table keyed `(event_id, field_key)`; status PREFILLED_PENDING_CONFIRMATION, CONFIRMED, NOT_APPLICABLE | Missing (jsonb column) | FIXED BY PLAN ITEM C2 | 1 | `test_schema.py::test_stub_and_part3_tables_have_the_spec_columns` |
| 1.7 open-gap index | Partial unique index on `event(system_id)` where `type = monitoring_gap` and `state = FLAGGED` | On a separate `monitoring_gap` table instead | FIXED BY PLAN ITEMS C2, C4 | 1 | `test_gaps.py::test_the_database_refuses_a_second_open_gap` |
| 1.5 retain_until | Detected time + six calendar months, at least 183 days | Not stored | FIXED BY PLAN ITEM D4 | 2 | `test_formats.py::test_retain_until_is_the_later_of_six_months_and_183_days`, `test_gaps.py::test_a_gap_detected_on_1_september_is_retained_until_3_march` |

## 7. Ledger entries written by Part 3 (spec 2.3, 2.4)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| GAP_OPENED | `{event_id, system_id, last_received_at}`, subject event id | Keys match. `last_received_at` is null for a system that never sent data (this goes away with C3) | MATCHES (after C3) | 2 | `test_gaps.py::test_gap_ledger_entries_have_exact_payload_keys_actor_and_subject` |
| GAP_CLOSED | `{event_id, system_id, gap_seconds}` | Keys match; `gap_seconds` counts from the last sample | FIXED BY PLAN ITEM A7 | 2 | `test_gaps.py::test_recovery_closes_the_gap_and_counts_from_detection` |
| NOTIFICATION_REQUESTED | `{notification_id, event_id, notification_type}`, subject event id | Keys match; actor is the caller's | FIXED BY PLAN ITEM B1 (actor `system`) | 2 | `test_notifications.py::test_notification_ledger_entries_have_exact_payload_keys` |
| NOTIFICATION_DELIVERED | `{notification_id, webhook_id, attempt, latency_seconds, within_bound}` | Keys match; latency counts from `requested_at` | FIXED BY PLAN ITEM A8 | 2 | `test_notifications.py::test_latency_counts_from_the_events_received_at` |
| NOTIFICATION_FAILED | `{notification_id, webhook_id, attempts, last_status}` | Keys match | MATCHES | 4 | `test_notifications.py::test_notification_ledger_entries_have_exact_payload_keys` |
| WEBHOOK_REGISTERED | `{webhook_id, url, event_types}`, subject webhook id | Keys match; the URL may be refused before this point (A10) | MATCHES | 4 | `test_webhooks.py::test_urls_with_query_credentials_or_fragment_are_accepted_as_received` |
| Addendum rule 2 | Integers stay integers; `latency_seconds` is an integer | As specified | MATCHES | 4 | `test_notifications.py::test_delivery_appends_delivered_with_integer_latency_and_boolean_bound` |
| Audit order | A gap's audit: GAP_OPENED, NOTIFICATION_REQUESTED, NOTIFICATION_DELIVERED, GAP_CLOSED | As specified | MATCHES | 4 | `test_worker_system.py::test_gap_to_delivery_to_close_and_reverify` |

## 8. Part 3 recap and worker (spec 3.2, 3.3)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| 3.2 Gap threshold | `gap_timeout_intervals × sampling_interval_seconds` per system; exactly three intervals opens a gap | Threshold from env; ≥ comparison (decision 92) | FIXED BY PLAN ITEM D1 (comparison MATCHES) | 2 | `test_gaps.py::test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do` |
| 3.2 Separate processes | Never cache the clock offset | Clock read on every call | MATCHES | n/a | Part 4's clock |
| 3.3 Process | Same image, different command | `python -m app.worker` | MATCHES | n/a | README |
| 3.3 Two loops | Delivery every 5 s, sweeper every 60 s, on real elapsed time | One loop that runs both schedules (the structure is Open, spec 3.9 box) | MATCHES | 4 | `test_worker_system.py::test_the_loop_sweeps_every_minute_and_survives_a_failed_iteration` |
| 3.3 Resilience | Every iteration catches and logs all exceptions; one bad row never stops the worker | The loop catches everything, but a non-database error for one system or one delivery aborts the rest of that pass | NEW FINDING N3 | 2 | `test_gaps.py::test_a_failure_for_one_system_does_not_stop_the_others` |
| 3.3 Heartbeat | After every sweep, write the real time to `sweeper_heartbeat.last_ran_at` | Not written | FIXED BY PLAN ITEM C7 | 2 | `test_gaps.py::test_sweep_writes_the_wall_clock_heartbeat` |
| 3.3 Reset | Tolerate tables being emptied by a test reset mid-run | Untested | NEW FINDING N4 (untested) | 4 | `test_worker_system.py::test_the_loop_survives_a_reset_that_empties_the_tables` |

## 9. Gap sweeper (spec 3.4)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Step 1 | Lock each system row; skip it if another process holds it | Waits on `FOR UPDATE` | FIXED BY PLAN ITEM D2 | 2 | `test_gaps.py::test_a_locked_system_row_is_skipped_not_waited_for` |
| Step 2 | Silence = `clock.now() − last_received_at`, which starts at seed time | Falls back to `monitoring_started_at` | FIXED BY PLAN ITEM C3 | 1 | `test_gaps.py::test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do` |
| Step 3 | Silence ≥ threshold and no open gap: create the event and four harm_field rows, append GAP_OPENED, then enqueue; commit | Harm fields null; separate `monitoring_gap` row | FIXED BY PLAN ITEMS A6, C4 | 2, 3 | `test_gaps.py::test_a_gap_event_has_the_spec_fields_and_four_not_applicable_harm_fields` |
| Step 4 | One event per outage, however long | As specified | MATCHES | 4 | `test_gaps.py::test_one_event_per_outage_after_another_1000_seconds` |
| Gap event fields | type `monitoring_gap`, state FLAGGED; metric fields null; `detected_at` = `received_at` = now; evidence null; `retain_until`; four NOT_APPLICABLE harm fields | No `received_at` or `retain_until`; harm fields null | FIXED BY PLAN ITEMS A6, D4 | 2, 3 | `test_gaps.py::test_a_gap_event_has_the_spec_fields_and_four_not_applicable_harm_fields` |
| `close_if_open` | Sets CLOSED, appends GAP_CLOSED with seconds from the gap's `received_at` to now, returns the id | Counts from the last sample | FIXED BY PLAN ITEM A7 | 2 | `test_gaps.py::test_recovery_closes_the_gap_and_counts_from_detection` |
| Later outage | A later silence opens a new gap | As specified | MATCHES | 4 | `test_gaps.py::test_after_closing_a_new_silence_opens_a_new_gap` |

## 10. Notifications (spec 3.5)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Enqueue | Inserts a notification row and appends NOTIFICATION_REQUESTED in the caller's transaction | Also creates delivery rows | FIXED BY PLAN ITEM A5 | 2 | `test_notifications.py::test_enqueue_records_the_request_and_routes_nothing` |
| Step 1 Route | In the pass: unrouted notifications get one PENDING row per matching webhook; none means wait; later webhooks do not get already-routed ones | Routed at enqueue; no webhook means never sent | FIXED BY PLAN ITEM A5 | 2 | `test_notifications.py::test_no_webhook_leaves_the_notification_unrouted_until_one_is_registered`, `test_notifications.py::test_a_routed_notification_is_not_sent_to_a_webhook_registered_later` |
| Step 2 Claim | PENDING and due rows, `FOR UPDATE SKIP LOCKED`, short lease, commit before any call | Uses an extra IN_FLIGHT status | FIXED BY PLAN ITEM C5 | 1, 2 | `test_notifications.py::test_a_claimed_delivery_is_not_taken_by_a_second_pass` |
| Step 3 Send | HTTP POST, the spec body, 5 s timeout, no lock held | Timeout from env (default 5); old body | FIXED BY PLAN ITEMS A2, D3 | 2, 3 | `test_notifications.py::test_the_send_timeout_is_five_seconds`, `test_notifications.py::test_delivery_body_is_exactly_the_spec_shape` |
| Step 4 Success | Any 2xx: DELIVERED and NOTIFICATION_DELIVERED in a new transaction | As specified | MATCHES | 4 | `test_notifications.py::test_delivery_appends_delivered_with_integer_latency_and_boolean_bound` |
| Step 5 Failure | Retries after 10, 30, 60, 120, 300 s; six attempts; then FAILED and NOTIFICATION_FAILED | 30/60/120/240 s; five attempts | FIXED BY PLAN ITEM A4 | 2 | `test_notifications.py::test_retries_500_500_200_make_exactly_three_attempts`, `test_notifications.py::test_permanent_failure_stops_after_six_attempts` |
| Body | `{notification_type, notification_id, event_id, system_id, type, detected_at, sent_at}` | `{type, notification_id, webhook_id, event_id, requested_at, attempt}` | FIXED BY PLAN ITEM A2 | 3 | `test_notifications.py::test_delivery_body_is_exactly_the_spec_shape` |
| At least once | A crash between send and record can duplicate; `notification_id` lets receivers de-duplicate | As specified | MATCHES | 4 | `test_notifications.py::test_a_lock_timeout_while_recording_is_rolled_back_and_retried` |
| Latency | Delivery time minus the event's `received_at`; `within_bound` when ≤ the bound | Counts from `requested_at` | FIXED BY PLAN ITEM A8 | 2 | `test_notifications.py::test_latency_counts_from_the_events_received_at` |
| Late deliveries | Log a warning and count every late delivery | Neither | NEW FINDING N5 | 2 | `test_notifications.py::test_late_delivery_is_logged_and_counted` |
| Isolation | Webhook failures never affect ingest or event creation | Delivery runs outside the event transaction | MATCHES | 4 | `test_gaps.py::test_a_gap_event_has_the_spec_fields_and_four_not_applicable_harm_fields` (gap committed with no webhook) |

## 11. POST /v1/webhooks (spec 3.6)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Auth | REVIEWER or VIEWER | As specified | MATCHES | 4 | `test_webhooks.py::test_no_principal_is_401_and_a_system_key_is_403` |
| Request | `url` an http or https URL; `event_types` a non-empty subset of `flag_notification`, `reminder`; anything else 400 | Also refuses credentials, query strings, fragments (A10) and URLs over 2048 characters; caps `event_types` at 10 entries | FIXED BY PLAN ITEM A10; NEW FINDING N6 (length cap and entry cap) | 3 | `test_webhooks.py::test_urls_with_query_credentials_or_fragment_are_accepted_as_received`, `test_webhooks.py::test_a_long_url_and_many_event_types_are_accepted` |
| Verification | Ping `{notification_type: "verification_ping", sent_at}`, 5 s timeout; non-2xx, timeout or connection error: 422 WEBHOOK_UNVERIFIED and nothing stored; no lock held | Other ping body; other code | FIXED BY PLAN ITEMS A1, A3 | 3 | `test_webhooks.py::test_ping_body_is_exactly_the_spec_shape`, `test_webhooks.py::test_an_unreachable_url_is_422_webhook_unverified_and_writes_nothing` |
| Success | 201 `{webhook_id}`; WEBHOOK_REGISTERED with subject the webhook id | As specified | MATCHES | 4 | `test_webhooks.py::test_register_pings_then_stores_and_appends` |
| Non-test | https only; refuse private or loopback addresses; test allows any | Not implemented | FIXED BY PLAN ITEM A9 | 3 | `test_webhooks.py::test_outside_test_refused_urls_are_400_and_never_pinged`, `test_webhooks.py::test_outside_test_a_hostname_resolving_to_a_private_address_is_refused`, `test_webhooks.py::test_outside_test_a_public_https_url_is_accepted` |
| Reminder type | Accepted, never sent in Phase 3 | As specified | MATCHES | 4 | `test_webhooks.py::test_register_pings_then_stores_and_appends` |

## 12. Part 3 data (spec 3.7)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| webhook | `webhook_id, url, event_types (text[]), created_at` | `registered_by, registered_at` | FIXED BY PLAN ITEM C6 | 1 | `test_schema.py::test_stub_and_part3_tables_have_the_spec_columns` |
| notification | `notification_id, event_id, notification_type, created_at`; one row per event; FK to event | `requested_at`; no FK; no uniqueness | FIXED BY PLAN ITEM C6 | 1 | `test_schema.py::test_notification_references_the_event_once` |
| notification_delivery | PK `(notification_id, webhook_id)`; status PENDING, DELIVERED, FAILED; `attempt_count, next_attempt_at, lease_until`; index `(status, next_attempt_at)`; `delivered_at`, `last_status` text | IN_FLIGHT status; `attempts`, `lease_expires_at`; extra `latency_seconds`; `last_status` integer | FIXED BY PLAN ITEM C5; NEW FINDING N7 (`last_status` type) | 1 | `test_schema.py::test_stub_and_part3_tables_have_the_spec_columns` |
| sweeper_heartbeat | `last_ran_at`, single row, real time | Missing | FIXED BY PLAN ITEM C7 | 1 | `test_gaps.py::test_sweep_writes_the_wall_clock_heartbeat` |
| monitoring_gap | Not in the spec | Extra table | FIXED BY PLAN ITEM C4 | 1 | `test_schema.py::test_stub_and_part3_tables_have_the_spec_columns` |

## 13. Observability (spec 3.8)

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| Sweep log | Each sweep: systems examined, gaps opened | Logged only when a gap opened, without the count examined | NEW FINDING N8 | 2 | `test_gaps.py::test_sweep_logs_systems_examined_and_gaps_opened` |
| Attempt log | Each attempt: notification id, webhook id, status, duration | Only failures of the HTTP client are logged | NEW FINDING N8 | 2 | `test_notifications.py::test_delivery_attempt_is_logged` |
| Late log | Each late delivery | Missing | NEW FINDING N5 | 2 | `test_notifications.py::test_late_delivery_is_logged_and_counted` |
| Health values | Seconds since the last sweep, pending deliveries, oldest pending age, for Part 4's health endpoint | Missing | NEW FINDING N9 | 2 | `test_notifications.py::test_health_values` |

## 14. Part 3 rows of spec 4.4 and 4.5

| Clause | Spec requires | Before | Status | Stage | Proof |
|---|---|---|---|---|---|
| 4.4 Order | 0001 Platform, 0002 Ledger, 0003 Detection (Part 1), 0004 Notifications (Part 3) | Part 3 is 0003 | FIXED BY PLAN ITEM C1 | 1 | `tests/api/test_ledger_system.py` (head 0004) |
| 4.4 0001 contents | `clock_state, api_key, monitored_system, threshold_version, sweeper_heartbeat`, roles | 0001 stub has `monitored_system` (non-spec columns) and Part 1's `event` | FIXED BY PLAN ITEMS C2, C3, C7 | 1 | `test_schema.py::test_stub_and_part3_tables_have_the_spec_columns` |
| 4.4 monitored_system | `system_id, sampling_interval_s, gap_timeout_intervals, last_received_at` (set to seed time) | `monitoring_started_at`; nullable `last_received_at` | FIXED BY PLAN ITEM C3 | 1 | same |
| 4.4 Roles | Application role: normal read and write on every table except the ledger | SELECT, INSERT, UPDATE only (no DELETE) on stub and Part 3 tables | NEW FINDING N10 | 1 | `test_schema.py::test_application_role_has_normal_read_write_outside_the_ledger` |
| 4.4 Deployed migrations | Never edit a deployed migration | Nothing in this repository is deployed (deployment is Part 4's) | MATCHES | n/a | Report, UNCERTAINTIES |
| 4.5 Clock | `now()` timezone-aware UTC; no direct system-time use outside the clock module | Part 2 stub; `wall_now()` needed for the heartbeat | FIXED BY PLAN ITEM C7 | 2 | `test_clock_rule.py::test_no_direct_system_time_calls_outside_the_clock_module` |
| 4.6 Clock advance | Calls `gaps.sweep` and `notifications.deliver_due` once | Part 4 | OUT OF SCOPE | n/a | n/a |

## 15. Spec 3.9 test table (current coverage)

| Row | Before | Stage |
|---|---|---|
| AC-02 three missed intervals | `test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do`, system test | 4 (update) |
| AC-02 boundary 600 s | same | 4 |
| Exact threshold 899/900 | same | 4 |
| One event per outage (+1,000 s) | Close (`test_no_second_gap_while_one_is_open` uses +3,600 s steps) | 4 (add the exact row) |
| Recovery | `test_the_next_sample_closes_the_gap`, `test_after_closing_a_new_silence_opens_a_new_gap` | 4 (update for A7) |
| Duplicate resets silence | Needs Part 1's ingest | Deferred |
| Wrong sender clock | Needs Part 1's ingest | Deferred |
| AC-04 bound honored | Bound tests; no slow-receiver case | 4 (add) |
| Retries 500, 500, 200 | Missing | 4 (add) |
| Permanent failure (six) | Five-attempt version | 4 (update) |
| No webhook registered | Missing (behaviour was the opposite) | 4 (add) |
| Registration | Present; codes change | 4 (update) |
| Atomicity | Missing | 4 (add) |
| Worker resilience | Lease-expiry test present | 4 (rename and keep) |

## 16. New findings, summarised

| Id | Finding | Fix |
|---|---|---|
| N1 | Order of checks (401, 403, 400, ping) is untested with a bad body | Test only |
| N2 | `LOG_LEVEL` is ignored by the worker | Read it in `app/worker.py` |
| N3 | A non-database error for one system or one delivery aborts the rest of that pass | Catch every exception per system and per delivery, log by type name |
| N4 | Reset tolerance is untested | Test; handle a vanished delivery row |
| N5 | Late deliveries are neither logged as warnings nor counted | Warning plus a process counter |
| N6 | Webhook URL capped at 2048 characters; `event_types` capped at 10 entries; neither is in the spec | Remove both caps |
| N7 | `notification_delivery.last_status` is integer; spec says text | Text column (HTTP status as text); payload keeps the integer |
| N8 | Sweep and attempt log lines lack the fields spec 3.8 names | Add the log lines |
| N9 | No way for Part 4's health endpoint to read the three worker values | One read-only function |
| N10 | No DELETE for the application role on non-ledger tables | Grant SELECT, INSERT, UPDATE, DELETE, as spec 4.4 says |
