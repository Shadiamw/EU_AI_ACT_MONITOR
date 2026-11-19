# Phase 5 models: what changed (spec section 8)

Written at integration from the merged code and tests (`phase5-integration`, Parts 1 to 3
merged; Phase 5 spec 8). It replaces nothing in [`phase4_models.md`](phase4_models.md): it
adds the Phase 5 rows and diagrams. Every diagram below is drawn from the code (the routes,
migrations 0008 to 0010 and `app/reports.py`), and every test named here exists on this branch.
The design-requirement mapping used for the tester's `coverage.model` is
[`phase5_design_requirements.md`](phase5_design_requirements.md).

## 8.1 Context diagram

```mermaid
flowchart LR
    subgraph boundary["EU AI Act Monitor (Fly app eu-ai-act-monitor-api)"]
        api["API (FastAPI)"]
        worker["Worker: gap sweep, reminders, delivery, nightly verify, alerts"]
        db[("PostgreSQL 16: ledger, events, reports, record keys")]
        api --- db
        worker --- db
    end
    ai["Monitored AI system (sys-42)"] -- "POST /v1/metrics (system key)" --> api
    reviewer["Reviewer (rev-1, rev-2)"] -- "reviews, dispositions; downloads report JSON/PDF; records submission" --> api
    viewer["Viewer (viewer-1)"] -- "reads events, audit, export" --> api
    operator["Operator (configuration only; reviewer key)"] -- "reads dashboard and alerts; triggers erasure" --> api
    worker -- "flag and reminder webhooks" --> receiver["Webhook receivers"]
    gh["Chain-head store (GitHub chain-heads branch, scheduled workflow)"]
    api -- "daily export + head (viewer key)" --> gh
    reviewer -. "submits report manually (outside system boundary)" .-> authority["Market surveillance authority"]
```

Unchanged from Phase 4: no identity provider, webhooks only, no Evidently. The Phase 4
"report submission not built yet" marking is gone: the reviewer downloads the report and
records the manual submission in the system (Part 1, spec 3.7).

## 8.2 Model 1 (state machine)

The event states are still the four contract states (D1). Report records have their own
statuses, as `GET /v1/events/{id}/reports` lists them (`app/reports.py`, `listing`).

```mermaid
stateDiagram-v2
    direction LR
    state "Event (unchanged, four contract states)" as Event {
        [*] --> FLAGGED
        FLAGGED --> UNDER_REVIEW: reviewer first view
        UNDER_REVIEW --> DISPOSITIONED: serious tier
        UNDER_REVIEW --> CLOSED: NOT_SERIOUS + rationale
        FLAGGED --> DISPOSITIONED: serious tier
        FLAGGED --> CLOSED: NOT_SERIOUS + rationale
        DISPOSITIONED --> DISPOSITIONED: revision
        DISPOSITIONED --> CLOSED: revision to NOT_SERIOUS
        CLOSED --> DISPOSITIONED: reopen
    }
    state "Report record (one row per disposition row, on first GET)" as Report {
        [*] --> ASSEMBLED: reviewer GET report (REPORT_ASSEMBLED)
        ASSEMBLED --> SUBMITTED: POST submission, current, hash matches (REPORT_SUBMITTED)
        ASSEMBLED --> SUPERSEDED: revision (DISPOSITION_REVISED)
        SUBMITTED --> SUBMITTED_SUPERSEDED: revision
    }
```

| From | Trigger and guard | To | Ledger | Test |
| --- | --- | --- | --- | --- |
| DISPOSITIONED (serious tier) | Reviewer GET report, no report for the latest disposition row | report ASSEMBLED | REPORT_ASSEMBLED | `test_report_after_a_disposition_is_200_not_501` |
| CLOSED (NOT_SERIOUS) | Reviewer GET report | closure report ASSEMBLED, marked NOT FOR SUBMISSION (D2) | REPORT_ASSEMBLED | `test_closure_report_for_not_serious_has_no_deadline` |
| ASSEMBLED | POST submission, current report, matching hash | SUBMITTED | REPORT_SUBMITTED | `test_submission_records_reviewer_time_and_hash` |
| ASSEMBLED or SUBMITTED | Disposition revision | SUPERSEDED or SUBMITTED_SUPERSEDED (derived); next GET assembles version n+1 | DISPOSITION_REVISED | `test_a_revision_creates_a_new_report_version`, `test_report_status_is_ready_then_submitted` |
| any, after `retain_until` | POST erasure | ERASED marker (not a state): report routes 410 | RECORD_ERASED | `test_ac18_personal_data_is_unreadable_after_erasure` |
| FLAGGED or UNDER_REVIEW | GET report | unchanged (409 NO_DISPOSITION) | none | `test_ac08_a_report_without_a_disposition_is_409_and_changes_nothing` |

REJECTED (a response, not a state) gains: submission of a non-current report (409
`REPORT_NOT_CURRENT`), of an already-submitted one (409 `REPORT_ALREADY_SUBMITTED`), of a
closure report (409 `NOT_SUBMITTABLE`) or with a wrong hash (409 `REPORT_HASH_MISMATCH`).
Model 1's Phase 2 names map as before: REPORT_READY = ASSEMBLED.

## 8.3 Model 2 (data schema)

