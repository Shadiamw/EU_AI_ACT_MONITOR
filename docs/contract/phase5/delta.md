# Phase 5 contract delta (PROPOSED)

**Status: proposed, not agreed.** Phase 5 spec 9.2: every row needs both teams' agreement, and
a Change row also needs the instructor's sign-off; the team obtains these. When the delta is
agreed, Part 4 exports the whole agreed contract as `docs/contract/pod_contract_phase5.md`
(spec 9.4). Until then this file is the only repository copy, and that export does not exist.

"Change" alters text already in the shared contract. "Additive" is a new endpoint, or a new
optional field or error that existing clients do not see unless they call it. Every row is
built on `phase5-integration`; `tests/acceptance/test_phase5_contract.py` checks that each
new route answers 401 without a key (not 404 or 405).

| # | Kind | Item | Proposed text | As built |
| --- | --- | --- | --- | --- |
| Δ1 | Change | Define the Report object | The shape of [`report.schema.json`](report.schema.json) (Draft 2020-12), including `report_kind` (`SERIOUS_INCIDENT` or `NOT_SERIOUS_CLOSURE`, D2) and `schema_version` `pod-h-report-1` | `app/reports.py`; `test_ac13_report_matches_the_schema` |
| Δ2 | Change | `GET /v1/events/{event_id}/report` | Auth: reviewer role required. Query `format=json\|pdf`. Success: 200 Report object (JSON), or the PDF of the same report (`application/pdf`); the bytes are those stored at assembly, with headers `ETag`, `X-Report-Id`, `X-Report-Version`, `X-Report-Sha256`. Errors: 400 MALFORMED_QUERY, 401, 403 non-reviewer, 404 unknown event, 409 NO_DISPOSITION, 410 RECORD_ERASED. Side effect: the first request after a disposition or revision assembles and stores a new report version. | `app/api/report_routes.py` |
| Δ3 | Additive | `POST /v1/events/{event_id}/report/submission` | Reviewer only. Body `{report_id, json_sha256, report_type, expected_next_report_date?, authority_reference?}`; `report_type` one of "Initial", "Follow up", "Combined initial and final", "Final (Reportable incident)". 201 `{submission_id, report_id, event_id, submitted_by, submitted_at, submission_kind (INITIAL\|FOLLOW_UP), report_type, expected_next_report_date, authority_reference}`. Records a manual submission; sends nothing. Errors: 400 MALFORMED_BODY, 401, 403, 404, 409 NOT_SUBMITTABLE, REPORT_NOT_CURRENT, REPORT_HASH_MISMATCH, REPORT_ALREADY_SUBMITTED, 410 RECORD_ERASED | `app/reports.py` (`submit`) |
| Δ4 | Additive | `GET /v1/events/{event_id}/reports`, `GET /v1/reports/{report_id}` | The first: reviewer or viewer, a bare array of report versions with `status` ASSEMBLED, SUBMITTED, SUPERSEDED or SUBMITTED_SUPERSEDED. The second: reviewer only, one stored report version (410 after erasure) | `app/api/report_routes.py` |
| Δ5 | Additive | `GET /v1/ledger/verifications`, `POST /v1/ledger/verifications` | GET (reviewer or viewer): the verification runs, newest first, as a bare array. POST (reviewer): run one now, 201 with the run (`trigger` MANUAL) | `app/api/ops_routes.py` |
| Δ6 | Additive | `POST /v1/events/{event_id}/erasure` | Reviewer only, empty body or `{}`. 200 `{event_id, erased_at, erased_fields}`; 400 MALFORMED_BODY; 404; 409 RETENTION_NOT_EXPIRED before `retain_until`; 409 ALREADY_ERASED | `app/api/erasure_routes.py` |
| Δ7 | Additive | Error codes | `RECORD_ERASED` (410), `NOT_SUBMITTABLE`, `REPORT_NOT_CURRENT`, `REPORT_HASH_MISMATCH`, `REPORT_ALREADY_SUBMITTED`, `RETENTION_NOT_EXPIRED`, `ALREADY_ERASED` (409), `MALFORMED_QUERY` (400); bodies `{error_code, message}` as every existing error | |
| Δ8 | Additive | `WEBHOOK_REGISTERED` payload | `url` becomes `url_sha256` (lowercase hex SHA-256 of the UTF-8 URL), observable in the export (D16) | `test_webhook_registered_carries_url_sha256_not_url` |
| Δ9 | Change | `POST /v1/test/clock/advance` errors | "400 if seconds is negative, not an integer, or would move the simulated time to or past 9999-07-01T00:00:00Z" | `test_clock_advance_into_the_last_half_of_9999_is_400` |
| Δ10 | Additive (internal) | `GET /v1/ops/dashboard` | Reviewer or viewer; the operations dashboard (spec 7.2). Proposed **not** to enter the shared contract | `app/ops.py` |
| Δ11 | Additive | `tools/verify_ledger.py --anchor` | Compares the export with a stored chain head; output unchanged without the option | `test_verifier_output_without_anchor_is_unchanged` |