```mermaid
erDiagram
    event ||--o{ report : "event_id"
    disposition ||--o{ report : "disposition_id"
    report ||--o| report_submission : "report_id (at most one)"
    event ||--o| record_key : "subject_id = event_id"
    event {
        text event_id PK
        timestamptz retain_until
        timestamptz erased_at "new (0009)"
    }
    report {
        text report_id PK
        text event_id FK
        int report_version
        text disposition_id FK
        text report_kind
        text schema_version
        bytea json_bytes "sealed"
        bytea pdf_bytes "sealed"
        text json_sha256
        text pdf_sha256
        timestamptz assembled_at
        text assembled_by
    }
    report_submission {
        text submission_id PK
        text report_id FK
        text event_id
        text submitted_by
        timestamptz submitted_at
        text submission_kind
        text report_type
        date expected_next_report_date
        text authority_reference
    }
    record_key {
        text subject_id PK
        bytea data_key "NULL once destroyed"
        timestamptz created_at
        timestamptz destroyed_at
    }
    ledger_verification_run {
        text run_id PK
        text trigger "NIGHTLY or MANUAL"
        timestamptz started_at
        timestamptz finished_at
        bool valid
        int entries_checked
        text first_mismatch
        bigint head_seq
        text head_hash
        text head_check
        jsonb projection_mismatches
        jsonb report_mismatches
    }
    platform_counter {
        bool singleton PK
        bigint reset_count
    }
```

- `report`, `report_submission` and `ledger_verification_run` are insert-only (grants INSERT
  and SELECT, plus a trigger rejecting UPDATE and DELETE); `record_key` is never deleted;
  `platform_counter` is written only by reset (`test_phase5_tables_and_grants`,
  `test_report_tables_are_insert_only`).
- The three report timestamps live inside the stored JSON; only `assembled_at` is a column.
- `ledger_entry.payload`: "jsonb; personal values sealed per record key from 0009 (P2); webhook
  URL as url_sha256 (D16)" (`test_webhook_registered_carries_url_sha256_not_url`).

## 8.4 Model 4 (invariants)

| INV | Phase 4 | Phase 5 (as merged) | Tests |
| --- | --- | --- | --- |
| INV-1 | Partly | **Holds:** a report exists only for an event with a disposition row, and every disposition row has four decided fields | `test_report_after_a_disposition_is_200_not_501`, `test_ac08_a_report_without_a_disposition_is_409_and_changes_nothing`, `test_ac13_report_matches_the_schema` |
| INV-4 | Holds | Still holds; `report`, `report_submission`, `ledger_verification_run` join the insert-only tables | `test_report_tables_are_insert_only` |
| INV-7 | Partly | **Holds:** chain links, plus the nightly run passes | `test_nightly_verify_records_a_valid_run`, `test_model5_ex5_nightly_verify_finds_a_payload_edit` |
| INV-8 | Differs | Still Differs: reading a stored report appends nothing; assembly and submission append (D19) | `test_reading_a_stored_report_appends_nothing` |
| INV-9 | Partly | **Holds:** report generation and submission are REVIEWER-only | `test_ac14_the_reviewer_gets_the_report_and_others_are_denied`, `test_submission_roles` |
| INV-10 | Partly | **Holds** in Phase 5's meaning: nothing is removed (no purge, D9) and erasure is refused before `retain_until` | `test_nothing_is_erased_before_retain_until`, `test_erasure_before_retain_until_is_409_and_changes_nothing` |
| INV-11 | Partly | **Holds:** destroying a record key changes no ledger hash, and verify passes | `test_ac18_chain_positions_and_hashes_are_unchanged_and_verify` |
| INV-14 (new) | — | Stored report bytes never change: SHA-256 of the opened bytes = the `report` hashes = the `REPORT_ASSEMBLED` payload | `test_report_bytes_are_served_exactly_as_stored`, `test_tampered_report_bytes_are_found_by_the_nightly_check` |
| INV-15 (new) | — | At most one submission per report; only the current serious report is submittable | `test_a_report_is_submitted_at_most_once`, `test_only_the_current_report_can_be_submitted` |

INV-2, 3, 5, 6, 12 and 13 are unchanged, and their Phase 4 tests carry over.

## 8.5 Model 3 (timeline)

Unchanged. Its note gains: "a submitted current report clears alerts A6 and A7, and does not
stop reminders" (Model 5 Ex-1 shows the reminders continuing after the submission).

## 8.6 Model 5 (worked examples)

[`tests/fixtures/phase5/model5_examples.json`](../../tests/fixtures/phase5/model5_examples.json)
(its README gives the source of every value) is the Phase 4 file with:

- **Ex-1:** at d3 +1 h, the report (200; detected d0, determined d0 + 4 h, last revised d3,
  assembled at the request; 10-day window; `REPORT_ASSEMBLED` carries the served hash), then a
  submission (201 `INITIAL`). The reminders that follow are unchanged.
- **Ex-2:** the report is 200 `NOT_SERIOUS_CLOSURE` (no deadline, the rationale, NOT FOR
  SUBMISSION) instead of 501; its submission is 409 `NOT_SUBMITTABLE`.
- **Ex-4:** unchanged.
- **Ex-5:** in-process only, `test_model5_ex5_nightly_verify_finds_a_payload_edit` (the
  runner lists and skips it).
- **Ex-3:** still covered by the AC-02 tests.

`tools/run_model5.py` runs Ex-1, Ex-2 and Ex-4 over HTTP in the CI image job.