## As-built additions (not in spec 9.2), proposed

**Status: proposed, not agreed, like every row above.** These are behaviours the built service
shows to a client that spec 9.2 does not list (audit F-03, section 8; added in the Part 4 audit
fix round). Each needs the same agreement as a 9.2 row before it enters the shared contract.

| # | Kind | Item | Proposed text | As built |
| --- | --- | --- | --- | --- |
| A1 | Additive | `GET /healthz` fields | The body adds `version` (`APP_VERSION`, null when unset), `last_verification` (`{finished_at, valid, head_check}` of the latest verification run of any trigger, or null), `last_verification_age_seconds` (whole wall-clock seconds, or null) and `reset_count` (0 before the first reset). The 503 body for an unreachable database carries the same keys: `version` from the environment and the other three null (decision 306). Ledger integrity never makes `/healthz` 503 (spec 6.5) | `app/api/health_routes.py`; `test_healthz_reports_the_app_version`, `test_healthz_is_503_with_the_body_when_the_database_fails` |
| A2 | Change | `POST /v1/metrics` errors | 400 `MALFORMED_BODY` for a sample whose `timestamp` is at or after `9999-07-01T00:00:00Z` (spec 5.4, D10; Δ9 covers only the clock advance) | `app/ingestion.py`; `test_a_sample_dated_after_9999_07_01_is_400` |
| A3 | Change | `DISPOSITION_RECORDED` and `DISPOSITION_REVISED` payloads | `rationale` is the sealed string (`pd1:` followed by the sealed value), not the text, in `GET /v1/ledger/export` and the audit view; a null rationale stays null (decision 279) | `app/dispositions.py`; `tests/records/test_personal_data.py` |
| A4 | Additive | Ledger entry types | `REPORT_ASSEMBLED`, `REPORT_SUBMITTED` and `RECORD_ERASED` are new `entry_type` values in `GET /v1/ledger/export` and `GET /v1/events/{event_id}/audit` (only `RECORD_ERASED` the error code is in Δ7) | `app/ledger/canonical.py`; `test_all_seventeen_entry_types_are_accepted` |
| A5 | Change | `GET /v1/events/{event_id}` after erasure | Still 200; every `harm_fields.*.evidence` and `disposition.rationale` is null; every other field is unchanged (spec 4.4) | `test_ac18_personal_data_is_unreadable_after_erasure` |
| A6 | Change | `POST /v1/test/reset` | Also empties the Phase 5 tables (`report`, `report_submission`, `record_key`, `ledger_verification_run`, `platform_counter`, keeping `reset_count` + 1). Can return 500 when a detected deadlock or its 2 s lock timeout aborts the truncate three times in a row; each attempt rolls back whole (decision 300). The contract lists only 204 | `app/platform.py`; `test_reset_retries_after_a_detected_deadlock` |
| A7 | Additive | `POST /v1/events/{event_id}/disposition` errors | 409 `ALREADY_ERASED` ("the personal data of this event has been erased") for any disposition on an erased event, after the 400 and 404 checks and before `EVENT_NOT_REVIEWABLE` (decision 287, which assigns it to this document) | `app/dispositions.py`; `test_revision_of_an_erased_event_is_refused_with_and_without_a_rationale` |

Operational note, proposed to stay internal: `GET /v1/ops/dashboard` and `GET /healthz` can
return 500 or 503 while a reset runs concurrently (decision 300's residual). The only claim made
is that no invisible lock cycle is left (`test_reset_lock_timeout_breaks_an_invisible_cycle`,
`test_dashboard_does_not_hold_locks_while_it_waits_for_another_connection`), not a measured
failure rate.

## Open inputs (spec 9.3; flagged, not decided here)

- **Opaque versus UUID ids:** the contract shows `event_id — string (uuid)`, but its own
  examples use `evt-7f3a` and `sys-42`; the service issues `evt-<32 hex>`, and the new ids
  follow it (`rpt-`, `sub-`, `lvr-`). The pod's 2026-11-04 plan should settle this before Δ1.
- **Response shapes** for clock advance, verify and the event list are open (Phase 4 Q6.2).
  The new list endpoints use bare arrays, like `GET …/audit`.
- **Viewer writes:** viewers may still call `POST /v1/webhooks` and the test endpoints. The
  new write endpoints (submission, erasure, manual verification) are reviewer-only.
