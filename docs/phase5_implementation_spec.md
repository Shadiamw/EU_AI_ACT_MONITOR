# Phase 5 Implementation Specification

Compliance monitoring system: submission-ready reports, submission records, ledger hardening,
retention and erasure, and the health dashboard, built as four parallel parts.

Pod H · 2 November 2026 · builds on `main` at `e53d7c7` (the merge of PR #6, which follows
PR #5's Phase 4 integration merge `9b2e587`) · **status: approved by SS on 2 November 2026** (section 12). Nothing in this
document has been implemented, and no code, test, migration or workflow was changed while
writing it.

---

## How to use this document

This specification splits Phase 5 into four parts, one per team member, in the same shape as
the Phase 4 specification (`Phase4_Implementation_Spec.docx`, 20 October 2026). Each part
section stands on its own. It restates the shared rules it depends on, the interfaces it calls
or provides, the data it owns, the behaviour it must produce and the tests that prove it.

- **Precedence (for this phase, as set by the team):**
  1. The Phase 5 description (Project Requirements.docx, section 6.3, quoted in section 0.1) sets
     the **scope**.
  2. `Contracts Schemas.docx` (the pod contract) wins on every endpoint, request, response and
     error shape.
  3. `Project Requirements.docx` wins on the wording of every REQ and AC. This document quotes
     that wording and never rewords it.
  4. Below these: the Part 2 (ledger) addendum (its thirteen rules still hold), the Phase 4
     Addendum, the Phase 4 Specification, and then this document. Where this document changes
     a Phase 4 rule, Appendix D lists the change. Everything this document does not mention keeps
     its Phase 4 behaviour.
  5. Models 1 to 5 are design context below all of the above.
- **Where artifacts disagree**, this document does not resolve it silently. Each case is a row
  in the Conflicts table (section 13) with a *proposed* resolution, or SS's decision where one
  was given. Those that change behaviour are also decision records (section 12).
- **MUST / SHOULD / MAY** have their RFC 2119 meanings. **Pinned** means the item must be done
  exactly as written, because another part or the other team can observe it. **Open** means the
  implementer chooses the simplest well-maintained open-source option and records the choice as
  a decision in `docs/decisions.md`.
- **UNVERIFIED** marks anything that could not be checked against a source. Every such item is
  also listed in section 14 (Open questions) or section 15 (Assumptions to confirm). None is
  stated as fact.
- **Decisions.** Section 12 has 20 decision records, D1 to D20, all settled by SS on 2 November
  2026. Six (D1, D2, D11, D12, D17 and D18) were asked in the project thread and are
  **decided**; the other fourteen (D3 to D10, D13 to D16, D19, D20) were this document's
  proposals and are **approved as proposed**. Conditions SS added (D2, D7, D12, D15) are written
  into the records. Section 1.4 sets the priority and cut line.

| Part | Owner role | Builds | Acceptance criteria |
| --- | --- | --- | --- |
| **Part 1** | Report owner | Report assembly (JSON and PDF), byte-exact storage and hashing, report versions after revisions, the submission record, the replacement of the Phase 4 report guard | AC-13 (schema check), AC-14 (report half), AC-08 (regression) |
| **Part 2** | Records owner | Per-record keys, personal-data sealing and erasure, the retention rule and its test, the year-9999 clock bound, the webhook-URL commitment | AC-05, AC-18 |
| **Part 3** | Ledger-operations owner | The nightly verification job, the external chain-head anchor, the event-table projection check, `/healthz` version and health fields, the dashboard endpoint, alerts and the operations note | AC-06 (hardening; regression), Model 5 Ex-5 |
| **Part 4** | Platform owner | The base commit, the second reviewer seed, the contract-delta document, the console report, submission and dashboard views, the AC-16 scripted trace, the HTTP runner and the `PHASE5_REQUIRED` gate, Model 5 fixtures, the models update, manual checklists and deployment | AC-16, and every earlier AC in CI and against the deployed service |

---

## 0. Inputs, sources and what could not be read

### 0.1 The Phase 5 description (verbatim, authoritative for scope)

From `Project Requirements.docx`, section 6.3, which matches the text the team supplied:

> "Deliverables will include a system that can generate submission-ready reports as JSON and
> PDF, hashed and stored exactly as produced, and allow reviewers to mark a report as
> submitted. The ledger will also be verified nightly, and its chain head will be stored
> outside the database. The system will retain records for at least six months, and a system
> health dashboard will be accessible. This covers REQ-12, the retention half of REQ-05, the
> hardening of REQ-06, and REQ-16. We will update the contract so that it gains the report,
> submission, and ledger-verification endpoints, and also update our system to now cover all 5
> models. We will add an operations note covering alerts and the dashboard and update the final
> documentation. The uncertainty to reduce includes whether the generated report can actually
> be usable as a real Article 73 submission, whether personal data can be made unreadable
> without invalidating the hash chain, and whether the whole chain holds together end to end.
> Demonstration of completion: The other team runs the complete AC-16 trace on the deployed
> service: breach, flag, review, disposition, deadline, report, and submission. They also
> trace the not-serious path and download the report and review it against the Article 73
> fields. We will add new tests for AC-05 (with a mocked clock), AC-13 (the schema check),
> AC-16 (the scripted end-to-end run), and AC-18. All earlier regression tests will also carry
> over. Manual checks are added: AC-02's real delivery, AC-09's screen review, and AC-13's
> usability review."

The team's brief adds "(see model1-model5 labeled pngs in artifacts, although these may need to
be changed given the state of the system based on decisions made in development)" after "all 5
models". Section 6.4 of the same document lists the agreed fallbacks if the pod falls behind:
"removing REQ-16, reducing PDF rendering for reports, and possibly removing the health
dashboard, instead focusing on just alerts."

### 0.2 Requirement and acceptance-criterion text relied on (verbatim, Project Requirements.docx)

- **REQ-05:** "The system shall retain every flagged event and its associated raw metric data
  for a minimum period of six months"
- **REQ-06:** "The system shall write event and severity-decision records in an append-only
  manner such that any alteration to a record after creation is detectable"
- **REQ-12:** "When severity is confirmed for an event, the system shall assemble a
  submission-ready report containing the event data, the four Article 3(49) severity fields
  defined in REQ-07, the reviewer's determination, and the detection timestamp, determination
  timestamp, and report-assembly completion timestamp"
- **REQ-13:** "The system shall restrict severity-determination and report-generation actions
  to users holding an authorized-reviewer role"
- **REQ-14:** "When any user views or changes an event record, the system shall log that user's
  identity and the timestamp of the action"
- **REQ-16:** "The system shall support rendering a record's personal-data fields permanently
  unreadable after its retention period, without altering the record's position in the
  tamper-evident chain"
- **AC-02 (REQ-02):** Setup: "Same harness, suppress metric emission". Action: "Withhold for
  exactly 3 intervals (15 min)". Expected: "Gap event generated, and reviewer notified within
  the configured 15-min delay of the 3rd missed interval". Boundary: "Withholding 2 intervals
  must NOT trigger". Method: "Automated (event log) + manual check of notification delivery"
- **AC-05 (REQ-05):** Setup: "Create event + metric data; mock system clock". Action: "Query
  record at 5mo29d and at 6mo1d". Expected: "Retrievable at 5mo29d; absence at 6mo1d is not a
  failure (minimum, not maximum)". Method: "Automated with mocked clock — cannot be tested via
  real wall-clock time in-semester, so this is a stated testing limitation"
- **AC-06 (REQ-06):** Setup: "Create event record, capture hash/signature". Action: "Attempt
  direct modification bypassing the application layer". Expected: "Modification is detectable
  via hash-chain mismatch on next verification pass". Method: "Automated (hash check) + manual
  review of storage-layer write permissions"
- **AC-07 (REQ-07):** Method: "Automated (schema/field check) + manual review of the
  confirmation record"
- **AC-09 (REQ-08b):** Setup: "Reviewer disposition interface". Method: "Manual inspection
  (design/config review) + automated check of the rationale-required path"
- **AC-13 (REQ-12):** Setup: "Event with severity confirmed". Action: "Retrieve the assembled
  submission-ready report". Expected: "Contains event data, all 4 Art. 3(49) fields, reviewer
  determination, and detection timestamp, determination timestamp, and report-assembly
  completion timestamp — all non-null and distinct where applicable". Method: "Automated
  (schema check) + manual review of whether the report would be usable as an actual regulatory
  submission (judgment call)"
- **AC-14 (REQ-13):** Setup: "One reviewer-role account, one non-reviewer account". Action:
  "Both attempt severity assignment and report generation". Expected: "Reviewer succeeds;
  non-reviewer denied on both actions". Method: "Automated (access-control test)"
- **AC-16 (REQ-03, REQ-04, REQ-07, REQ-08a, REQ-08b, REQ-09, REQ-10, REQ-12):** Setup: "Test
  model configured to breach its metric threshold". Action: "Run the full chain end-to-end:
  breach → flag → notify → parameterized report → disposition assignment → deadline
  computation → submission-ready report". Expected: "One event ID traceable through every
  stage with consistent data; no stage is skippable (e.g., report generation fails without a
  preceding disposition); the "not a serious incident" path is also traced end-to-end and
  confirmed to terminate without a deadline". Method: "Manual/analysis trace review — a
  coherence judgment across stages rather than a single automated assertion"
- **AC-18 (REQ-16):** Setup: "Event record past its retention period containing personal-data
  fields". Action: "Trigger the deletion/key-destruction process for that record". Expected:
  "Personal-data fields become unreadable; the record's position and hash in the
  tamper-evident chain remain unchanged and still verify". Method: "Automated (readability
  check pre/post) + manual review of chain integrity post-deletion"

### 0.3 Contract text relied on (verbatim, Contracts Schemas.docx)

- `GET /v1/events/{event_id}/report`: "assembled submission-ready report Purpose / REQs: REQ-08a,
  REQ-12 Success: 200, Report object Errors: 409 if no disposition recorded yet (enforces
  REQ-08a), 404 unknown event"
- `GET /v1/ledger/export` and `GET /v1/ledger/verify`: "export: 200, raw ledger entries + hashes
  for the other team to reverify offline"; "verify: 200, { valid: bool, entries_checked,
  first_mismatch: null|<entry_id> } — runs the same check server-side"
- `POST /v1/test/clock/advance`: "makes REQ-05, REQ-10, REQ-11 testable without real wall-clock
  waits Request: { seconds: <int> } Success: 200, { simulated_time }"
- EVENT OBJECT: `state — "FLAGGED" | "UNDER_REVIEW" | "DISPOSITIONED" | "CLOSED"`; `disposition
  — object or null: { tier, rationale, reviewer_id, assigned_at }`; `revisions — array of {
  prior_tier, new_tier, reviewer_id, revised_at }`; HARM FIELD OBJECT `status`, `evidence`,
  `confirmed_by`, `confirmed_at`.
- Section 5.1 Auth (Project Requirements.docx): "reviewer is required for disposition and report
  actions (REQ-13); viewer can read events/audit/ledger but not write."

**The contract names a "Report object" but defines no fields for it.** Neither
`Contracts Schemas.docx` nor section 5.1/5.2 of `Project Requirements.docx` contains a Report
schema, and the contract has **no submission endpoint**. The report's JSON structure therefore
cannot be "taken from the Report schema in Contracts_Schemas.docx" as the brief asked. Section
3.3 proposes one, and section 9 lists it as a contract change that needs both teams' agreement
(Conflicts C3, C15).

### 0.4 What was read

- `Phase 4 Report.docx` (all of it), `Contracts Schemas.docx` (all), `Project Requirements.docx`
  (all, including the six embedded figures: the context diagram and Models 1 to 5),
  `Phase4_Implementation_Spec.docx` (structure, sections 1 to 13, 2.8 to 2.11 and Appendices A
  to D), `Phase4_Addendum.docx` (rules 1, 7 to 9 and 11 to 12, as far as they bear on Phase 5),
  and `Phase4_Integration_Guide.docx` (authority and process only).
- Repository `main` at `e53d7c7`: `docs/decisions.md` (format and decisions 214 to 229 in full,
  headings of the rest), `docs/models/phase4_models.md`, the README heading list, migrations
  0001 to 0007, `app/` (the ledger, dispositions, events, review, health, worker, platform,
  formats, evidence, ingestion and webhooks modules, read where cited), `tools/check_acceptance.py`,
  `tests/acceptance/` (test names), `tests/fixtures/phase4/` (report entries), `pod_spec.txt`
  and `.github/workflows/ci.yml`.
- The Commission's draft reporting template (v1.0.0, SB-10407) and draft Art. 73 guidance,
  both uploaded by SS on 2 November 2026 and read in full (titles, versions and dates in 3.3.4).
  The guidance quotes Art. 3(49) in its paragraph (5) and restates the Art. 73(2) to (5) time
  limits in (38) and (39).
- Regulation text, secondary sources (see 0.5): artificialintelligenceact.eu pages for
  Art. 73 and Art. 3, the European Commission consultation page for the draft guidance and
  reporting template, and law-firm summaries (Latham & Watkins, 28 Oct 2025; Taylor Wessing,
  1 Oct 2025; A&O Shearman, 8 Oct 2025; DLA Piper, 15 Oct 2025).

### 0.5 What could not be read, and what we need from the team

| Item | Status | What we need |
| --- | --- | --- |
| **Phase 2 plan as a separate document** (Section 6.2 milestone, Appendix B) | Not in the repository (`git ls-files` has no plan document; `docs/` holds only part audits, decisions and models). Sections 2 (REQs), 4, 5.1, 6.1 to 6.4 and the model figures are in `Project Requirements.docx` and were read there. **Appendix B was not found anywhere.** | The Phase 2 plan with Appendix B, if Appendix B adds anything about reports, retention or REQ-16. The Phase 4 report cites "[2, Appendix B]" for D2 and the reports scope. |
| **Commission draft reporting template and draft guidance** | **Read in full** after SS uploaded both PDFs (3.3.4 records titles, version 1.0.0, reference SB-10407, and dates). Both are drafts of 26 September 2025. Whether a final version was adopted later is **UNVERIFIED** (one web search on 2 November 2026 found none). | Q-13: tell us if a final version exists; the mapping (3.3.4) is then redone against it. |
| **Art. 73 and Art. 3(49), official text** | EUR-Lex returned only its cover page. Art. 73(1) and (2) were read verbatim from artificialintelligenceact.eu. Art. 3(49) is now quoted from the Commission's draft guidance (5), and the Art. 73(2) to (5) time limits from its (38) and (39). Art. 73(6) is from that site's summary only. None of them was checked against the Official Journal, so they stay **UNVERIFIED** (Q-12). | A check against the Official Journal text before the AC-13 usability review. |
| `development-deploy` (`fly.toml`, `GET /livez`) | Not read on purpose (hard rule 3). Facts come from Phase 4 report LIM-02 and decision 229. | Nothing: OD-1 is decided (7.6); deployment config stays there and is still not read. |
| The other team's suite and results | Not available. Phase 4 report Q6.1 says their run against our service is "pending". | Their Phase 5 trace results, at the demonstration. |

---

## 1. Scope

### 1.1 Scope table

Every Phase 5 requirement, acceptance criterion and carried-over item is listed here, each with
its section and the tests that prove it. Test names are new. Before writing this table, each
name was checked against `tests/` with grep and no match was found. Section 10.1 gives each
test's file and expectation.

| Item | Source | Spec section | Owner | Tests (new unless marked) |
| --- | --- | --- | --- | --- |
| REQ-12 report assembly | 6.3, REQ-12 | 3 | Part 1 | `test_ac13_report_matches_the_schema`, `test_ac13_three_timestamps_are_non_null_and_distinct`, `test_report_bytes_are_served_exactly_as_stored`, `test_report_hashes_are_in_the_ledger` |
| JSON and PDF, hashed and stored as produced | 6.3 | 3.4 | Part 1 | `test_pdf_and_json_are_produced_once_and_never_regenerated`, `test_pdf_contains_every_required_field`, `test_tampered_report_bytes_are_found_by_the_nightly_check` (Part 3 runs it) |
| Mark a report as submitted | 6.3 | 3.7 | Part 1 | `test_submission_records_reviewer_time_and_hash`, `test_only_the_current_report_can_be_submitted`, `test_a_report_is_submitted_at_most_once`, `test_submission_after_a_revision_is_a_follow_up`, `test_submission_report_type_is_one_of_four_template_values` |
| AC-13 schema check | AC-13 | 3.3, 10 | Part 1 | `test_ac13_report_matches_the_schema` (+ HTTP `test_ac13_report_over_http_matches_the_schema`), `test_report_carries_the_article73_template_mapping`, `test_closure_report_is_marked_not_for_submission` |
| AC-13 usability review (manual) | 6.3, AC-13 | 10.6 | Part 4 | Manual checklist M3 |
| AC-14 report half ("reviewer succeeds") | AC-14, Phase 4 D1 | 3.6 | Part 1 | `test_ac14_the_reviewer_gets_the_report_and_others_are_denied` (and the changed Phase 4 tests in 10.5) |
| Phase 4 report guard (409 then 501) replaced | Phase 4 report LIM-01, Q3.1 | 3.6 | Part 1 | `test_report_after_a_disposition_is_200_not_501` |
| REQ-05 retention half; AC-05 | 6.3, REQ-05, AC-05 | 5 | Part 2 (merge 2a) | `test_ac05_retrievable_at_5mo29d`, `test_ac05_at_6mo1d_presence_or_absence_is_accepted`, existing regression `test_retain_until_is_the_later_of_six_months_and_183_days` (`tests/worker/test_formats.py:37`, unchanged) |
| REQ-16; AC-18 | 6.3, REQ-16, AC-18 | 4 | Part 2 (merge 2b) | `test_nothing_is_erased_before_retain_until`, `test_report_bytes_are_sealed_at_rest`, `test_ac18_personal_data_is_unreadable_after_erasure`, `test_ac18_chain_positions_and_hashes_are_unchanged_and_verify`, `test_ac18_standalone_verifier_agrees_after_erasure`, `test_erasure_before_retain_until_is_409_and_changes_nothing` |
| REQ-06 hardening: nightly verification | 6.3, INV-7, Model 5 Ex-5 | 6.1 | Part 3 | `test_nightly_verify_records_a_valid_run`, `test_model5_ex5_nightly_verify_finds_a_payload_edit`, `test_nightly_verify_runs_once_per_utc_day` |
| REQ-06 hardening: chain head outside the database | 6.3, LIM-04 | 6.2 | Part 3 | `test_anchor_detects_truncation_below_the_anchor`, `test_anchor_detects_a_forged_anchored_entry`, `test_anchor_accepts_a_longer_valid_chain` |
| Event-table edits not covered by the chain | Phase 4 report Q1.1 (REQ-06 partial) | 6.3 | Part 3 | `test_projection_check_finds_an_edited_event_state`, `test_projection_check_finds_an_edited_deadline`, `test_projection_check_is_clean_after_the_ac16_trace` |
| Health dashboard; operations note | 6.3 | 7 | Part 3 (Part 4 console view) | `test_dashboard_roles`, `test_dashboard_shape`, `test_alert_deadline_passed_without_submission`; `docs/operations.md` |
| Deployed-version traceability in `/healthz` | LIM-02 | 6.5 | Part 3 | `test_healthz_reports_the_app_version` |
| AC-16 scripted trace, both paths | 6.3, AC-16 | 10.3 | Part 4 | `test_ac16_breach_to_submission_trace`, `test_ac16_not_serious_trace_has_no_deadline`, `tools/demo_phase5.py` |
| AC-02 real delivery (manual) | 6.3, AC-02 | 10.6 | Part 4 | Manual checklist M1 |
| AC-09 screen review (manual) | 6.3, AC-09 | 10.6 | Part 4 | Manual checklist M2 |
| AC-07 manual review of the confirmation record | Phase 4 report Q1.1 (REQ-07 partial) | 10.6 | Part 4 | Manual checklist M4 |
| AC-18 manual chain-integrity review | AC-18 | 10.6 | Part 4 | Manual checklist M5 |
| Model 1 REPORT_READY, SUBMITTED | Phase 4 report Q5.1 | 3.2, 8 | Part 1 (Part 4 diagram) | `test_report_status_is_ready_then_submitted` |
| Model 2 `report`, `record_key` | Phase 4 report Q5.1 | 3.5, 4.3, 8 | Parts 1, 2 | `test_phase5_tables_and_grants` (Part 4 base, slots filled by each part) |
| Model 5 Ex-5 (nightly verifier) | Phase 4 report Q5.1 | 6.1 | Part 3 | `test_model5_ex5_nightly_verify_finds_a_payload_edit` |
| INV-1, 7, 9, 10, 11 (Partly in Phase 4) | `docs/models/phase4_models.md` §3 | 8.4 | Parts 1 to 3 | listed in 8.4 |
| LIM-01 to LIM-15 | Phase 4 report Q7.2 | 1.2 | various | listed in 1.2 |
| Contract delta | 6.3 | 9 | Part 4 (document) | `test_contract_delta_routes_exist` |
| All earlier regression tests | 6.3 | 10.5 | Part 4 | 133 HTTP runner cases, the unflagged and flagged pytest runs, Model 5, the Phase 4 demo |

### 1.2 Phase 4 limitations carried over (Phase 4 report Q7.2)

| LIM | Phase 4 text (short) | Phase 5 handling | Section |
| --- | --- | --- | --- |
| LIM-01 | Report route is a guard: 409, then 501 | Replaced by the real report (200) | 3.6 |
| LIM-02 | Deployed commit not readable; `fly.toml` and `/livez` on `development-deploy` | `/healthz` gains `version` (D15). `fly.toml` and `/livez` stay on `development-deploy` (OD-1, decided); the deployer passes the main commit as `GIT_SHA` | 6.5, 7.6, 11.6 |
| LIM-03 | Shared test environment, public keys | Not fixed in code. The operations note adds a booking rule for test windows. The AC-16 trace refuses to start while another trace holds the advisory slot (Part 4, 10.3) | 7.5, 10.3 |
| LIM-04 | Truncation and forged last entry not detected | External chain head (D12) | 6.2 |
| LIM-05 | Reminders only to webhooks; no named compliance officer | Not changed. Stated as a limitation in the operations note | 7.5 |
| LIM-06 | At-least-once delivery, possible reordering | Not changed (intended design) | — |
| LIM-07 | Triggered reminder delivered after a NOT_SERIOUS revision; late routing | Not changed. The dashboard shows reminders delivered for events now CLOSED (informational) | 7.2 |
| LIM-08 | No reminder while machines are stopped | Not changed. Alert A5 (overdue escalations) makes it visible | 7.3 |
| LIM-09 | A reviewer's GET starts the review | Not changed (agreed behaviour) | — |
| LIM-10 | Only one seeded reviewer | Second reviewer `rev-2` (D17, decided) | 11.5 |
| LIM-11 | In-service suite skips sink tests | Not changed in code. Manual check M1 uses the public receiver | 10.6 |
| LIM-12 | Clock advance into the last six months of 9999 accepted; ingest then 500 | Advance bound moved to 9999-07-01 (D10) | 5.4 |
| LIM-13 | Export shows webhook URLs | `WEBHOOK_REGISTERED` carries `url_sha256` instead of `url` (D16) | 4.6 |
| LIM-14 | Fly region iad (not EU) | Out of scope (target phase 6 in the report) | — |
| LIM-15 | Working days use UTC days | Out of scope (target phase 6) | — |

### 1.3 Out of scope

Automatic transmission to an authority (Model 1 says "out of scope: auto-transmission"); email or
named compliance-officer accounts (LIM-05); a purge of retained data (D9); EU hosting (LIM-14);
Evidently integration (Phase 4 report Q7.1); and anything that changes the contract before both
teams and the instructor agree (section 9).

### 1.4 Priority and cut line (SS, 2 November 2026; Pinned)

The fallbacks the pod agreed, quoted in 0.1 from section 6.4: "removing REQ-16, reducing PDF
rendering for reports, and possibly removing the health dashboard, instead focusing on just
alerts." SS set the priority order below. If the pod falls behind, items are dropped **from the
bottom up**: item 5 first, then item 4 (keeping the alerts). Inside item 1, "reducing PDF
rendering" means a plain one-column PDF with the same fields; it is never a reason to drop the
PDF or the JSON. Items 1 to 3 are not planned to be dropped; the table still says what each
would cost.

| Priority | Item | Sections | Lost if dropped | Parts that then no longer depend on it |
| --- | --- | --- | --- | --- |
| 1 | Part 1 (report assembly, storage, hashing, submission) plus the AC-16 trace | 3, 10.3 | REQ-12, AC-13, AC-14 (report half), AC-16 and the demonstration itself; the Phase 4 501 stays | None can drop it: Part 3's report-bytes check (`reports.verify_stored`) and Part 4's trace, console views and gate need it |
| 2 | Nightly verify, the external anchor and the `/healthz` version | 6.1, 6.2, 6.5 | The REQ-06 hardening in the Phase 5 description ("verified nightly", "chain head will be stored outside the database"), Model 5 Ex-5, and the deployed-version check of 11.6 step 5 (LIM-02 stays) | The dashboard's `last_verification` fields and alerts A1 and A2 (7.3) fall back to their stub values; the ops note's sections 2 and 5 shrink |
| 3 | AC-05 (retention test, mocked clock) | 5.3 | The new AC-05 evidence for the retention half of REQ-05. The rule itself is Phase 3 code (`app/formats.py:44-50`) and its regression test stays | Erasure step 5 (`retain_until`) uses the existing rule, not this test; the dashboard's due-records list uses the column; none depend on the test |
| 4 | The dashboard (`GET /v1/ops/dashboard` and the console Health view) | 7.1, 7.2 | The "system health dashboard will be accessible" deliverable. **Alerts stay** (the 6.4 fallback): worker log lines and `/healthz` | Part 4's console Health view; nothing else |
| 5 | Erasure: REQ-16, AC-18, D7, D8, D16 (migration 0009) | 4 | REQ-16 and AC-18; rationale and report bytes stay plain at rest; LIM-13 (URLs in the export) stays; INV-10 still holds because nothing removes or erases anything | Part 1 keeps the identity seal stub (11.3) for good; Part 3 keeps alert A9 and the dashboard's `retention` counts, which then only list due records;  Part 4 drops the AC-18 runner file, M5 and the AC-18 entry of `PHASE5_REQUIRED`; the Phase 3 and 4 tests and payloads listed in 10.2 for Part 2 stay unchanged; `0009_personal_data.py` stays an empty placeholder |

**Erasure merges last (confirmed).** Part 2 is built by one owner but merged twice: **2a**
(section 5 and D10: AC-05 and the clock bound) and **2b** (section 4 with D7, D8 and D16, and
migration 0009). The merge order in 11.4 is **2a, 1, 3, 4, 2b**, so erasure, which changes Phase
3 and 4 payloads and tests, lands last and can be cut without touching anything merged before
it.

---

## Common ground: what all four parts share

### 2. Where Phase 4 left the code

Paths are relative to the repository root, at `main` `e53d7c7`.

| Area | What is there now | Phase 5 consequence |
| --- | --- | --- |
| Report route | `app/api/disposition_routes.py:69-76`: REVIEWER only, `dispositions.report_guard` (404, 409 `NO_DISPOSITION`, `app/dispositions.py:270-283`), then `raise ApiError(501, "NOT_IMPLEMENTED", ...)`. | Part 1 moves the route to `app/api/report_routes.py` and replaces the 501 (3.6). |
| Report data | No `report` table. The migration head is `0007` (`tests/part1/test_schema.py:126`). | Part 1 fills migration 0008. |
| Event row | `event` has `evidence_bundle` (JSONB), `evidence_sha256` and `retain_until` (NOT NULL) (`migrations/versions/0003_detection.py:111-113`). | Retention (Part 2) reads `retain_until`. The report includes the evidence bundle and its hash. |
| Retention rule | `app/formats.py:30-50`: `RETENTION_MONTHS = 6`, `RETENTION_MIN_DAYS = 183`, `retain_until(detected_at)` = the later of +6 calendar months and +183 days. Set at flag time (`app/ingestion.py:230`) and gap open (`app/gaps.py:150`). | Unchanged rule. Part 2 tests it (AC-05) and uses it as the erasure precondition. |
| Ledger hash | `app/ledger/canonical.py:1-8`: canonical JSON of `{seq, entry_type, actor_id, subject_id, payload, recorded_at}`; hash = SHA-256 of `prev_hash + canonical`. The payload is hashed **as stored, in plain JSONB**. | Any later change to a stored payload breaks the chain. Erasure therefore cannot edit ledger payloads (section 4). |
| Entry types | 14 in `ENTRY_TYPES` (`app/ledger/canonical.py:41-58`) and a separate list in `tools/verify_ledger.py:47`, checked at line 156. | The base commit adds three types to **both** lists (11.1). |
| Personal data in the ledger | `DISPOSITION_RECORDED` and `DISPOSITION_REVISED` carry `rationale` as plain text (`app/dispositions.py:195-230`). `WEBHOOK_REGISTERED` carries `url` (`app/webhooks.py:195-201`). Every entry carries `actor_id` (`rev-1`, `viewer-1`, `sys-42`, `system`). Evidence text is not copied into the ledger (Phase 4 spec 2.10). | Section 4.1 inventory. |
| Append-only tables | `ledger_entry` (`migrations/versions/0002_ledger.py:191-212`) and `disposition` (`migrations/versions/0006_dispositions.py:104-123`) have statement-level UPDATE/DELETE-blocking triggers, and the app role has INSERT and SELECT only. | Erasure must not need UPDATE on either (D7). |
| Verify | `app/ledger/verify.py:25-26`: "removing entries from the end of the ledger, or forging the last entry with a consistent hash, is not detectable by the chain alone until Phase 5." | Part 3 (6.2). |
| Health | `GET /healthz` (`app/api/health_routes.py:93-116`), no key, body of exactly four fields. `tests/api/test_health.py:24-28` asserts the exact body. | Part 3 adds fields, and the exact-body tests change in the same commit (10.5). |
| Worker | `app/worker.py:26-50`: each pass runs the sweep (when due), `escalations.fire_due`, then deliveries, sleeping `DELIVERY_POLL_SECONDS` (5 s). | Part 3 adds the nightly step. |
| Clock bound | `app/platform.py:92-103`, decision 226: an advance past 2^63 − 1 or `datetime.max` is 400. A time in the last six months of 9999 is accepted, and ingest there is 500 (LIM-12). | Part 2 (5.4, D10). |
| Gate | `tools/check_acceptance.py:13-29` (`REQUIRED`: AC-02, 04, 06, 15) and `:33-91` (`PHASE4_REQUIRED`), merged by `required_criteria()`. AC-01 and AC-03 are not in either. | Part 4 adds `PHASE5_REQUIRED` (10.4), including AC-01 and AC-03 (Q-9, decided). |
| CI branch conditions | `.github/workflows/ci.yml:49, 52, 74, 99, 106` run the flagged suite, the gate, the HTTP runner, Model 5 and the demo only on `main`, `phase4-integration` and PRs into main. | The base commit adds `phase5-integration` (11.1). |
| Dependencies | `pyproject.toml:10-17`: fastapi, sqlalchemy, alembic, psycopg, httpx, uvicorn. No PDF, crypto or JSON-Schema library. | D5 (PDF), D7 (crypto) and the AC-13 schema check add three dependencies (3.4, 4.3, 10.1). |
| Seeded accounts | `tools/seed_part1.py:55-56`: `rev-1` (REVIEWER), `viewer-1` (VIEWER), plus `sys-42` (SYSTEM). | `rev-2` if D17 is approved. |
| Contract copy | `pod_spec.txt` has the endpoint list only, not the Event and HarmField schemas or the three examples of `Contracts Schemas.docx`. | Conflict C11. Part 4 exports the full agreed contract (9.4). |
| Phase 4 deployment config | Not on main: `fly.toml` and `/livez` are on `development-deploy` (decision 229, LIM-02). | OD-1 (decided): they stay there; main is not changed (11.6). |

### 3. Report generation (REQ-12): the shared rules

The report rules are shared because Parts 2, 3 and 4 depend on them. Part 1 owns the code.

#### 3.1 What "assemble" means here (D3)

- **Report generation is the reviewer's `GET /v1/events/{event_id}/report`.** This is the
  contract's only report operation, and Phase 4 already tests AC-14's "report generation"
  through it (`tests/part2/test_disposition_api.py::test_ac14_the_reviewer_succeeds_on_both_actions`).
- The first such GET after the disposition now in force **assembles** the report. In one
  transaction it renders the JSON and PDF bytes, stores them, and appends `REPORT_ASSEMBLED`.
  Every later GET while that disposition is in force **serves the stored bytes** and assembles
  nothing.
- After a revision (a new `disposition` row), the next GET assembles a **new report version**
  bound to the new disposition row. Earlier versions stay stored and unchanged (3.5).
- REQ-12 says "when severity is confirmed". Under D3, severity is confirmed by the reviewer's
  first report request after the disposition, which is Model 1's "reviewer confirms severity"
  edge from CLASSIFIED to REPORT_READY. The alternative, assembling inside the disposition
  transaction, was rejected. It would make `assembled_at` equal to the determination time,
  against AC-13's "distinct where applicable", and it would couple Part 1 to the Phase 4
  disposition transaction.

#### 3.2 Report status, and the event state (D1, decided)

The contract's Event `state` stays exactly `FLAGGED | UNDER_REVIEW | DISPOSITIONED | CLOSED`.
Model 1's REPORT_READY and SUBMITTED are statuses of the **report record**, not event states:

| Model 1 | Stored as | Shown by |
| --- | --- | --- |
| REPORT_READY | A `report` row for the disposition in force, with no `report_submission` row | `GET …/reports` `status = "ASSEMBLED"` |
| SUBMITTED | A `report_submission` row for that report | `status = "SUBMITTED"` |
| (new) superseded | A `report` row whose `disposition_id` is no longer the latest disposition row of the event | `status = "SUPERSEDED"` or `"SUBMITTED_SUPERSEDED"` |
| (new) erased | `event.erased_at` set (Part 2) | `GET …/report` returns 410 `RECORD_ERASED` |

Status is **derived** when it is read (it is never a stored column), so the report table stays
insert-only.

**After a submission, `event.state` is unchanged: DISPOSITIONED** (a NOT_SERIOUS closure report
cannot be submitted, so a CLOSED event is never submitted). "Submitted" is exposed by
`GET /v1/events/{event_id}/reports`: the report's `status` is `"SUBMITTED"` (or
`"SUBMITTED_SUPERSEDED"` after a later revision), and its `submission` object is non-null. The
`REPORT_SUBMITTED` entry appears in `GET …/audit`. The contract Event object does not change.

#### 3.3 The Report object (proposed; contract change C3)

##### 3.3.1 Shape (Pinned once the pod agrees)

Field order is as listed. Times use the API format `YYYY-MM-DDTHH:MM:SSZ`, with a fraction only
when one is present (Phase 4 spec 7). Numbers are exact (`app/jsonout.py`). Objects the
contract already defines are reused unchanged: the HarmField object, the revision object, and
the disposition object's fields.

```json
{
  "report_id": "rpt-<32 hex>",
  "report_version": 1,
  "report_kind": "SERIOUS_INCIDENT",
  "schema_version": "pod-h-report-1",
  "event": {
    "event_id": "evt-…",
    "system_id": "sys-42",
    "type": "threshold_breach",
    "metric_name": "accuracy",
    "observed_value": 0.61,
    "threshold_value": 0.70,
    "detected_at": "2026-09-27T09:00:00Z",
    "evidence_bundle": {"window": {"start": "…", "end": "…"}, "samples": [ … ]},
    "evidence_sha256": "<64 hex>"
  },
  "harm_fields": {
    "health_safety_harm": {"status": "NOT_APPLICABLE", "evidence": null, "confirmed_by": "rev-1", "confirmed_at": "…"},
    "critical_infrastructure_disruption": { … },
    "fundamental_rights_infringement": { … },
    "property_environmental_damage": { … }
  },
  "determination": {
    "tier": "DEATH",
    "tier_label": "Death",
    "rationale": null,
    "disposition_id": "dsp-…",
    "reviewer_id": "rev-1",
    "assigned_at": "2026-09-27T13:00:00Z",
    "deadline_utc": "2026-10-07T13:00:00Z",
    "calendar": "calendar_days",
    "revisions": [{"prior_tier": "OTHER_SERIOUS", "new_tier": "DEATH", "reviewer_id": "rev-1", "revised_at": "…"}]
  },
  "timestamps": {
    "detected_at": "2026-09-27T09:00:00Z",
    "determined_at": "2026-09-27T13:00:00Z",
    "last_revised_at": "2026-09-30T13:00:00Z",
    "assembled_at": "2026-09-30T14:02:11Z"
  },
  "risk_indicator": {"detection_to_assignment_seconds": 14400},
  "regulation": {
    "instrument": "Regulation (EU) 2024/1689",
    "reporting_article": "Article 73",
    "serious_incident_definition": "Article 3(49)",
    "reporting_window_days": 10
  },
  "submission_marking": null,
  "article73_template": {
    "template": "Incident Report for Serious Incidents under the AI Act (High-risk AI systems)",
    "template_version": "1.0.0",
    "template_reference": "SB-10407",
    "fields": [
      {"field": "1.2 c", "name": "Date and time of detection of the incident",
       "status": "mapped", "value": "2026-09-27T09:00:00Z"},
      {"field": "1.3.2 a", "name": "Provider organisation name",
       "status": "not_available", "value": null}
    ]
  },
  "ledger_anchor": {"seq": 41, "hash": "<64 hex>"}
}
```

##### 3.3.2 Field sources

"REQ-12" in the last column marks a field that REQ-12 or AC-13 requires. "Added" marks a field
this spec adds, which the pod must agree to.

| Field | Source | Required by |
| --- | --- | --- |
| `report_id`, `report_version`, `report_kind`, `schema_version` | New `report` row (3.5); version = count of earlier reports for the event + 1 | Added |
| `event.event_id`, `system_id`, `type`, `metric_name`, `observed_value`, `threshold_value`, `detected_at` | `event` row, exactly as `events.detail` returns them (`app/events.py:45-93`) | REQ-12 "event data" |
| `event.evidence_bundle`, `event.evidence_sha256` | `event.evidence_bundle`, `event.evidence_sha256` (`app/evidence.py`). Null for gap events, which never reach a report. | REQ-05 "associated raw metric data" (added to the report) |
| `harm_fields` | `harm_field` rows, in the contract HarmField shape (`app/events.py:83-91`) | REQ-12 "four Article 3(49) severity fields" |
| `determination.tier`, `rationale` | The latest `disposition` row (the decision in force), as `dispositions.summary` (`app/dispositions.py:236-268`) | REQ-12 "reviewer's determination" |
| `determination.tier_label` | The REQ-08b label for the tier (Phase 4 spec section 6 table) | Added (makes the PDF readable) |
| `determination.reviewer_id`, `assigned_at` | The **first** disposition row, the same as the contract's `disposition` object (Phase 4 Appendix C) | REQ-12 |
| `determination.disposition_id` | The latest row's id, which this report is bound to | Added |
| `determination.deadline_utc`, `calendar` | `event.deadline_utc`, `disposition.calendar` | Added (REQ-10 context) |
| `determination.revisions` | `dispositions.summary(...)["revisions"]` | Added (REQ-09 context) |
| `timestamps.detected_at` | `event.detected_at` | REQ-12 "detection timestamp" |
| `timestamps.determined_at` | The first disposition row's `assigned_at` (the contract's `disposition.assigned_at`; Model 5 Ex-1: "determined 13:00") | REQ-12 "determination timestamp" |
| `timestamps.last_revised_at` | The latest row's `assigned_at` when there are revisions, else null | Added |
| `timestamps.assembled_at` | `clock.now()` at assembly, which is the same instant as `REPORT_ASSEMBLED.recorded_at` truncated to the API format | REQ-12 "report-assembly completion timestamp" |
| `risk_indicator.detection_to_assignment_seconds` | `determined_at − detected_at`, in whole seconds | Added (REQ-15 context) |
| `regulation.*` | Constants. `reporting_window_days` is 2, 10 or 15 by tier, null for NOT_SERIOUS | Added |
| `submission_marking` | null for `SERIOUS_INCIDENT`; the exact string `"NOT FOR SUBMISSION"` for `NOT_SERIOUS_CLOSURE` (D2) | Added |
| `article73_template` | One entry per field of the Commission template, in template order, built from the table in 3.3.4. `value` is filled only for `mapped` and `derivable` fields, and is null otherwise | Added (D18) |
| `ledger_anchor.seq`, `hash` | The ledger head read under the append lock immediately before `REPORT_ASSEMBLED` is appended | Added (ties the report to the chain) |

**Contract fields we cannot populate:** the contract defines no Report fields (C3), so no
contract field is missing. None of REQ-12's own items is missing. The template fields the system
cannot know are listed in `article73_template` with status `requires_reviewer_input` or
`not_available` and a null value (3.3.4). They are never guessed, in the same spirit as REQ-07's
"blank, not guessed".

##### 3.3.3 AC-13 "non-null and distinct where applicable" (Pinned)

For a `SERIOUS_INCIDENT` report: `event.*` (except `metric_name`, `observed_value` and
`threshold_value`, which exist for breach events, the only kind that can be reported),
`harm_fields` (four entries, each `status` CONFIRMED or NOT_APPLICABLE, with `confirmed_by` and
`confirmed_at`), `determination.tier`, `reviewer_id`, `assigned_at`, and the three
`timestamps.detected_at`, `determined_at` and `assembled_at` are **non-null**. The three
timestamps are **distinct and ordered**: `detected_at < determined_at < assembled_at`. The
schema check (`docs/contract/phase5/report.schema.json`, 10.1) enforces non-null. The ordering is a
separate assertion, because JSON Schema cannot compare fields.

Distinctness depends on time passing between the steps. The AC-13 tests advance the test clock
between flag, disposition and report. On the real clock, distinct whole seconds are not
guaranteed if a reviewer asks for the report in the same second as the disposition. **Q-3 is
decided (SS): distinct values at microsecond resolution are sufficient.** So (Pinned) the
assembly time is read from `clock.now()` only after the disposition transaction has committed
(D3 makes assembly a later request), and every report timestamp is written with `api_time`
(`app/formats.py:13-21`), which keeps fractional seconds and is never truncated to whole
seconds. The ordering is checked on those stored values.

##### 3.3.4 Article 73 fields: the Commission template (D18, decided)

**Documents read in full** (uploaded by SS to the project files on 2 November 2026):

| Document | Title as printed | Version and date | File |
| --- | --- | --- | --- |
| Template | "Incident Report for Serious Incidents under the AI Act (High-risk AI systems)" | "Reporting Template Version: 1.0.0", reference "(SB-10407)", 7 pages. Published as a draft on 26 September 2025; consultation closed 7 November 2025 (Commission consultation page and SS). The PDF's own metadata says created 31 July 2025. Its signature block shows a "Date" placeholder of "2026/01/01 02:00". | `Incident_report_for_serious_incidents_under_the_AI_Act_Highrisk_AI_systems_…_119623.pdf` |
| Guidance | "DRAFT GUIDANCE ARTICLE 73 AI ACT- INCIDENT REPORTING (HIGH-RISK AI SYSTEMS)" | No version or date printed; 13 pages. Published as a draft on 26 September 2025 (same source). PDF metadata: created 31 July 2025. | `Draft_Guidance_article_73_AI_Act__incident_reporting_…_119624.pdf` |

**Newer version:** none was found by one web search on 2 November 2026, which returned only the
draft and commentary on it. Whether a final version exists is **open question Q-13**, and none is
assumed. Both documents are drafts, so their field lists may change.

**What the guidance fixes (quoted from the draft guidance, paragraph numbers in brackets):**
Art. 3(49) as quoted in (5): "an incident or malfunctioning of an AI system that directly or
indirectly leads to any of the following: (a) the death of a person, or serious harm to a
person's health; (b) a serious and irreversible disruption of the management or operation of
critical infrastructure; (c) the infringement of obligations under Union law intended to protect
fundamental rights; (d) serious harm to property or the environment". (12) lists "Significant
drops in accuracy" among examples of incidents or malfunctions. (37) says the report goes to "the
market surveillance authorities of the Member States where that incident occurred", and "If the
exact location is not known to the provider, it is the business location of the deployer that
counts". (38) gives the 15-, 2- and 10-day limits "after the provider becomes aware of the
serious incident". (39): "an initial report that is incomplete should be submitted" if that is
needed for timely reporting. (42) and (43) forbid altering the system, including "modification
of log files", before informing the authorities.

**What our report is.** It is **not** the Commission template and does not replace the
provider's submission. It is a system-produced, hash-sealed annex that (a) fills every template
field the monitoring system can know, quoting the template's field number and name, and (b) lists
every other field as needing the reviewer or the provider's organisation. "Usable as an Article
73 submission" means: a provider could complete and file the template's **Initial** report
(guidance (39)) from our PDF plus its own organisation data, without re-deriving any value the
system holds. Manual check M3 judges that (10.6).

**Field mapping (Pinned; template v1.0.0).** Field names are quoted exactly from the template,
including its spelling and capitalisation. The contract defines no Report schema (C3), so the
"Report field" column names fields of the **proposed** schema (3.3.1). Status:
**mapped** = copied from a stored value unchanged; **derivable** = computed from stored values
by a pinned rule, and the reviewer confirms it; **requires reviewer input** = the reviewer
supplies it (recorded on the submission, never in the assembled bytes); **not available** = held
only by the provider's organisation, not in our system.

| Template field | Exact name in the template | Status | Report field (proposed schema) | Source in our data |
| --- | --- | --- | --- | --- |
| §1 | "Section 1: Administrative information" | (section) | `article73_template.fields` 1.x | — |
| 1.1 | "Responsible market surveillance authority in which country the incident occurred" | not available | — | — |
| 1.1 a | "Name of receiving market surveillance authority" | not available | — | — |
| 1.1 b | "Reference number assigned by market surveillance authority for this incident" | requires reviewer input | `GET …/reports` → `submission.authority_reference` | `report_submission.authority_reference` (3.7) |
| 1.2 | "Date, type, and classification of incident report" | (group) | — | — |
| 1.2 a | "Date of report submission" ("in format YYYY-MM-DD") | derivable (at submission) | `GET …/reports` → `submission.submitted_at` (date part) | `report_submission.submitted_at` |
| 1.2 b | "Date of incident" … "to" ("in format YYYY-MM-DD") | derivable (start only); end requires reviewer input | `event.evidence_bundle.window` and `event.detected_at` | `event.detected_at` (start date, provisional). No end date is held |
| 1.2 c | "Date and time of detection of the incident" ("in format YYYY-MM-DD") | mapped | `timestamps.detected_at` | `event.detected_at` |
| 1.2 d | "Manufacturer awareness date of reportability" ("in format YYYY-MM-DD") | derivable | `determination` → first serious-tier assignment | `assigned_at` of the first `disposition` row with a serious tier (equals `timestamps.determined_at` unless the event was first closed as NOT_SERIOUS). The reviewer confirms it (C20) |
| 1.2 e | "Type of report": "Initial", "Follow up", "Combined initial and final", "Final (Reportable incident)", "Final (Non-reportable incident)" | requires reviewer input | `GET …/reports` → `submission.report_type` | `report_submission.report_type` (3.7) |
| 1.2 f | "In case of initial and follow-up reports, please indicate the expected date of the next report" | requires reviewer input | `submission.expected_next_report_date` | `report_submission.expected_next_report_date` (3.7) |
| 1.2 g | "Classification of serious incident": "Death"; "Harm to a person’s health"; "Disruption of the management of critical infrastructure"; "Disruption of the operation of critical infrastructure"; "Infringement of obligations under Union law intended to protect fundamental rights"; "Harm to property"; "Harm to environment"; "All other reportable incidents" | derivable in part; requires reviewer input to finish | `article73_template` entry 1.2 g, with `value` = the list of candidate options by the rule below | `disposition.tier` and the four `harm_field.status` values (C17 to C19) |
| 1.3 | "Submitter information" | (group) | — | — |
| 1.3.1 a | "Submitter of the report": "Provider", "Deployer", "Authorised representative", "Other, please specify" | not available | — | — |
| 1.3.2 | "Provider information": a "Provider organisation name", b "Contact’s first name", c "Contact’s last name", d "Email", e "Phone", f "Country", g "Street", h "Street number", i "Address complement", j "PO Box", k "City name", l "Postal code" | not available (each) | — | — (contact details are personal data the system does not store; D20) |
| 1.3.3 | "Authorised representative information": a "Authorised representative organisation name", b to l as 1.3.2 | not available (each) | — | — |
| 1.3.4 | "Submitter’s details if not also provider or authorised representative": a "Registered commercial name of company", b to l as 1.3.2 | not available (each) | — | — |
| 1.3.5 | "Information about other incident obligations": "Incident was already reported to:", "Under Regulation/Law" | not available | — | — |
| §2 | "Section 2: AI System information" | (section) | — | — |
| 2.1 a | "Categorisation of system": "EU Database registration id" | not available | — | `monitored_system` holds `system_id` and sampling settings, no registration id (`migrations/versions/0001_platform_stub.py:62-68`) |
| 2.2 a | "System name (brand/trade /proprietary or common name)" | not available | `event.system_id` is given as the system's identifier in our system, not its name | `event.system_id` |
| 2.2 b | "Description of the System and its intended purpose" ("Nomenclature text") | not available | — | (`harm_context.context_text` describes harm context, not the system's intended purpose) |
| 2.2 c to h | "Model", "Catalogue/reference number", "Serial number", "Lot/batch number", "Software version", "Firmware version" | not available (each) | — | — |
| §3 | "Section 3: Incident information" | (section) | — | — |
| 3.1 a | "Provide a comprehensive description of the incident, including (1) what went wrong with the system (if applicable) (2) a description of the effects (if applicable) and (3)likely causality." | (1) derivable; (2) mapped; (3) requires reviewer input | (1) `event.metric_name`, `observed_value`, `threshold_value`, `evidence_bundle`; (2) `harm_fields.*.evidence` of CONFIRMED fields; (3) — | (1) `event` row, `metric_sample`; (2) `harm_field.evidence`; (3) not captured: the disposition rationale is required only for NOT_SERIOUS or a loosening |
| 3.2 a | "Estimated number of users affected" | requires reviewer input | — | — |
| 3.2 b | "Operator of system at the time of the incident": "Professional user", "Other, please describe" | not available | — | — |
| 3.2 c | "Remedial actions taken subsequent to the incident" | not available | — | — |
| 3.3 | "Initial reporter": a "Contact’s first name", b "Contact’s last name", C "Email", d "Phone", e "Country" ("if other, please specify"), f "Street", g "Street number", h "Address complement", i "PO Box", j "City name", k "Postal code" | not available (each) | `determination.reviewer_id` is shown as the pseudonymous id of the reviewer who determined severity | `disposition.reviewer_id` (pseudonym only; D20) |
| §4 | "Section 4: Provider analysis" | (section) | — | — |
| 4.1 a | "For initial and follow-up reports: preliminary results and conclusions of provider’s investigation" | requires reviewer input (the system supplies supporting facts) | `determination.tier`, `tier_label`, `harm_fields` | `disposition`, `harm_field` |
| 4.1 B | "Initial actions (corrective and/or preventive) implemented by the provider" | not available | — | — |
| 4.1 c | "What further investigations do you intend in view of reaching final conclusions?" | not available | — | — |
| 4.2 a | "For Final (Serious incident): Description of the providers’s evaluation concerning possible root causes /causative factors and conclusion" | not available | — | — |
| 4.2 b | "For Final (Non-reportable incident): Fill out rationale for why this is considered not reportable" | mapped (closure report only) | `determination.rationale` when `report_kind = NOT_SERIOUS_CLOSURE` | NOT_SERIOUS `disposition.rationale` (opened from its sealed form) |
| 4.2 c | "Has the risk assessment been reviewed ?" ("Yes", "No", "If 'No', rationale for no review required:"); "If the risk assessment has been reviewed, is it still adequate?" ("Yes", "No"); "Results of the assessment:" | not available | — | — |
| §5 | "Section 5: General comments" | derivable (system provenance) | `report_id`, `report_version`, `json_sha256` (header), `ledger_anchor` | `report` row and ledger (the PDF prints these under this heading) |
| Declaration | "I affirm that the information given above is correct to the best of my knowledge." and "Date" | not available (the provider signs) | — | — |

Every section, group and field of template v1.0.0 has a row and a status. No field was
left unmapped, so nothing in the table is UNVERIFIED against the template text. The template
itself is a draft (Q-13).

**Rule for 1.2 g candidates (Pinned).** The system never chooses a classification; it lists the
options that the stored decision supports, and the reviewer picks one:

| Stored decision | Candidate template options in `value` |
| --- | --- |
| `tier = DEATH` | "Death" |
| `tier = CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD` | "Disruption of the management of critical infrastructure", "Disruption of the operation of critical infrastructure", and, if `fundamental_rights_infringement` is CONFIRMED, "Infringement of obligations under Union law intended to protect fundamental rights" (the template has no "widespread infringement" option: C17) |
| `tier = OTHER_SERIOUS` | One option per CONFIRMED harm field: `health_safety_harm` → "Harm to a person’s health"; `critical_infrastructure_disruption` → both critical-infrastructure options; `fundamental_rights_infringement` → "Infringement of obligations under Union law intended to protect fundamental rights"; `property_environmental_damage` → "Harm to property" and "Harm to environment" (C18). If no field is CONFIRMED: "All other reportable incidents" |
| `tier = NOT_SERIOUS` | none (empty list) |

**AC-13 usability review (M3) checks, derived from the guidance.** A person, ideally from the
other team, takes the path-S PDF and JSON and the template v1.0.0, and records pass or fail for
each item:

- **U1 Transcription.** Every `mapped` and `derivable` field in the table can be copied into the
  template without re-computation, and its date is shown in the template's "YYYY-MM-DD" format
  next to our full timestamp.
- **U2 Initial report completeness (guidance (39)).** Every field the system can know is filled;
  every other field is listed by number and name as `requires_reviewer_input` or
  `not_available`; nothing is silently missing.
- **U3 Classification (guidance (5), (15) to (30)).** The 1.2 g candidates follow from the tier
  and the confirmed harm fields, and the confirmed evidence text supports the chosen class (for
  example, guidance (26) says fundamental-rights cases must "significantly interfere with
  Charter-protected rights on a large scale").
- **U4 Timing (guidance (38)).** `deadline_utc` matches the tier's 2, 10 or 15 days, and the
  report shows `detected_at`, `determined_at` and the 1.2 d derivation side by side, so the
  provider can judge its own awareness date (C8, C20).
- **U5 Causality (guidance (13), (14)).** The report states what went wrong (metric, threshold,
  observed value, samples) without asserting causality; 3.1 a (3) is flagged for the reviewer.
- **U6 Evidence integrity (guidance (42), (43)).** The report's hashes match the ledger, and
  `ledger_anchor` verifies against the export, so the provider can show the logs were not altered.
- **U7 Destination and scope (guidance (37), (40), (56) to (61)).** The report claims no authority
  or Member State, and the reviewer checks whether a sectoral rule limits reporting to
  fundamental-rights cases (for example a credit-scoring system under Annex III point 5(b)). The
  system does not know the Annex III category (risk R-6).
- **U8 Closure report.** It carries "NOT FOR SUBMISSION" in the JSON and on every PDF page, and its
  4.2 b text is the NOT_SERIOUS rationale.

M3 passes when U1 to U8 all pass and the reviewer writes a one-paragraph judgement on whether the
provider could file an Initial report from it.

##### 3.3.5 NOT_SERIOUS closure report (D2, decided)

A NOT_SERIOUS disposition in force gives a report with `report_kind = "NOT_SERIOUS_CLOSURE"`,
`submission_marking = "NOT FOR SUBMISSION"`, `determination.deadline_utc = null`,
`regulation.reporting_window_days = null` and the rationale. The PDF prints **NOT FOR
SUBMISSION** as a banner at the top of every page, in a type size no smaller than the title. It
is assembled and stored like any report but **cannot be submitted** (409 `NOT_SUBMITTABLE`).
SS's check (D2): AC-16, REQ-12 and the contract's report endpoint do not limit reports to serious
tiers, so option A stands. Model 5 Ex-2 ("no report") and Model 1 differ (C2).

Template fields for the closure report:

| Applies (filled or listed as for a serious report) | Does not apply (omitted from `article73_template`) |
| --- | --- |
| 1.2 b "Date of incident" (start, derivable); 1.2 c "Date and time of detection of the incident" (mapped); 3.1 a (1) and (2) (derivable, mapped); 4.2 b "For Final (Non-reportable incident): Fill out rationale for why this is considered not reportable" (mapped from the rationale); §5 provenance; and as `not_available` the §1.3 and §2 organisation and system fields | 1.1 a and b (no authority is involved); 1.2 a, e and f (no submission is recorded); 1.2 d "Manufacturer awareness date of reportability" (nothing was found reportable); 1.2 g "Classification of serious incident" (no serious class); 3.2 a to c; 4.1 a, B and c ("For initial and follow-up reports"); 4.2 a ("For Final (Serious incident)"); 4.2 c; the declaration |

The template's own "Final (Non-reportable incident)" type exists for an incident that was
reported and later found not reportable. Our rule makes every closure report non-submittable,
including for an event whose serious report was already submitted and which was then revised to
NOT_SERIOUS. That case conflicts with the template (C21). **Q-14 is decided (SS): submission of
closure reports stays refused.** This is a **known limitation** of Phase 5: the system cannot
record a "Final (Non-reportable incident)" submission, so the provider files that type outside
the system. It is listed in section 16 and in the operations note (7.5 item 9).

#### 3.4 "Hashed and stored exactly as produced" in bytes (D4, D5; Pinned)

1. At assembly, `reports.render_json(report) -> bytes` produces the JSON **once**: UTF-8, keys
   in the field order of 3.3.1, the exact-number encoder of `app/jsonout.py`, no trailing
   newline. `reports.render_pdf(report) -> bytes` produces the PDF **once** from the same
   in-memory report dict, not by re-parsing the JSON.
2. `json_sha256` = lowercase hex SHA-256 of the JSON bytes; `pdf_sha256` = the same of the PDF
   bytes.
3. Both byte strings are stored in PostgreSQL (`report.json_bytes`, `report.pdf_bytes`,
   `bytea`), sealed by Part 2 under the event's record key (4.3), so the stored column holds
   ciphertext. Both hashes are stored in plain text on the row and in the `REPORT_ASSEMBLED`
   payload. Hashes are computed over the **plaintext** produced bytes.
4. Every download serves `personal_data.open_bytes(...)` of the stored column, byte for byte,
   with `Content-Type: application/json` or `application/pdf`. A download MUST NOT render
   again. The response carries `ETag: "<sha256>"` and `X-Report-Id`, `X-Report-Version` and
   `X-Report-Sha256` headers (additive).
5. **PDF non-determinism.** PDF writers embed creation times and random document ids, so two
   renderings of the same report usually differ. Because the stored bytes are the record, this
   does not affect integrity. To keep tests simple, the renderer SHOULD still run in a
   reproducible mode: with reportlab, `invariant=1` and `CreationDate` set to `assembled_at`.
   A re-render is used **only** in a test to show that the renderer is stable. It MUST NOT
   replace stored bytes.
6. Object storage (Model 2's `pdf_ref object key`) is not used. Fly has no object store in this
   deployment, and keeping the bytes in the database puts them under the same backup, reset and
   erasure rules (Model 2 change, section 8).
7. Size: a report is a few kilobytes (the evidence bundle holds at most one hour of samples at a
   5-minute interval). No size limit is pinned. Part 1 records the observed sizes in its report.

#### 3.5 Data owned by Part 1 (migration 0008, `0008_reports.py`)

| Table.column | Type | Notes |
| --- | --- | --- |
| `report.report_id` | text PK | `rpt-…` (`app/formats.new_id`) |
| `report.event_id` | text FK → event | |
| `report.report_version` | int | Unique with `event_id`; starts at 1 |
| `report.disposition_id` | text FK → disposition, **unique** | At most one report per disposition row. Concurrent first GETs therefore produce one report (3.8) |
| `report.report_kind` | text | Check: `SERIOUS_INCIDENT`, `NOT_SERIOUS_CLOSURE` |
| `report.schema_version` | text | `pod-h-report-1` |
| `report.json_bytes`, `report.pdf_bytes` | bytea | Sealed (ciphertext) |
| `report.json_sha256`, `report.pdf_sha256` | text | 64 lowercase hex (check constraint) |
| `report.assembled_at` | timestamptz | |
| `report.assembled_by` | text | The reviewer's actor id |
| `report_submission.submission_id` | text PK | `sub-…` |
| `report_submission.report_id` | text FK → report, **unique** | One submission per report (D6) |
| `report_submission.event_id` | text FK → event | |
| `report_submission.submitted_by` | text | Actor id |
| `report_submission.submitted_at` | timestamptz | `clock.now()` |
| `report_submission.submission_kind` | text | `INITIAL` or `FOLLOW_UP` (D6) |
| `report_submission.report_type` | text | Template 1.2 e, exactly one of `Initial`, `Follow up`, `Combined initial and final`, `Final (Reportable incident)` |
| `report_submission.expected_next_report_date` | date null | Template 1.2 f |
| `report_submission.authority_reference` | text null | Template 1.1 b "Reference number assigned by market surveillance authority for this incident". Not personal data, so not sealed |

- **Both tables are insert-only**, exactly like `disposition`: the app role gets INSERT and
  SELECT, and a statement-level trigger blocks UPDATE and DELETE (copy of
  `migrations/versions/0006_dispositions.py:104-123`). Reset truncates them as the owner. Both
  go into the shared truncate list (11.1).
- Index on `report(event_id, report_version)`.

#### 3.6 `GET /v1/events/{event_id}/report` (Pinned; replaces Phase 4 spec 2.8)

Query parameter `format`: absent or `json` gives JSON; `pdf` gives PDF; anything else is
400 `MALFORMED_QUERY` with message `format must be json or pdf`. Order of checks; the first one
that fails decides the response:

| Step | Condition | Response |
| --- | --- | --- |
| 1 | No or unknown key | 401 `MISSING_OR_INVALID_KEY` (unchanged) |
| 2 | Role is not REVIEWER | 403 `FORBIDDEN_ROLE` (unchanged; REQ-13) |
| 3 | `format` invalid | 400 `MALFORMED_QUERY` |
| 4 | Unknown event | 404 `NOT_FOUND` (contract) |
| 5 | `event.erased_at` is set | 410 `RECORD_ERASED`, message `the personal data of this record has been erased` |
| 6 | No disposition row | 409 `NO_DISPOSITION` (contract; unchanged message `no disposition has been recorded for this event`). Gap events always land here. State unchanged, nothing written. |
| 7 | (only if D2 takes the other option) the disposition in force is NOT_SERIOUS | 409 `NOT_REPORTABLE` |
| 8 | A report exists for the latest disposition row | 200, the stored bytes. Nothing written (D19) |
| 9 | Otherwise | Assemble (3.8), commit, then 200 with the new bytes |

The status is 200 in steps 8 and 9 alike: the contract says "Success: 200, Report object". The
Phase 4 `NOT_IMPLEMENTED` error and the 501 branch of `app/observability.py:128-130` are
removed. The observability line keeps its 5xx rule without the 501 exception.

#### 3.7 Submission (D6; contract addition)

`POST /v1/events/{event_id}/report/submission`, REVIEWER only.

Request: `{ "report_id": "<rpt-…>", "json_sha256": "<64 hex>", "report_type": "<template 1.2 e>",
"expected_next_report_date": "YYYY-MM-DD" | null, "authority_reference": "<string>" | null }`.
`report_id`, `json_sha256` and `report_type` are required, and the hash proves which bytes the
reviewer submitted. `report_type` MUST be exactly one of the template's 1.2 e strings
`"Initial"`, `"Follow up"`, `"Combined initial and final"` or `"Final (Reportable incident)"`.
`"Final (Non-reportable incident)"` is refused (C21). `expected_next_report_date` is allowed
only with `"Initial"` or `"Follow up"` (template 1.2 f). Order of checks:

| Step | Condition | Response |
| --- | --- | --- |
| 1, 2 | Key, role | 401, 403 (as 3.6) |
| 3 | Body not an object; a field of the wrong type; an unknown field; a `report_type` outside the four strings; a bad date; or `expected_next_report_date` with another type | 400 `MALFORMED_BODY` |
| 4 | Unknown event, or a `report_id` not belonging to this event | 404 `NOT_FOUND` |
| 5 | Record erased | 410 `RECORD_ERASED` |
| 6 | `report_kind` is `NOT_SERIOUS_CLOSURE` | 409 `NOT_SUBMITTABLE`, `a not-serious closure report cannot be submitted` |
| 7 | The report is not bound to the latest disposition row | 409 `REPORT_NOT_CURRENT`, `the report was superseded by a later disposition` |
| 8 | `json_sha256` differs from the stored hash | 409 `REPORT_HASH_MISMATCH`, `json_sha256 does not match the stored report` |
| 9 | A submission already exists for this report | 409 `REPORT_ALREADY_SUBMITTED` |
| 10 | Otherwise | 201 `{submission_id, report_id, event_id, submitted_by, submitted_at, submission_kind, report_type, expected_next_report_date, authority_reference}`; one `REPORT_SUBMITTED` ledger entry |

- `submission_kind` is `INITIAL` when the event has no earlier submission, else `FOLLOW_UP`.
  Art. 73(5) (summary, UNVERIFIED) allows an incomplete initial report followed by a complete
  one, and the contract lets a reviewer revise at any time ("repeat calls append a revision").
  So a revision after a submission is **not** blocked: it creates a new report version, which
  can be submitted as a follow-up.
- **Immutable after submission:** the report row and its bytes (always), and the submission
  row. Nothing on the event changes, and the event state stays DISPOSITIONED (D1).
- A submission does not move the deadline, does not cancel reminders and does not change
  escalation rows. Alert A6 uses submission to tell "deadline passed and reported" apart from
  "deadline passed and not reported" (7.3).

#### 3.8 Transactions, locks and ledger entries (Pinned)

- **Assembly** is one transaction: lock the event row (`SELECT … FOR UPDATE`, which is the same
  lock the disposition transaction takes, so assembly and revision serialize), re-check steps 4
  to 8, read the latest disposition row, build the report, render both formats, seal, insert
  `report`, append `REPORT_ASSEMBLED` last, and commit. Lock order is unchanged from Phase 4
  spec 7: event row, then that event's child rows, then the ledger lock. Rendering is CPU-only;
  there is no network call inside the transaction.
- **Submission** is one transaction: lock the event row, run steps 4 to 9, insert the
  submission, append `REPORT_SUBMITTED`, and commit.

| entry_type | actor | subject | Payload (Pinned) |
| --- | --- | --- | --- |
| `REPORT_ASSEMBLED` | reviewer | event_id | `{event_id, report_id, report_version, report_kind, disposition_id, json_sha256, pdf_sha256, assembled_at, ledger_anchor_seq}` |
| `REPORT_SUBMITTED` | reviewer | event_id | `{event_id, report_id, submission_id, submission_kind, report_type, json_sha256, pdf_sha256}` |

No report content and no `authority_reference` goes into the ledger. Both entries appear in
`GET …/audit` because their subject is the event (`app/ledger/read.py:20-32`).

#### 3.9 Report listing and retrieval by id (contract additions)

- `GET /v1/events/{event_id}/reports` (REVIEWER or VIEWER): 200 with an array, oldest first, of
  `{report_id, report_version, report_kind, disposition_id, assembled_at, assembled_by,
  json_sha256, pdf_sha256, status, submission}`. Here `status` is one of `ASSEMBLED`,
  `SUBMITTED`, `SUPERSEDED` or `SUBMITTED_SUPERSEDED`, and `submission` is null or
  `{submission_id, submitted_by, submitted_at, submission_kind, report_type,
  expected_next_report_date, authority_reference}`. Errors: 401, 403 (SYSTEM),
  404. It does not contain report content, so VIEWER may read it (contract 5.1: "viewer can read
  events/audit/ledger"). After erasure it still lists the rows and hashes.
- `GET /v1/reports/{report_id}[?format=pdf]` (REVIEWER only): the stored bytes of that exact
  version, **never** assembling. Errors: 401, 403, 400 `MALFORMED_QUERY`, 404, 410.

### 4. Erasure versus the hash chain (REQ-16, AC-18)

#### 4.1 Where personal data is today (inventory)

"Personal data" here means any value that identifies a natural person or may contain free text
about one. Values that only identify the monitored system or a metric are not personal data.

| # | Location | Holds | Mutable? | In the hash chain? | Evidence |
| --- | --- | --- | --- | --- | --- |
| P1 | `ledger_entry.actor_id` | `rev-1`, `viewer-1`, `sys-42`, `system` (pseudonymous account ids) | No (trigger) | **Yes**: a canonical field | `app/ledger/canonical.py:120-133`; `tools/seed_part1.py:55-56` |
| P2 | `ledger_entry.payload.rationale` in `DISPOSITION_RECORDED` / `_REVISED` | Reviewer free text | No | **Yes** | `app/dispositions.py:195-230` |
| P3 | `ledger_entry.payload.url` in `WEBHOOK_REGISTERED` | Receiver URL (may embed tokens or names; LIM-13) | No | **Yes** | `app/webhooks.py:195-201` |
| P4 | `disposition.rationale` | Reviewer free text | No (trigger) | No | `migrations/versions/0006_dispositions.py:71` |
| P5 | `disposition.reviewer_id`, `harm_field.confirmed_by` | Account ids | `disposition`: no; `harm_field`: yes | No | 0006 line 72; 0003 line 135 |
| P6 | `harm_field.evidence`, `harm_field.prefill_evidence` | Pre-filled or reviewer evidence text | Yes | No (Phase 4 spec 2.10) | 0003 line 134; 0005 line 72 |
| P7 | `harm_context.context_text` | System-level context, configured per monitored system | Yes (owner) | No | 0005 lines 58-71. **Not personal**: one row per system, not per event |
| P8 | `webhook.url` | Receiver URL | Yes | No | 0004 line 52 |
| P9 | `api_key.actor_id` | Account id; no name, e-mail or person mapping is stored anywhere in the system | Yes (owner) | No | 0001 lines 75-85 |
| P10 | `event.evidence_bundle`, `metric_sample` | Metric values and timestamps | `event`: yes | No | `app/evidence.py:1-16`. **Not personal** under assumption A-4 |
| P11 | **New:** `report.json_bytes` and `pdf_bytes` | Copies of P2, P4, P5 and P6 for one event | No (insert-only) | Hashes only | 3.5 |
| P12 | Logs (Fly) and database backups | Request logs do not carry bodies (`app/observability.py`); backups carry everything | n/a | n/a | Not erasable by the application (4.7) |

The pinned hash (`app/ledger/canonical.py:1-8`) covers P1, P2 and P3 **as stored**. Editing any
of them changes that entry's hash and fails verify at that seq, and the Part 2 addendum pins
that hash. Erasure therefore MUST NOT change any stored ledger field, and it cannot UPDATE
`disposition` or the new insert-only report tables either.

#### 4.2 Designs considered

**Design A: commitments plus a separate erasable store.** From migration 0009, a personal value
never enters an append-only store. The ledger payload holds a salted commitment
`{"pd": "sha256:<hex of salt‖value>"}`. The value and its salt live in a mutable
`personal_value(subject_id, ref, salt, value)` table, and `disposition.rationale` and the report
columns hold a reference to that table. Erasure deletes the subject's rows.

- *For:* no new dependency and no key material; the standalone verifier is unchanged; the
  commitment proves that a value existed, and the salt stops dictionary guessing.
- *Against:* every reader of P2, P4 and P11 changes to dereference. `disposition.rationale`
  changes meaning, and the CHECK constraints `ck_disposition_not_serious_rationale` and
  `ck_disposition_rationale_not_empty` (Phase 4 `docs/models/phase4_models.md` INV-2 row) must
  change. Report bytes cannot be split into referenced parts, so a report would have to be
  stored whole in the erasable table. That gives two erasure mechanisms (delete for values,
  delete for blobs). Model 2's `record_key` and INV-11's wording no longer describe the design.

**Design B: per-record keys (crypto-shredding), recommended (D7).** Each event gets a random
256-bit data key in `record_key` (Model 2). Every personal value written to an **append-only**
store (P2, P4, P11) is sealed with AES-256-GCM under its event's key. The ledger payload and the
table column hold the sealed string `pd1:<base64url(nonce‖ciphertext‖tag)>`, with associated
data `subject_id|field`. Personal values in **mutable** columns (P6) are stored in plain text, as
today, and are set to null by erasure. Erasure destroys the key (`record_key.data_key = NULL`,
`destroyed_at = now`), and every sealed value of that record becomes permanently unreadable at
once.

- *For:* nothing in `ledger_entry`, `disposition`, `report` or `report_submission` is ever
  updated, so INV-4 still holds. Ledger hashes cover the sealed string, so **they do not
  change**. This is Model 4 INV-11 word for word ("Destroying a record_key changes no ledger
  hash"). One erasure point covers the ledger, the dispositions and the report bytes. The
  standalone verifier stays standard-library only, because it hashes the stored strings and
  never decrypts. The export now shows sealed strings instead of rationale text.
- *Against:* one new runtime dependency (`cryptography`, Apache-2.0 or BSD). Readers of P2 and
  P4 must open values (`dispositions.summary`, the report builder). Phase 3 and 4 tests that
  read `rationale` from SQL or from a payload change (10.5). **Keys are unwrapped and stored
  in the same database** (D7, as approved): the design gives **erasure, not confidentiality**.
  Anyone who can read the database before erasure can decrypt, which is no worse than today's
  plain text. **Only rows written from migration 0009 on are erasable.** Rows written before migration 0009 stay
  plain and cannot be erased (4.7).

**Design C: hash a payload digest instead of the payload (rejected).** Changing the canonical
form to hash `sha256(payload)` and storing the payload beside it would allow deleting payloads,
but it changes the pinned hash definition, invalidates `fixture_export.json` and the README
worked example, and breaks every copy of `tools/verify_ledger.py` the other team holds.

| Criterion | A: commitments | B: per-record keys | C: payload digest |
| --- | --- | --- | --- |
| Ledger hashes unchanged by erasure (AC-18) | Yes | Yes | Yes |
| Hash definition unchanged (Part 2 addendum, fixture) | Yes | Yes | **No** |
| Updates to append-only tables | None, but `disposition` changes meaning | None | Ledger changes |
| Standalone verifier | Unchanged | Unchanged | Rewritten |
| New dependency or secret | None | `cryptography`; no secret | None |
| Report bytes | Stored whole in the erasable table | Sealed under the same key | n/a |
| Matches Model 2 and INV-11 | No (model change) | Yes | No |
| Phase 3/4 tests changed | P2, P3, P4 readers, plus two CHECK constraints | P2, P3, P4 readers | Many |

#### 4.3 Data owned by Part 2 (migration 0009, `0009_personal_data.py`)

| Table.column | Type | Notes |
| --- | --- | --- |
| `record_key.subject_id` | text PK | The event id. One key per event, created on the first seal |
| `record_key.data_key` | bytea null | 32 random bytes (`secrets.token_bytes(32)`), stored unwrapped (no key-encryption key): for erasure, not confidentiality (D7); null once destroyed |
| `record_key.created_at`, `destroyed_at` | timestamptz, timestamptz null | Check: `(data_key IS NULL) = (destroyed_at IS NOT NULL)` |
| `event.erased_at` | timestamptz null (new column) | Set by erasure. Read by the report and by `events.detail` |

- The app role gets SELECT, INSERT, UPDATE on `record_key` and no DELETE. The row is kept as a
  tombstone, so "erased" stays provable. `record_key` is added to the shared truncate list.
- Interfaces (Pinned, section 11.2): `personal_data.seal(session, subject_id, field, text) -> str`,
  `seal_bytes(...) -> bytes`, `open(session, subject_id, field, stored) -> str | None`,
  `open_bytes(...) -> bytes | None`. `open` returns the input unchanged when it does not start
  with `pd1:` (pre-0009 rows), and returns None when the key is destroyed.
- Call sites that change (Part 2 owns these edits): `app/dispositions.py` seals the rationale
  for the disposition row and the ledger payload, and `dispositions.summary` opens it.
  `app/webhooks.py` gets the D16 change (4.6). Part 1's report builder seals the report bytes
  and opens the rationale.

#### 4.4 Erasure (D7, D8; contract addition)

`POST /v1/events/{event_id}/erasure`, REVIEWER only, with an empty body or `{}`. Order of checks:

| Step | Condition | Response |
| --- | --- | --- |
| 1, 2 | Key, role | 401, 403 |
| 3 | Body present and not `{}` | 400 `MALFORMED_BODY` |
| 4 | Unknown event | 404 `NOT_FOUND` |
| 5 | `clock.now() < event.retain_until` | 409 `RETENTION_NOT_EXPIRED`, `the retention period of this record has not ended`; nothing written |
| 6 | `event.erased_at` already set | 409 `ALREADY_ERASED` |
| 7 | Otherwise | 200 `{event_id, erased_at, erased_fields}`; one `RECORD_ERASED` entry |

In one transaction: lock the event row; destroy the event's `record_key` (if there is one); set
`harm_field.evidence` and `prefill_evidence` to null for the event; set `event.erased_at`; then
append `RECORD_ERASED {event_id, erased_at, retain_until, key_destroyed: bool, erased_fields:
[<names>]}`, where `erased_fields` lists the field names (for example
`"disposition.rationale"`, `"harm_field.evidence"`, `"report.json_bytes"`), never values.

After erasure:

- `GET /v1/events/{id}` returns 200 with `evidence: null` in every HarmField and
  `disposition.rationale: null`. Everything else is unchanged.
- `GET …/report` and `GET /v1/reports/{id}` return 410 `RECORD_ERASED`.
- `GET …/reports` still lists the rows and hashes.
- `GET …/audit` shows `RECORD_ERASED`.
- Export and both verifiers: valid; every earlier entry is byte-identical.

Erasure is **operator-triggered, not automatic** (D8). REQ-16 says "support rendering …
unreadable after its retention period". An automatic pass could erase data that an open
investigation still needs (Art. 73(6), summary, UNVERIFIED), and no artifact defines a legal
hold. The dashboard lists records past `retain_until` that have not been erased (7.2).

#### 4.5 What AC-18 checks (Pinned)

`test_ac18_*` (Part 2, in-process with the real database) and
`tests/acceptance/test_ac18_erasure.py` (HTTP) do the following:

1. **Setup:** reset; register a webhook; breach on `sys-42`; reviewer view; dispose
   OTHER_SERIOUS with all four fields decided, `fundamental_rights_infringement` CONFIRMED with
   evidence `"Applicant Jane Example was refused a loan"` and rationale `"Raised by Jane
   Example's complaint"`; revise to DEATH with a rationale; GET the report (assembled); submit
   it. Export the ledger (E0) and run server verify (V0).
2. **Pre-check (readable):** the Event shows the evidence and the rationale; the report JSON
   contains `"Jane Example"`; SQL shows the `disposition.rationale` and DISPOSITION payload
   values starting with `pd1:` and not containing `"Jane"`; `open` returns the plain text.
3. **Too early:** `POST …/erasure` returns 409 `RETENTION_NOT_EXPIRED`; nothing changed (E0 is
   still a prefix of the export, and no `RECORD_ERASED` entry exists).
4. **Advance** the test clock past `retain_until` (from the event's `retain_until`, read through
   the owner engine in-process; over HTTP, by `max(6 calendar months, 183 days) + 1 day` from
   `detected_at`). Then `POST …/erasure` returns 200.
5. **Post-check (unreadable):** Event evidence and rationale are null; report endpoints return
   410; `record_key.data_key IS NULL`; `open` of each stored sealed value returns None; no
   stored column of the event, its dispositions or its reports contains `"Jane"` in plain text.
6. **Chain unchanged:** the new export E1 has exactly `len(E0) + 1` entries; `E1[:len(E0)] ==
   E0` field for field, so every entry has the same seq, the same hash and the same sealed
   payload; `E1[-1]` is `RECORD_ERASED`; server verify is `{valid: true, entries_checked:
   len(E0)+1, first_mismatch: null}`; `tools/verify_ledger.py` on E1 gives the same result.
7. **Idempotence:** a second erasure returns 409 `ALREADY_ERASED`.

#### 4.6 Webhook URLs in the ledger (D16, LIM-13)

From 0009, `WEBHOOK_REGISTERED`'s payload is `{webhook_id, url_sha256, event_types}`, where
`url_sha256` is the lowercase hex SHA-256 of the UTF-8 URL. It replaces the `url` key. The URL
stays in `webhook.url`, which delivery needs and which is not exported. This changes a Phase 3
payload (Appendix D) and the tests that read it (10.5).

#### 4.7 Limits of erasure (stated, not solved)

- **Only rows written from migration 0009 on are erasable** (D7, as approved). Rows written
  **before** it hold plain text and cannot be erased by this design.
  The deployed database contains only test data (LIM-14 workaround). The Phase 5 deployment
  runs a reset after migrating (11.6). Production use would start after 0009.
- Database **backups** and Fly volume snapshots keep destroyed keys until the backups expire.
  Erasure is "permanent" only once the backups that hold the key are gone. The operations note
  states the backup retention (UNVERIFIED for Fly Postgres; open question Q-6).
- `actor_id` (P1) is a pseudonymous account id, and the mapping to a person is not stored in the
  system (P9). It is not erased (D20). If a team ever seeds human names as actor ids, this
  stops holding (assumption A-3).

### 5. Retention (REQ-05, AC-05)

#### 5.1 The rule (unchanged; Pinned)

`retain_until = max(detected_at + 6 calendar months, detected_at + 183 days)`
(`app/formats.py:44-50`), set once when the event is created. It applies to the event, its harm
fields, dispositions, escalations, reports, submissions, ledger entries and the metric samples
linked to it (`metric_sample.event_id`), plus its evidence bundle.

#### 5.2 Kept, purged and erased

- **Kept:** everything, for at least `retain_until`. **No purge exists in Phase 5 (D9).**
  AC-05 says "absence at 6mo1d is not a failure (minimum, not maximum)", so a purge is allowed
  but not required, and a purge would remove rows from tables that other rows reference, which
  would need its own rules.
- **Erased after `retain_until`, on request:** the personal-data fields of section 4 only. The
  event, its metric values, its states, its hashes and its ledger entries stay.
- **Interaction:** erasure is refused before `retain_until` (4.4 step 5). That is how INV-10
  ("No event record or its evidence bundle is removed before retain_until") is enforced and
  tested.
- **Unlinked metric samples** (healthy samples with no event) are not covered by REQ-05 ("every
  flagged event and its associated raw metric data") and are kept like everything else.

#### 5.3 AC-05 test design (mocked clock)

With T0 = the breach sample's timestamp, `5mo29d = add_calendar_months(T0, 5) + 29 days` and
`6mo1d = add_calendar_months(T0, 6) + 1 day` (`app/formats.add_calendar_months`):

1. Reset; ingest four healthy samples and one breach for `sys-42` at T0 (evidence bundle of five
   samples).
2. Advance the clock by `5mo29d − now`. `GET /v1/events/{id}` returns 200 with the same fields
   as at flag time. In-process: the `event` row, its `evidence_bundle` (whose SHA-256 equals
   `evidence_sha256`) and its five `metric_sample` rows are present.
3. Advance to `6mo1d`. `GET` returns either 200 with unchanged fields, or 404; **both pass**,
   and any other status (in particular 500) fails. In-process the test also checks that
   `retain_until` equals the rule of 5.1.
4. Boundary for INV-10: at `5mo29d`, `POST …/erasure` returns 409 `RETENTION_NOT_EXPIRED`
   (Part 2, merge 2b, test `test_nothing_is_erased_before_retain_until`; dropped with erasure
   if erasure is cut, 1.4). Steps 1 to 3 do not depend on erasure.

The advance runs the sweep and reminder passes (`POST /v1/test/clock/advance`, Phase 4 spec
4.5), so gap events for `sys-42` appear during the jump. The test reads only its own event id.
A six-month advance needs a test environment with a clean clock, so the HTTP version runs only
in the acceptance runner after its own reset (LIM-03).

#### 5.4 The year-9999 limit (LIM-12, D10; Pinned)

`platform.advance_clock` MUST refuse with 400 `MALFORMED_BODY` (the same body as other bad
values) any advance whose simulated time would be at or after `9999-07-01T00:00:00Z`, in place
of the `datetime.max` check of decision 226. For any detected time before that instant,
`retain_until` is representable: `9999-06-30T23:59:59Z + 6 months = 9999-12-30T23:59:59Z`, and
+183 days gives the same. The 2^63 − 1 offset check stays. Test:
`test_clock_advance_into_the_last_half_of_9999_is_400`. A future-dated **sample timestamp** in
the second half of 9999 can still overflow at ingest, because `detected_at` is the sample's own
timestamp (Phase 4 INV-13 "Differs"). Ingest MUST reject such a sample with 400
`MALFORMED_BODY` (test `test_a_sample_dated_after_9999_07_01_is_400`).

### 6. Ledger hardening (REQ-06)

#### 6.1 Nightly verification (D11, decided)

- **Where:** a step in the existing worker loop (`app/worker.py:26-50`), no new process.
  `ledger_ops.nightly_due(engine)` is true when no `ledger_verification_run` with trigger
  `NIGHTLY` exists for the current **wall-clock** UTC date and the wall-clock hour is at least
  `NIGHTLY_VERIFY_HOUR_UTC` (default 2). Wall clock, like the sweeper heartbeat, because this is
  an operational job and the simulated clock jumps.
- **What it runs**, in one read-only transaction plus one insert:
  1. `verify_ledger(session)` (unchanged, `app/ledger/verify.py:71-79`);
  2. the **head comparison**: the run's head (seq, hash) against the previous run's head, which
     MUST still be in the chain at the same seq with the same hash. A smaller head seq or a
     changed hash at the previous head is `head_check = "REGRESSED"`;
  3. the **projection check** (6.3);
  4. the **report-bytes check**: for every non-erased report, open the stored bytes and compare
     their SHA-256 with `report.json_sha256` and `pdf_sha256` and with the `REPORT_ASSEMBLED`
     payload (Part 1's `reports.verify_stored`).
- **Where results go:** `ledger_verification_run` (migration 0010), **not** the ledger.
  Appending a ledger entry every night would change the exact entry counts that Phase 3 and 4
  tests assert whenever CI runs near the nightly hour.

| Column | Type | Notes |
| --- | --- | --- |
| `run_id` | text PK | `lvr-…` |
| `trigger` | text | `NIGHTLY` or `MANUAL` |
| `started_at`, `finished_at` | timestamptz | Wall clock |
| `valid`, `entries_checked`, `first_mismatch` | bool, int, text null | Exactly verify's three fields |
| `head_seq`, `head_hash` | bigint null, text null | Null for an empty ledger |
| `head_check` | text | `OK`, `REGRESSED`, `FIRST_RUN` |
| `projection_mismatches` | jsonb | Array of `{event_id, column, expected, actual}`; empty when clean |
| `report_mismatches` | jsonb | Array of `{report_id, which}` |

- App role: INSERT, SELECT. The table is insert-only (trigger), in the truncate list.
- An invalid result, `REGRESSED`, or any projection or report mismatch logs **ERROR**
  `alert: code=LEDGER_INTEGRITY …` with the seq or ids and no values, and raises dashboard
  alert A1 (7.3). This is Model 5 Ex-5's "critical alert raised; modification detected, not
  prevented".
- `MANUAL` runs: `POST /v1/ledger/verifications` (REVIEWER only, 201 with the run) runs the
  same job on demand, and `GET /v1/ledger/verifications?limit=n` (REVIEWER or VIEWER; default
  10, maximum 100, newest first) lists runs. These are the "ledger-verification endpoints" of
  the Phase 5 description (contract addition; section 9). `GET /v1/ledger/verify` is unchanged:
  exactly three fields (Part 2 addendum rule 6).

#### 6.2 Chain head outside the database (D12, decided, with SS's condition)

- **Store:** a branch `chain-heads` in this repository, written only by a scheduled GitHub
  Actions workflow `.github/workflows/chain-head.yml` (cron `17 3 * * *`, daily at 03:17 UTC,
  after the nightly verify, plus `workflow_dispatch`). Each run appends one line to
  `heads.jsonl`: `{"taken_at", "base_url", "app_version", "seq", "hash", "recorded_at",
  "verify": {valid, entries_checked, first_mismatch}}`.
- **What the workflow does:** download `GET /v1/ledger/export` with the viewer key (GitHub secret
  `POD_VIEWER_KEY`, **needs the team's approval**: hard rule 3). Run `python
  tools/verify_ledger.py export.json --anchor heads.jsonl` (new option, standard library only).
  Append the new head and push to `chain-heads`. **Every run compares the new head with the
  previous committed head** (the last line of `heads.jsonl`, seq s and hash h). The job fails
  (a red run; GitHub e-mails the repository owner) and commits nothing when:
  1. the export does not verify; or
  2. **the chain is shorter**: the export's last seq is smaller than s; or
  3. **the old head is missing**: the export has no entry s, or its entry s has a hash other
     than h (truncation below s, or a forged or rewritten entry at or below s).
- `--anchor` output adds one line, `anchor: OK seq=<s>` or `anchor: MISMATCH seq=<s>`, after the
  existing result. Without `--anchor`, the script's output and exit codes are unchanged, so the
  other team's copy keeps working.
- **What is detected:** truncation of anchored entries; a forged or rewritten entry at or below
  the last anchor, even with consistent hashes after it, because the anchor holds the original
  hash.
- **Test resets (Pinned, per SS's condition):** a `POST /v1/test/reset` on the deployed service
  truncates the ledger, so the next scheduled run **fails**, as a shorter chain must. The run
  records `reset_count` from `GET /healthz` (6.5) in its log, so the operator can see whether a
  reset explains the failure, but it does **not** skip the check. To start a new series, a
  person runs the workflow by hand (`workflow_dispatch`) with the inputs `new_series: true` and
  a non-empty `reason`. That run appends a line `{"new_series": true, "reason", "reset_count",
  "previous_seq", "previous_hash", "actor"}` before the new head, so the break stays visible in
  git history. A scheduled run can never start a new series.
- **What stays undetected:** entries appended **after** the last anchor (up to about 24 hours)
  can be truncated or forged without detection until the next anchor run, and even then they
  were never anchored. Shrinking the window means anchoring more often (the cron is a
  configuration change). This is stated in the operations note.

#### 6.3 Event-table edits not covered by the chain (D13)

The chain covers ledger rows, not the `event`, `disposition` or `harm_field` rows (Phase 4
report Q1.1, REQ-06 "partial"). **Projection check:** `ledger_ops.projection_check(session)`
replays each event's ledger entries and compares the result with the current rows:

| Column | Expected from the ledger |
| --- | --- |
| `event.state` | `EVENT_FLAGGED` or `GAP_OPENED` → FLAGGED; `REVIEW_STARTED` → UNDER_REVIEW; latest `DISPOSITION_*` tier → DISPOSITIONED or CLOSED; `GAP_CLOSED` → CLOSED |
| `event.deadline_utc` | Latest `DISPOSITION_*` payload `deadline_utc` (null if none) |
| `event.system_id`, `metric_name`, `observed_value`, `threshold_value` | `EVENT_FLAGGED` payload (decimal strings compared exactly) |
| latest `disposition` row: `tier`, `deadline_utc`, `anchor_at`, `calendar` and the row count | `DISPOSITION_*` payloads in order |
| `harm_field.status` (four) | Latest `DISPOSITION_*` payload `harm_fields` |
| `report` rows: count, hashes | `REPORT_ASSEMBLED` payloads |

**Still uncovered:** `event.detected_at`, `received_at`, `retain_until`, `evidence_bundle` and
`evidence_sha256`, `harm_field.evidence` and `confirmed_at`, and `metric_sample` rows. None of
them appears in any ledger payload today. Covering them would mean adding them to the
`EVENT_FLAGGED` payload from 0010 onwards, a change to a pinned Phase 3 payload. **SS decided
not to (Q-7)**, and the operations note lists these columns as uncovered.

#### 6.4 Signals

`ledger_verification_run` (latest), the `chain-heads` workflow status (GitHub), and the ERROR log
line of 6.1. The dashboard (7) and `/healthz` (6.5) expose the latest run.

#### 6.5 `/healthz` (Pinned; deployed-version traceability, D15)

The body gains four fields; the old four keep their names, meanings and order, and the 503 rules
are unchanged:

```json
{"sweeper_last_ran_seconds_ago": 12, "pending_deliveries": 0,
 "oldest_pending_delivery_seconds": null, "overdue_escalations": 0,
 "version": "e53d7c7…", "last_verification": {"finished_at": "…", "valid": true, "head_check": "OK"},
 "last_verification_age_seconds": 3600, "reset_count": 3}
```

- `version` comes from the environment variable `APP_VERSION`, set at image build from a
  Dockerfile `ARG GIT_SHA`. The Dockerfile build argument and this field are on **main** (Part
  3). The deployer passes `--build-arg GIT_SHA=<the main commit being deployed>` **explicitly**,
  never `git rev-parse HEAD`, because the image is built from `development-deploy`, whose head
  is a merge commit, not the main commit (11.6). It is null when unset; it is never read from
  git at run time.
- `last_verification` is null before the first run. `reset_count` is a counter in a new
  single-row table `platform_counter`, incremented by `platform.reset()`.
- Ledger integrity does **not** make `/healthz` 503, because a 503 there makes the platform
  treat the process as unhealthy (a liveness signal), and restarting does not fix a tampered
  ledger. Integrity is an alert (7.3).
- **OD-1 (decided by SS):** deployment config (`fly.toml`, `GET /livez`) **stays on
  `development-deploy`**. It is **not** added to main or to the Part 4 base commit. The Fly
  health check uses `/livez` on `development-deploy`, or `/healthz`; the choice is made there,
  and the operations note describes both (7.5). This document still does not read
  `development-deploy` (hard rule 3).

### 7. Health dashboard and operations note

#### 7.1 Placement (D11, D14)

`GET /v1/ops/dashboard` in the API process (REVIEWER or VIEWER; SYSTEM 403; no key 401), plus a
"Health" view in the existing console (`app/static/`) that calls it every 30 seconds. Data comes
only from the database and the environment, never from in-process counters: `observability`'s
counters (`app/observability.py:39-62`) are per process, so the API cannot see the worker's.

#### 7.2 Content (Pinned shape)

```json
{
  "generated_at": "…", "version": "…", "environment": "test",
  "worker": {"sweeper_last_ran_seconds_ago": 12, "pending_deliveries": 0,
             "oldest_pending_delivery_seconds": null, "failed_deliveries_24h": 0,
             "overdue_escalations": 0},
  "ledger": {"entries": 412, "head_seq": 412, "last_verification": { …run… },
             "last_verification_age_seconds": 3600},
  "events": {"by_state": {"FLAGGED": 1, "UNDER_REVIEW": 0, "DISPOSITIONED": 2, "CLOSED": 5},
             "open_gaps": 0},
  "deadlines": {"due_within_24h_unsubmitted": 1, "passed_unsubmitted": 0, "passed_submitted": 1},
  "reports": {"assembled": 3, "submitted": 2, "superseded": 1},
  "retention": {"past_retain_until_not_erased": 0, "erased": 1},
  "clock": {"simulated_time": "…", "offset_seconds": 0},
  "alerts": [{"code": "A6", "severity": "critical", "since": "…", "detail": {"event_id": "evt-…"}}]
}
```

`clock` is present only when `ENVIRONMENT=test`. `detail` carries ids and counts, never personal
data.

#### 7.3 Alert conditions (Pinned thresholds; configurable where a variable is named)

| Code | Condition | Severity | Threshold source |
| --- | --- | --- | --- |
| A1 | Latest verification invalid, `head_check = REGRESSED`, or any projection or report mismatch | critical | 6.1 |
| A2 | No verification run for more than 26 hours (wall clock) | warning | One nightly run plus 2 hours of slack |
| A3 | `sweeper_last_ran_seconds_ago > 3 × SWEEP_INTERVAL_SECONDS` | critical | Same rule as the existing `/healthz` 503 (`app/api/health_routes.py:114-116`) |
| A4 | `oldest_pending_delivery_seconds > NOTIFY_MAX_DELAY_SECONDS` (900) | warning | REQ-04 bound |
| A5 | `overdue_escalations > 0` | warning | LIM-08 |
| A6 | A DISPOSITIONED event whose `deadline_utc < now` has no submitted report bound to its latest disposition | critical | REQ-10, Art. 73 |
| A7 | A DISPOSITIONED event with `deadline_utc − now < ALERT_DEADLINE_WARNING_HOURS` (default 24) and no submitted current report | warning | |
| A8 | `failed_deliveries_24h > 0` (FAILED `notification_delivery` rows whose last attempt was in the last 24 h) | warning | |
| A9 | `past_retain_until_not_erased > 0` | info | REQ-16 operator task |

`now` is `clock.now()` for A6 and A7 (simulated time in tests) and wall time for A2 and A3.
**Delivery of alerts:** the dashboard's `alerts` array, and the worker logs each raised alert
once per code and subject per wall-clock UTC day (`alert: code=A6 event_id=…`, ERROR for
critical, WARNING otherwise), visible in Fly logs. The anchor workflow's failure is reported by
GitHub. **No new webhook event type**: the contract fixes `event_types` to `flag_notification`
and `reminder`, so an `ops_alert` type would be a contract change; **SS decided not to add one
(Q-8)**.

#### 7.4 Role access

| Endpoint | SYSTEM | VIEWER | REVIEWER | none |
| --- | --- | --- | --- | --- |
| `GET /healthz` | 200 | 200 | 200 | 200 (unchanged: no key) |
| `GET /v1/ops/dashboard` | 403 | 200 | 200 | 401 |
| `GET /v1/ledger/verifications` | 403 | 200 | 200 | 401 |
| `POST /v1/ledger/verifications` | 403 | 403 | 201 | 401 |

#### 7.5 Operations note (`docs/operations.md`, Part 3; outline Pinned)

1. Processes and where they run (api, worker, prepare, sink; Fly and Vercel; compose for CI).
2. Release process and version (OD-1): main is merged into `development-deploy`; the image is
   built and deployed from there with `--build-arg GIT_SHA=<the main commit being deployed>`
   passed explicitly (not `git rev-parse HEAD` of the deploy branch); after the deploy,
   `/healthz` `version` must equal that main commit (11.6 steps 4 and 5). The Fly health-check
   path: `/livez` as configured on `development-deploy`, or `/healthz`; what each checks, and
   why a tampered ledger does not fail either (6.5).
3. Health signals: every `/healthz` and dashboard field, with its meaning.
4. Alerts A1 to A9: condition, threshold, what to check, what to do (runbook per alert,
   including Model 5 Ex-5's tamper response: stop writes, export, compare with `chain-heads`,
   preserve the database, inform the team).
5. Nightly verification and anchoring: schedule, where results live, how to run one manually,
   what is and is not detected (6.2, 6.3). **Restarting the anchor series after a test reset**
   (6.2): (a) confirm from the failed run's log that `reset_count` rose since the last
   committed head, and that a reset was booked (item 8); if it did not rise, treat the failure
   as tampering (item 4) and do not restart; (b) in GitHub Actions, run `chain-head.yml` by hand
   (`workflow_dispatch`) with `new_series: true` and a `reason` naming the reset and who made
   it; (c) check that `heads.jsonl` on `chain-heads` gained the `new_series` line followed by a
   new head, and that the next scheduled run passes.
6. Retention and erasure: the rule, how to find due records, how to erase, limits (4.7).
7. Backups and restore (Fly Postgres). Retention of backups is UNVERIFIED (Q-6).
8. Test environment etiquette: reset, clock, shared keys, booking a window (LIM-03).
9. Known limitations carried from Phase 4 (LIM-05 to LIM-09, LIM-14, LIM-15), plus the Phase 5
   ones: closure reports cannot be submitted, so a "Final (Non-reportable incident)" is filed
   outside the system (Q-14); rows from before migration 0009 cannot be erased (4.7); erasure
   gives erasure, not confidentiality (D7).
10. Secrets inventory: names only (`DATABASE_URL`, `DATABASE_ADMIN_URL`, the three or four test
    keys, `POD_VIEWER_KEY` in GitHub), never values.

#### 7.6 Decision OD-1 (decided)

Deployment config stays on `development-deploy`; see 6.5 and 11.6. Section 14 records it as
decided.

### 8. Models: what changes

`docs/models/phase5_models.md` (Part 4, written at integration from the merged tests, the same
way as integration decision 223) replaces nothing in `phase4_models.md`. It adds the Phase 5
rows and diagrams below. Every diagram is redrawn from the code, not from this spec.

#### 8.1 Context diagram

- **Add:** "Chain-head store (GitHub `chain-heads` branch, written by a scheduled workflow)"
  as an external system, with the edge "daily export + head (viewer key)" from the system to it.
- **Change:** the reviewer–authority edge stays "submits report manually (outside system
  boundary)". Add "downloads report JSON/PDF; records submission" between the reviewer and the
  system. Remove Phase 4's "report submission not built yet" marking.
- **Add:** an operator–system edge, "reads dashboard and alerts; triggers erasure". The
  Operator role is still configuration only, and erasure uses a reviewer key (D8).
- **Unchanged:** no identity provider, webhooks only, no Evidently (Phase 4 report Q5.1).

#### 8.2 Model 1 (state machine)

- The event states stay the four contract states (D1). REPORT_READY and SUBMITTED are drawn as
  **report-record statuses** in a separate region attached to DISPOSITIONED, with these edges:
  DISPOSITIONED --reviewer GET report (D3)--> REPORT_READY; REPORT_READY --POST
  submission--> SUBMITTED; REPORT_READY or SUBMITTED --revision (new disposition row)-->
  SUPERSEDED, followed by a new REPORT_READY on the next GET; SUBMITTED of a superseded report
  stays SUBMITTED (shown as SUBMITTED_SUPERSEDED).
- CLOSED (NOT_SERIOUS) --reviewer GET report--> closure report (REPORT_READY, not submittable)
  (D2). The edge "exported" in the Phase 2 figure becomes "POST submission (records a manual
  submission)".
- REJECTED gains "submission of a non-current, already-submitted or closure report (409)".
- ERASED is drawn as a marker on any state (not a state), reached by POST erasure after
  `retain_until`.

| From | Trigger and guard | To | Ledger | Test |
| --- | --- | --- | --- | --- |
| DISPOSITIONED (serious tier in force) | Reviewer GET report, no report for the latest disposition row | report ASSEMBLED (REPORT_READY) | REPORT_ASSEMBLED | `test_report_after_a_disposition_is_200_not_501` |
| CLOSED (NOT_SERIOUS in force) | Reviewer GET report | closure report ASSEMBLED | REPORT_ASSEMBLED | `test_closure_report_for_not_serious_has_no_deadline` |
| REPORT_READY | POST submission, current report, matching hash | SUBMITTED | REPORT_SUBMITTED | `test_submission_records_reviewer_time_and_hash` |
| REPORT_READY or SUBMITTED | Disposition revision | SUPERSEDED (derived) | DISPOSITION_REVISED | `test_a_revision_creates_a_new_report_version` |
| any, after `retain_until` | POST erasure | ERASED marker | RECORD_ERASED | `test_ac18_personal_data_is_unreadable_after_erasure` |
| FLAGGED or UNDER_REVIEW | GET report | unchanged | none (409 NO_DISPOSITION) | Phase 4 `test_ac08_a_report_without_a_disposition_is_409_and_changes_nothing` |

#### 8.3 Model 2 (data schema)

- **Add** `report` and `report_submission` (3.5). Model 2's `report` had `json`, `pdf_ref
  object key`, `sha256`, `detected_at, determined_at, assembled_at` and `template_version`.
  As built: `json_bytes` and `pdf_bytes` (bytea, sealed), two hashes, `assembled_at`,
  `schema_version`, `disposition_id` and `report_version`. The three timestamps live inside the
  stored JSON, and only `assembled_at` is a column.
- **Add** `record_key` (4.3), as Model 2 drew it, except that `data_key` is not wrapped (D7). It
  is keyed by `subject_id` (the event id) and has `created_at`.
- **Add** `ledger_verification_run` (6.1) and `platform_counter` (6.5). **Add**
  `event.erased_at`.
- **Change** the `ledger_entry.payload` note from "jsonb (PII encrypted, per record key)" (the
  Phase 2 intent, which Phase 4 recorded as "plain JSONB") to "jsonb; personal values sealed
  per record key from 0009 (P2); webhook URL as url_sha256 (D16)".

#### 8.4 Model 4 (invariants)

| INV | Phase 4 status | Phase 5 target | Tests |
| --- | --- | --- | --- |
| INV-1 | Partly | **Holds:** a report exists only for an event with a disposition row, and every disposition row was written with four decided fields | `test_report_after_a_disposition_is_200_not_501`, Phase 4 AC-08 tests, `test_ac13_report_matches_the_schema` (four decided statuses) |
| INV-7 | Partly | **Holds:** chain links, plus the nightly run passes | `test_nightly_verify_records_a_valid_run`, `test_model5_ex5_nightly_verify_finds_a_payload_edit` |
| INV-9 | Partly | **Holds:** report generation and submission are REVIEWER-only | `test_ac14_the_reviewer_gets_the_report_and_others_are_denied`, `test_submission_roles` |
| INV-10 | Partly | **Holds** within Phase 5's meaning: nothing is removed at any time (no purge, D9), and erasure is refused before `retain_until` | `test_nothing_is_erased_before_retain_until`, `test_erasure_before_retain_until_is_409_and_changes_nothing` |
| INV-11 | Partly | **Holds:** destroying a record key changes no ledger hash, and verify passes | `test_ac18_chain_positions_and_hashes_are_unchanged_and_verify` |
| INV-4 | Holds | Still holds; `report`, `report_submission` and `ledger_verification_run` join the insert-only tables | `test_report_tables_are_insert_only` |
| INV-8 | Differs | Still Differs (Part 2 addendum rule 9): reading a stored report appends nothing; assembly and submission append (D19) | `test_reading_a_stored_report_appends_nothing` |
| **INV-14 (new)** | — | Stored report bytes never change: SHA-256 of the opened bytes = the `report` hashes = the `REPORT_ASSEMBLED` payload | `test_report_bytes_are_served_exactly_as_stored`, `test_tampered_report_bytes_are_found_by_the_nightly_check` |
| **INV-15 (new)** | — | At most one submission per report; only the current serious report is submittable | `test_a_report_is_submitted_at_most_once`, `test_only_the_current_report_can_be_submitted` |

INV-2, 3, 5, 6, 12 and 13 are unchanged, and their Phase 4 tests carry over.

#### 8.5 Model 3

Unchanged. Its note gains: "a submitted current report clears alerts A6 and A7, and does not
stop reminders".

#### 8.6 Model 5 (worked examples)

`tests/fixtures/phase5/model5_examples.json` (Part 4) copies the Phase 4 fixture and changes:

- **Ex-1:** adds the report step after the revision to DEATH: 200; `timestamps.detected_at` =
  d0 09:00, `determined_at` = d0 13:00, `last_revised_at` = d3 13:00, `assembled_at` = the
  request time; `reporting_window_days` 10; then a submission (201).
- **Ex-2:** the report step changes from the Phase 4 501 to 200 with `report_kind =
  "NOT_SERIOUS_CLOSURE"`, `deadline_utc` null, rationale "seasonal shift, no user impact"; then
  a submission attempt returns 409 `NOT_SUBMITTABLE` (D2).
- **Ex-4:** unchanged (409 `NO_DISPOSITION`, 403 for the viewer, not logged).
- **Ex-5:** new, in-process only (it needs the owner role to edit a row directly), and run by
  `test_model5_ex5_nightly_verify_finds_a_payload_edit`. As the owner, the test disables the
  trigger, changes one character of the payload at a middle seq k, and re-enables the trigger.
  The nightly run then records `valid = false`, `first_mismatch = "k"`, alert A1 is raised and
  one ERROR line is logged. The figure's "hash(812) no longer matches row 813 prev_hash" is
  reported by our verifier as the first mismatch at 812, because the recomputed hash check of
  812 fails before 813's link check (`app/ledger/verify.py:6-14`).
- **Ex-3:** stays covered by the AC-02 tests (Phase 4 decision).

### 9. Contract delta

#### 9.1 Gap analysis: what the contract defines versus what the service does

| Contract item | Contract says | Service at `e53d7c7` | Gap |
| --- | --- | --- | --- |
| `GET /v1/events/{id}/report` | 200 Report object; 409 no disposition; 404 | 409, 404, then **501** (`app/api/disposition_routes.py:69-76`); 401 and 403 added | Report not built; **Report object undefined in the contract** |
| Report object | Named, not defined | — | Needs a schema (C3) |
| Submission | **Not in the contract** | — | New endpoint |
| `GET /v1/ledger/export` | 200 raw entries + hashes | 200, eight fields per entry (`app/ledger/read.py:35-38`) | None |
| `GET /v1/ledger/verify` | 200 `{valid, entries_checked, first_mismatch}` | Same three fields | None |
| Ledger-verification history, chain head | **Not in the contract** | — | New endpoints (the 6.3 "ledger-verification endpoints") |
| Erasure | **Not in the contract** | — | New endpoint |
| Dashboard | **Not in the contract** | — | New endpoint (internal; may stay outside the shared contract) |
| `POST /v1/test/clock/advance` | `{seconds: <int>}` → 200 `{simulated_time}` | Plus 400 for out-of-range values (decision 226; D10 moves the bound) | Error not listed |

#### 9.2 Proposed delta

"Change" = alters text already in the shared contract. "Additive" = a new endpoint or a new
optional field or error that existing clients do not see unless they call it. **Every row needs
both teams' agreement, and changes need the instructor's sign-off; the team obtains these, not
this spec.**

| # | Kind | Item | Proposed text |
| --- | --- | --- | --- |
| Δ1 | Change | Define the Report object | Section 3.3.1 shape and field table (including D2's `report_kind`) |
| Δ2 | Change | `GET /v1/events/{event_id}/report` | Auth: reviewer role required. Query `format=json\|pdf`. Success: 200 Report object (JSON), or the PDF of the same report (`application/pdf`); the bytes are those stored at assembly. Errors: 400 MALFORMED_QUERY, 401, 403 non-reviewer, 404 unknown event, 409 NO_DISPOSITION, 410 RECORD_ERASED. Side effect: the first request after a disposition or revision assembles and stores a new report version. |
| Δ3 | Additive | `POST /v1/events/{event_id}/report/submission` | 3.7 |
| Δ4 | Additive | `GET /v1/events/{event_id}/reports`, `GET /v1/reports/{report_id}` | 3.9 |
| Δ5 | Additive | `GET /v1/ledger/verifications`, `POST /v1/ledger/verifications` | 6.1 |
| Δ6 | Additive | `POST /v1/events/{event_id}/erasure` | 4.4 |
| Δ7 | Additive | Error codes `RECORD_ERASED` (410), `NOT_SUBMITTABLE`, `REPORT_NOT_CURRENT`, `REPORT_HASH_MISMATCH`, `REPORT_ALREADY_SUBMITTED`, `RETENTION_NOT_EXPIRED`, `ALREADY_ERASED` (409), `MALFORMED_QUERY` (400) | Bodies `{error_code, message}` as every existing error |
| Δ8 | Additive | `WEBHOOK_REGISTERED` payload `url` → `url_sha256` | Observable in the export (D16) |
| Δ9 | Change | `POST /v1/test/clock/advance` errors | "400 if seconds is negative, not an integer, or would move the simulated time to or past 9999-07-01T00:00:00Z" |
| Δ10 | Additive (internal) | `GET /v1/ops/dashboard` | 7.2. Proposed **not** to enter the shared contract |
| Δ11 | Additive | `tools/verify_ledger.py --anchor` | 6.2; output unchanged without the option |

#### 9.3 Phase 4 Q6.2 items (inputs to flag, not decided here)

- **Opaque versus UUID ids:** the contract shows `event_id — string (uuid)` and
  `"flagged_event_id": "<uuid>"`, but its own examples use `"evt-7f3a"` and `"sys-42"`. The
  service issues `evt-<32 hex>` (`app/formats.py:8-10`). New ids in this spec follow the
  service (`rpt-`, `sub-`, `lvr-`). The pod's 2026-11-04 plan (Phase 4 report Q6.2) should
  settle this before Δ1 is agreed.
- **Response shapes** for clock advance, verify and the event list (envelope) are also open in
  Q6.2. The new endpoints use bare arrays for lists (`GET …/reports`, `GET
  /v1/ledger/verifications`), like `GET …/audit`.
- **Viewer writes:** the service lets VIEWER call `POST /v1/webhooks` and the test endpoints,
  while 5.1 says viewers do not write (Phase 4 report Q3.2). The new write endpoints in this
  spec (submission, erasure, manual verification) are REVIEWER-only, in line with 5.1.

#### 9.4 Repository copy of the contract

When the delta is agreed, Part 4 exports the **whole** agreed contract, including the Event and
HarmField schemas and the examples that `pod_spec.txt` lacks today (Conflict C11), as
`docs/contract/pod_contract_phase5.md`, plus `docs/contract/phase5/report.schema.json`. Until
then, the delta lives only in this spec and in `docs/contract/phase5/delta.md`, and is marked
proposed.

### 10. Tests and gates

#### 10.1 New tests, by file (written first; each MUST fail before its code exists)

**Fail-first rule (Pinned):** each owner commits a part's tests before the code that makes them
pass, and the part report shows each new test failing at that commit (by name, with the
failure line). Tests of behaviour that already exists (for example `retain_until`) are shown to
fail against a deliberately broken production line, which is then restored, as integration
decision 221 did.

New directories: `tests/reports/` (Part 1), `tests/records/` (Part 2), `tests/ledger_ops/`
(Part 3), and `tests/platform5/` (Part 4). Every name below was checked with `grep -rn "def
<name>" tests/` on `e53d7c7` and has no match.

| File | Test | Expected |
| --- | --- | --- |
| `tests/reports/test_report_schema.py` | `test_ac13_report_matches_the_schema` | The JSON validates against `docs/contract/phase5/report.schema.json` (Draft 2020-12, `jsonschema` dev dependency), for each serious tier |
| | `test_ac13_three_timestamps_are_non_null_and_distinct` | Flag at T, advance 4 h, dispose, advance 1 h, report: `detected_at < determined_at < assembled_at`, all non-null, and equal to T, T+4 h and T+5 h |
| | `test_closure_report_for_not_serious_has_no_deadline` | `report_kind` NOT_SERIOUS_CLOSURE, `deadline_utc` null, `reporting_window_days` null, rationale present |
| | `test_report_carries_the_article73_template_mapping` | For each serious tier: `article73_template.template_version` is "1.0.0"; one entry per row of the 3.3.4 table, in template order, with the exact name and status; `value` non-null only for `mapped` and `derivable`; 1.2 g candidates follow the 3.3.4 rule (parametrised over tier and confirmed harm fields) |
| | `test_closure_report_is_marked_not_for_submission` | NOT_SERIOUS: JSON `submission_marking` is "NOT FOR SUBMISSION" and the 3.3.5 "does not apply" fields are absent; `pypdf` text of every PDF page contains "NOT FOR SUBMISSION"; a serious report has `submission_marking` null and no banner |
| `tests/reports/test_report_storage.py` | `test_report_bytes_are_served_exactly_as_stored` | Response body == `open_bytes(stored)`; SHA-256 == row hash == header `X-Report-Sha256` == `ETag` |
| | `test_report_hashes_are_in_the_ledger` | `REPORT_ASSEMBLED` payload equals 3.8 exactly, hashes included |
| | `test_pdf_and_json_are_produced_once_and_never_regenerated` | With the renderers patched to count calls: two GETs of each format give one render of each |
| | `test_pdf_contains_every_required_field` | Text extracted with `pypdf` (dev dependency) contains the event id, the four harm-field names and statuses, the tier label and the three timestamps |
| | `test_renderer_is_reproducible_in_invariant_mode` | Rendering the same report dict twice gives identical bytes (test-only, 3.4 item 5) |
| | `test_report_tables_are_insert_only` | UPDATE and DELETE on `report` and `report_submission` as the app role fail |
| `tests/reports/test_report_api.py` | `test_report_after_a_disposition_is_200_not_501` | Each tier: 200 |
| | `test_ac14_the_reviewer_gets_the_report_and_others_are_denied` | Reviewer 200; viewer and `sys-42` 403; no key 401; nothing appended for the denied calls |
| | `test_report_check_order` | Parametrised over 3.6 steps 1 to 6 |
| | `test_a_revision_creates_a_new_report_version` | Version 1, then revise, then version 2; version 1 bytes unchanged and still served by `GET /v1/reports/{id}` |
| | `test_concurrent_first_requests_assemble_one_report` | 20 parallel GETs: one `report` row, one `REPORT_ASSEMBLED`, 20 identical bodies |
| | `test_report_status_is_ready_then_submitted` | `GET …/reports` status ASSEMBLED, then SUBMITTED, then SUBMITTED_SUPERSEDED after a revision |
| | `test_reading_a_stored_report_appends_nothing` | The second GET appends no ledger entry (D19) |
| `tests/reports/test_submission.py` | `test_submission_records_reviewer_time_and_hash` | 201 body; `REPORT_SUBMITTED` payload; `submitted_at` = `clock.now()` |
| | `test_only_the_current_report_can_be_submitted` | Superseded report: 409 REPORT_NOT_CURRENT; nothing written |
| | `test_a_report_is_submitted_at_most_once` | Second POST: 409 REPORT_ALREADY_SUBMITTED |
| | `test_submission_after_a_revision_is_a_follow_up` | `submission_kind` FOLLOW_UP |
| | `test_closure_report_cannot_be_submitted` | 409 NOT_SUBMITTABLE |
| | `test_submission_hash_mismatch_is_409` | 409 REPORT_HASH_MISMATCH |
| | `test_submission_report_type_is_one_of_four_template_values` | Each of "Initial", "Follow up", "Combined initial and final", "Final (Reportable incident)" is accepted and stored; "Final (Non-reportable incident)", another string, or `expected_next_report_date` with a final type gives 400 MALFORMED_BODY and writes nothing |
| | `test_submission_roles` | Viewer, `sys-42`: 403; no key: 401; when `rev-2` is seeded (D17), `rev-2` may submit a report `rev-1` assembled |
| `tests/records/test_personal_data.py` | `test_seal_and_open_round_trip` | Text and bytes |
| | `test_open_after_destroy_is_none` | |
| | `test_open_passes_pre_0009_plain_values_through` | |
| | `test_sealed_values_differ_for_the_same_text` | Random nonce |
| | `test_report_bytes_are_sealed_at_rest` | Integration (needs Part 1): the `report` row's stored JSON and PDF columns start with the sealed form, not `{` or `%PDF`, and `open_bytes` returns the served bytes |
| `tests/records/test_erasure.py` | `test_ac18_personal_data_is_unreadable_after_erasure` | 4.5 steps 1, 2, 4, 5 |
| | `test_ac18_chain_positions_and_hashes_are_unchanged_and_verify` | 4.5 step 6 (server verify) |
| | `test_ac18_standalone_verifier_agrees_after_erasure` | 4.5 step 6 (`tools/verify_ledger.py` as a process) |
| | `test_erasure_before_retain_until_is_409_and_changes_nothing` | 4.5 step 3 |
| | `test_erasure_twice_is_409` | 4.5 step 7 |
| | `test_erasure_roles` | Viewer and `sys-42`: 403; no key: 401 |
| | `test_nothing_is_erased_before_retain_until` | 5.3 step 4, plus one second before `retain_until` |
| `tests/records/test_retention.py` | `test_ac05_retrievable_at_5mo29d` | 5.3 step 2 |
| | `test_ac05_at_6mo1d_presence_or_absence_is_accepted` | 5.3 step 3 |
| `tests/records/test_clock_bound.py` | `test_clock_advance_into_the_last_half_of_9999_is_400` | 5.4; an advance to 9999-06-30T23:59:59Z still 200 and ingest works there |
| | `test_a_sample_dated_after_9999_07_01_is_400` | 5.4 |
| `tests/records/test_webhook_commitment.py` | `test_webhook_registered_carries_url_sha256_not_url` | D16 |
| `tests/ledger_ops/test_nightly.py` | `test_nightly_verify_records_a_valid_run` | One run row, valid, `head_check` FIRST_RUN then OK |
| | `test_model5_ex5_nightly_verify_finds_a_payload_edit` | 8.6 Ex-5 |
| | `test_nightly_verify_runs_once_per_utc_day` | With the wall clock patched: two passes on one day give one NIGHTLY row; the next day gives a second |
| | `test_head_regression_is_detected` | Owner deletes the last two entries (trigger disabled): `head_check` REGRESSED, alert A1 |
| | `test_tampered_report_bytes_are_found_by_the_nightly_check` | Integration-marked: owner changes one byte of a sealed `json_bytes`; the run lists it |
| `tests/ledger_ops/test_anchor.py` | `test_anchor_detects_truncation_below_the_anchor` | Exit 1, `anchor: MISMATCH seq=<s>` |
| | `test_anchor_detects_a_forged_anchored_entry` | Rewritten entry s with recomputed later hashes: verify valid, anchor MISMATCH |
| | `test_anchor_accepts_a_longer_valid_chain` | Exit 0, `anchor: OK` |
| | `test_verifier_output_without_anchor_is_unchanged` | `fixture_export.json` output byte-identical to today's |
| `tests/ledger_ops/test_projection.py` | `test_projection_check_finds_an_edited_event_state` | Owner sets `event.state` to CLOSED on a DISPOSITIONED event: one mismatch |
| | `test_projection_check_finds_an_edited_deadline` | Owner moves `deadline_utc` by one day: one mismatch |
| | `test_projection_check_is_clean_after_the_ac16_trace` | Integration-marked: zero mismatches after both AC-16 paths |
| `tests/ledger_ops/test_dashboard.py` | `test_dashboard_roles` | 7.4 |
| | `test_dashboard_shape` | Exactly the keys of 7.2 |
| | `test_alert_deadline_passed_without_submission` | A6 raised past the deadline, cleared by a submission |
| | `test_alert_thresholds` | Parametrised A1 to A9, each at its threshold and just below it |
| `tests/ledger_ops/test_health_version.py` | `test_healthz_reports_the_app_version` | `APP_VERSION=abc123` gives `"version": "abc123"`; unset gives null |
| `tests/ledger_ops/test_verification_routes.py` | `test_verification_routes_roles_and_shapes` | 6.1 and 7.4 |
| `tests/platform5/test_phase5_schema.py` | `test_phase5_tables_and_grants` | Tables, columns and grants of 0008 to 0010 (slots, 11.1) |
| `tests/platform5/test_seed_rev2.py` | `test_rev2_is_seeded_when_the_key_is_set` | D17 |
| `tests/platform5/test_ac16_in_process.py` | `test_ac16_in_process_trace_both_paths` | Integration-marked: the trace of 10.3 through the ASGI app (for the gate) |
| `tests/acceptance/test_ac05_retention.py` | `test_ac05_over_http_retrievable_then_accepted` | 5.3 over HTTP |
| `tests/acceptance/test_ac13_report.py` | `test_ac13_report_over_http_matches_the_schema` | |
| | `test_ac13_pdf_download_matches_its_hash` | |
| `tests/acceptance/test_ac16_trace.py` | `test_ac16_breach_to_submission_trace` | 10.3 path S |
| | `test_ac16_not_serious_trace_has_no_deadline` | 10.3 path N |
| `tests/acceptance/test_ac18_erasure.py` | `test_ac18_over_http_erasure_keeps_the_chain` | 4.5 over HTTP |
| `tests/acceptance/test_phase5_contract.py` | `test_contract_delta_routes_exist` | Each Δ endpoint answers 401 without a key (not 404/405) |
| `tests/test_check_acceptance.py` | `test_phase5_required_is_merged` | `required_criteria()` includes every `PHASE5_REQUIRED` id |

#### 10.2 Phase 3 and 4 tests that change (owner in brackets; the Phase 4 Addendum 9.10 method)

Each change is the smallest one that makes the test state the new rule. No assertion is
removed or loosened without a line in the part report.

- `tests/part2/test_disposition_api.py::test_a_report_after_any_disposition_is_501_until_phase_5`:
  replaced by `test_report_after_a_disposition_is_200_not_501` and removed [Part 1].
- `tests/part2/test_disposition_api.py::test_ac14_the_reviewer_succeeds_on_both_actions`: the
  report half expects 200 (line 451) [Part 1].
- `tests/part1/test_observability.py::test_a_stub_501_is_logged_at_info_and_a_500_at_error`:
  the 501 half has no route left; it is replaced by "a 410 RECORD_ERASED is logged at INFO" [Part 1].
- `tests/part1/test_events_api.py::test_disposition_and_report_check_the_key_and_role_first`:
  unchanged expectations (409 and 404 before assembly); verify only [Part 1].
- `tests/acceptance/test_ac08_report_guard.py::test_after_a_disposition_the_guard_passes_to_the_phase_5_report`:
  expects 200 and a Report [Part 1].
- `tests/acceptance/test_ac14_role_matrix.py::test_a_non_reviewer_is_denied_both_actions_and_the_reviewer_is_not`:
  line 144 expects 200 [Part 1]. `test_role_matrix` gains the Δ routes [Part 4].
- `tests/part2/test_disposition_api.py` lines 207, 211, 650 and 651 (rationale read from SQL and
  from the ledger payload): compare `personal_data.open(...)` of the stored value [Part 2].
- Tests reading `WEBHOOK_REGISTERED`'s `url` (to be listed by Part 2 from a grep of `"url"` in
  `tests/worker/test_webhooks.py`, `test_webhook_urls.py`, `test_worker_system.py`,
  `tests/api/test_cross_verify.py`, `test_ledger_system.py`, `tests/test_verify*.py`): read
  `url_sha256` [Part 2].
- `tests/api/test_platform.py` tests of decision 226: the bound moves to 9999-07-01 [Part 2].
- `tests/api/test_health.py` exact bodies (lines 24-28, 64-68) and
  `tests/part3/test_escalations_integration.py::test_healthz_shows_a_real_overdue_row`: gain
  the four new fields [base commit].
- Migration-head pins `"0007"` → `"0010"`: `tests/worker/test_schema.py:205`,
  `tests/api/test_ledger_system.py:301`, `tests/part1/test_review_schema.py:197`,
  `tests/part1/test_schema.py:126-142` (the chain list gains 0008 to 0010) [base commit].
- `tests/test_canonical.py::test_all_fourteen_entry_types_are_accepted` → 17 types (may be
  renamed; the base report lists the rename) [base commit].
- `tests/fixtures/phase4/model5_examples.json`: unchanged. The Phase 5 fixture is a new file
  (8.6), and the Phase 4 runner keeps its file until Part 4 switches `tools/run_model5.py` to
  the Phase 5 file at integration [Part 4].

#### 10.3 AC-16 scripted trace (`tools/demo_phase5.py`; Part 4)

Driven only through HTTP (BASE_URL, the reviewer, second-reviewer and viewer keys, and the
public webhook receiver), like `tools/demo_phase4.py`. It prints each step, and exits 1 at the
first failed expectation. The event id from step S2 is carried through every step, and each
step checks it.

**Path S (breach to submission):**

| Step | Call | Expected |
| --- | --- | --- |
| S1 | Reset; `POST /v1/webhooks` for both types | 204; 201 |
| S2 | `POST /v1/metrics` breach on `sys-42` | 201 with `flagged_event_id` E; one flag_notification at the receiver within the bound (**notify**) |
| S3 | `GET …/report` (reviewer) before any disposition | 409 NO_DISPOSITION; state unchanged (**no stage is skippable**) |
| S4 | `GET /v1/events/E` (viewer), then (reviewer) | Pre-filled fields (**parameterized report**); UNDER_REVIEW |
| S5 | Advance 4 h; dispose OTHER_SERIOUS (reviewer) with four fields decided | 200 DISPOSITIONED; `deadline_utc = assigned_at + 15 d` (**deadline**) |
| S6 | Advance 1 d; dispose DEATH (`rev-2` if seeded, else `rev-1`) | 200; deadline = first `assigned_at` + 10 d; revision by that reviewer |
| S7 | Advance 1 h; `GET …/report` and `?format=pdf` | 200; schema-valid; ids, tier DEATH and timestamps consistent with S2 to S6; PDF SHA-256 = `X-Report-Sha256` (**submission-ready report**) |
| S8 | `POST …/report/submission` (reviewer) with S7's id and hash | 201 INITIAL |
| S9 | `GET …/reports`, `GET …/audit`, `GET /v1/ledger/verify`, export plus `tools/verify_ledger.py` | SUBMITTED; the trail shows EVENT_FLAGGED, NOTIFICATION_*, EVENT_VIEWED, REVIEW_STARTED, DISPOSITION_RECORDED, DISPOSITION_REVISED, REPORT_ASSEMBLED, REPORT_SUBMITTED in order; both verifiers valid |

**Path N (not serious):** N1 breach (new event F); N2 reviewer view; N3 NOT_SERIOUS without a
rationale → 400 RATIONALE_REQUIRED (contract Example 3); N4 NOT_SERIOUS with a rationale → 200
CLOSED, `deadline_utc` null, **no escalation rows** (advance 16 days: no reminder for F at the
receiver); N5 `GET …/report` → 200 closure report (D2) and PDF; N6 submission → 409
NOT_SUBMITTABLE; N7 trail and verify as S9 (no REPORT_SUBMITTED).

`tests/acceptance/test_ac16_trace.py` runs both paths as two tests. The AC-16 method stays
"Manual/analysis trace review": the script's printed trace is the artifact the other team
reviews (manual check M6).

#### 10.4 Gates

- `tools/check_acceptance.py` gains `PHASE5_REQUIRED`, merged per criterion like
  `PHASE4_REQUIRED` (`required_criteria()` takes the union of three mappings). It is filled at
  integration with the merged node ids for **AC-05, AC-06 (nightly additions), AC-13, AC-14
  (report half), AC-16 (`test_ac16_in_process_trace_both_paths`) and AC-18** (AC-18 at the 2b
  merge, 1.4). Until then each tuple is empty and adds nothing (decision 151's rule).
- **AC-01 and AC-03 join the gate (Q-9, decided).** They are in neither mapping today
  (`tools/check_acceptance.py:13-91`). Part 4 adds them to `PHASE5_REQUIRED` in the base commit,
  because their tests already exist and pass on main: AC-01
  `tests.part1.test_ingestion::test_ac01_cadence_twelve_samples_no_gap`
  (`tests/part1/test_ingestion.py:338`); AC-03 `tests.part1.test_ingestion::test_drift_boundary`,
  `tests.part1.test_ingestion::test_performance_boundary` and
  `tests.part1.test_ingestion::test_contract_example_1_flags_and_detail_shows_the_breach`
  (`tests/part1/test_ingestion.py:124, 141, 75`). The HTTP runner already runs
  `tests/acceptance/test_ac01_ingest.py` and `test_ac03_flagging.py`; that is unchanged.
- The HTTP acceptance runner (CI image job) gains the five new acceptance files. Its expected
  count grows from 133 by the new cases. Part 4's report states the new count.
- CI branch conditions (`.github/workflows/ci.yml:49, 52, 74, 99, 106`) add
  `phase5-integration`. The image job adds `python tools/demo_phase5.py` after the Phase 4
  demo.
- Static checks unchanged: `ruff check`, `ruff format --check`, `mypy` (strict).
  `tools/verify_ledger.py` stays standard-library only, and a test imports it in a subprocess
  with `-I -S` to prove it.

#### 10.5 Regression carry-over (Pinned)

At integration and on every merge into main, all of these pass:

- the 133 HTTP runner cases (with the expectation changes of 10.2);
- the unflagged run `pytest --ignore=tests/acceptance` and the flagged run `pytest
  --run-integration --ignore=tests/acceptance`;
- `tools/check_acceptance.py` with `REQUIRED`, `PHASE4_REQUIRED` and `PHASE5_REQUIRED`;
- `tools/run_model5.py` (Phase 5 fixture);
- `tools/demo_phase4.py` (unchanged; it does not call the report endpoint);
- `tools/demo_phase5.py`.

#### 10.6 Manual checklists (Part 4 writes `docs/manual_checks_phase5.md`; a person performs them)

| Id | Check | Pass when |
| --- | --- | --- |
| M1 | **AC-02 real delivery**: on the deployed service, a person registers a public receiver (the Fly sink app), withholds data for 3 intervals using the test clock, and watches the receiver | A gap `flag_notification` arrives within 15 min of the 3rd missed interval; withholding 2 intervals sends nothing; screenshots and receiver log saved |
| M2 | **AC-09 screen review**: open the console reviewer panel | Exactly four options with the REQ-08b labels and day counts; NOT_SERIOUS without a rationale is refused on screen; with one, the event closes with no deadline; screenshot saved |
| M3 | **AC-13 usability review**: a person, ideally from the other team, compares a downloaded PDF and JSON (path S, and the path-N closure report for U8) with the Commission template v1.0.0 and the draft guidance (3.3.4) | Pass or fail recorded for each of U1 to U8 (3.3.4); a one-paragraph judgement on whether the provider could file an "Initial" report from it (guidance (39)) |
| M4 | **AC-07 confirmation record review** | Each of the four fields shows status, confirmer and time, and matches what the reviewer did |
| M5 | **AC-18 chain integrity post-deletion**: compare the export before and after erasure | All earlier entries identical; both verifiers valid; sealed values unreadable |
| M6 | **AC-16 trace review** by the other team on the deployed service, both paths | One event id per path, consistent across stages; report downloaded and reviewed |
| M7 | **AC-06 storage-layer permissions** | Re-run the review of `docs/ac06_storage_evidence.md` for the three new insert-only tables |

### 11. Decomposition into parts

#### 11.1 The base commit (Part 4, before any branching; Phase 4 Addendum 9.1 method)

It lands on main as ordered commits, each leaving main green, and its last commit's id is
published before anyone branches:

1. **Ledger types:** add `REPORT_ASSEMBLED`, `REPORT_SUBMITTED` and `RECORD_ERASED` to
   `ENTRY_TYPES` in `app/ledger/canonical.py` **and** in `tools/verify_ledger.py`'s own list;
   add their rows to `integration_notes.md` section 2.5; update the count test.
2. **Migration chain:** placeholders `0008_reports.py`, `0009_personal_data.py` and
   `0010_ledger_ops.py` (ids "0008" to "0010", empty upgrade and downgrade); the head pins of
   10.2; `PHASE5_TABLES = ("report", "report_submission", "record_key",
   "ledger_verification_run", "platform_counter")`, added through the `to_regclass` helper
   (`app/platform.py:34-46`) to every truncate list.
3. **Stubs and wiring** (11.3), the `/healthz` fields with fixed values (`version` from
   `APP_VERSION`, `last_verification` null, `last_verification_age_seconds` null,
   `reset_count` 0) and their test changes.
4. **Slots:** one marked `config.py` block per part; `EXPECTED_COLUMNS` and `GRANT_EXCEPTIONS`
   slots in `tests/worker/test_schema.py`; one README section per owner; one `docs/decisions.md`
   heading per block (11.4); `PHASE5_REQUIRED = {}`; the `phase5-integration` CI conditions;
   the dependencies `reportlab`, `cryptography` (runtime) and `jsonschema`, `pypdf` (dev),
   pinned to minor versions; the empty `docs/contract/phase5/` directory with the schema file
   from 3.3.1.

#### 11.2 Shared interfaces (Pinned)

| Interface | Owner | Called by |
| --- | --- | --- |
| `personal_data.seal(session, subject_id, field, text) -> str`; `seal_bytes(...) -> bytes`; `open(session, subject_id, field, stored: str \| None) -> str \| None`; `open_bytes(...) -> bytes \| None`; `is_erased(session, event_id) -> bool` | Part 2 | Part 1 (report bytes, rationale), Part 2 (dispositions), Part 3 (report check) |
| `retention.erase(session, principal, event_id) -> dict` | Part 2 | Part 2's route |
| `reports.assemble_or_get(session, principal, event_id) -> StoredReport`; `reports.submit(session, principal, event_id, body) -> dict`; `reports.listing(session, event_id) -> list[dict]` | Part 1 | Part 1's routes; Part 4's console |
| `reports.verify_stored(session) -> list[dict]` | Part 1 | Part 3's nightly job |
| `ledger_ops.nightly_due(engine) -> bool`; `ledger_ops.run_verification(engine, trigger) -> dict`; `ledger_ops.projection_check(session) -> list[dict]` | Part 3 | worker loop; Part 3's routes |
| `ops.dashboard(session) -> dict`; `ops.raise_alerts(engine) -> None` | Part 3 | Part 3's route; worker loop |
| `config.app_version() -> str \| None` | Part 3 | `/healthz`, dashboard |
| `platform.reset()` increments `platform_counter.reset_count` | Part 4 (base) | — |

#### 11.3 Stubs (so the four parts build in parallel)

| Interface | Stub behaviour until the owner lands |
| --- | --- |
| `personal_data.seal*` / `open*` | Identity: returns the input unchanged; `is_erased` returns False |
| `reports.verify_stored` | Returns `[]` |
| `ledger_ops.*` | `nightly_due` False; `run_verification` returns verify's result with `head_check` FIRST_RUN; `projection_check` `[]` |
| `ops.dashboard` | Returns the 7.2 keys with zero or null values; `raise_alerts` does nothing |
| Report route | Stays the Phase 4 guard (409, then 501) until Part 1 lands |

#### 11.4 The parts

| | Part 1: reports | Part 2: records | Part 3: ledger operations | Part 4: platform |
| --- | --- | --- | --- | --- |
| **Scope** | 3.1 to 3.9 | 4, 5 (merged twice: 2a = 5 and D10; 2b = 4 with D7, D8, D16) | 6, 7 | 8, 9, 10.3 to 10.6, 11.1, 11.6 |
| **Files owned** | `app/reports.py`, `app/report_render.py`, `app/api/report_routes.py`; the report route removed from `app/api/disposition_routes.py`; `docs/contract/phase5/report.schema.json` (content) | `app/personal_data.py`, `app/retention.py`, `app/api/erasure_routes.py`; edits to `app/dispositions.py` (seal/open only), `app/webhooks.py` (D16), `app/platform.py` (D10), `app/ingestion.py` (5.4 sample bound) | `app/ledger_ops.py`, `app/ops.py`, `app/api/ops_routes.py`; edits to `app/worker.py`, `app/api/health_routes.py`, `tools/verify_ledger.py` (`--anchor`), `.github/workflows/chain-head.yml`, `docs/operations.md` | Base commit; `tools/seed_part1.py` (rev-2), `app/static/*` (report, submission and health views), `tools/demo_phase5.py`, `tools/run_model5.py`, `tests/fixtures/phase5/`, `tests/acceptance/*` (new and changed), `tools/check_acceptance.py`, `docs/models/phase5_models.md` and diagrams, `docs/contract/*`, `docs/manual_checks_phase5.md`, README, CI (no deployment config: OD-1) |
| **Migration** | 0008 | 0009 (2b) | 0010 | none (placeholders only) |
| **Endpoints** | `GET …/report`, `POST …/report/submission`, `GET …/reports`, `GET /v1/reports/{id}` | `POST …/erasure` | `GET`/`POST /v1/ledger/verifications`, `GET /v1/ops/dashboard`, `/healthz` fields | none new |
| **Ledger types** | REPORT_ASSEMBLED, REPORT_SUBMITTED | RECORD_ERASED | none | adds all three (base) |
| **Tests** | `tests/reports/*` | `tests/records/*` | `tests/ledger_ops/*` | `tests/platform5/*`, `tests/acceptance/*` |
| **Decision block** | 254–273 | 274–293 | 294–313 | 314–333 (integration 334–353) |
| **Depends on** | Part 2b's seal (identity stub until then, and for good if erasure is cut, 1.4) | none | Part 1's `verify_stored` (stub) | all, for the trace and the gate |

**Parallelism:** after the base commit, Parts 1, 2 and 3 are independent and run in parallel
against the stubs. Part 4's console and fixture work runs in parallel, but its AC-16 trace, Model
5 Ex-1 and Ex-2 and the gate fill need Parts 1 and 3 and merge 2a; only the AC-18 entries need
2b. Pure code goes first: renderers and schema (Part 1), the clock bound and AC-05 (Part 2a),
seal/open (Part 2b), `--anchor` and the projection replay (Part 3).

**Stacking and merge order into `phase5-integration`:** **2a, 1, 3, 4, 2b** (SS's priority,
1.4). 2a (retention and the clock bound) goes first: it is small and changes only decision 226's
tests. Part 1 follows, with the identity seal stub. Part 3 follows, because its report-bytes
check needs Part 1's `verify_stored`. Part 4 merges next, runs the AC-16 trace and fills
`PHASE5_REQUIRED` for everything but AC-18. **2b (erasure) merges last**, because it changes
Phase 3 and 4 payloads (`DISPOSITION_*` rationale, `WEBHOOK_REGISTERED` url) and their tests
(10.2), and it is the first item to drop; Part 4 then adds the AC-18 entries. Part 2's two
merges come from two branches cut from the base commit, `phase5-part2a` and `phase5-part2b`.
Every part branches from the base commit (no stacking). A part that needs another's real code
marks those tests `@pytest.mark.integration`.

**Expected-red CI per branch** (the flagged run and gate run only on `phase5-integration`,
`main` and PRs into main, so these are red only there until the named merge):

| Branch | Red until | Tests |
| --- | --- | --- |
| Part 2b | Part 1 merged | `test_report_bytes_are_sealed_at_rest` (integration) |
| Part 3 | Part 1 merged | `test_tampered_report_bytes_are_found_by_the_nightly_check`, `test_projection_check_is_clean_after_the_ac16_trace` (integration) |
| Part 4 | Parts 1, 2a and 3 merged | `test_ac16_in_process_trace_both_paths` (integration); HTTP runner AC-13 and AC-16 files; `tools/demo_phase5.py` in the image job |
| Part 4 | Part 2b merged | Part 4 adds the HTTP runner AC-18 file (`tests/acceptance/test_ac18_erasure.py`) and the AC-18 gate entries only after 2b merges, so no branch is red for them; neither is written if erasure is cut (1.4) |
| any | Part 1 merged | Phase 4 runner case `test_after_a_disposition_the_guard_passes_to_the_phase_5_report` (changed by Part 1) |

#### 11.5 Second reviewer (D17, decided)

`tools/seed_part1.py` seeds a fourth key, `REVIEWER_2_KEY` → (REVIEWER, `rev-2`), **only when
the variable is set**, so the Phase 3 three-key tests keep passing without it. The test keys are
published test keys (LIM-03). Adding the value to Fly is a deployment step that needs the team
(11.6).

#### 11.6 Integration and deployment steps (after the parts; the team runs them)

1. Merge in the order of 11.4 into `phase5-integration`; after each merge, run the unflagged and
   flagged suites, the gate and the HTTP runner; fill `PHASE5_REQUIRED`; write
   `docs/models/phase5_models.md` from the merged tests.
2. An independent audit of each part against this spec, and a black-box probe (the Phase 4
   Integration Guide method).
3. With the team's approval, merge into main.
4. **Deploy (team only; OD-1 decided):** deployment config stays on `development-deploy` and
   is not added to main or to the base commit. Back up the Fly database (an image at 0007
   cannot start on a 0010 database, as in Phase 4 rule 7). The deployer **merges main into
   `development-deploy`** and deploys from there with `fly deploy --build-arg GIT_SHA=<M>`,
   where `<M>` is the **main commit being deployed**, written out explicitly, **not** `git
   rev-parse HEAD` of the deploy branch (its head is the merge commit). The release step
   migrates; then `POST /v1/test/reset` so that no pre-0009 plain rows remain (4.7). Set
   `REVIEWER_2_KEY` as a Fly secret (approval needed); add `POD_VIEWER_KEY` to GitHub secrets
   and enable `chain-head.yml` (approval needed). The Fly health-check path (`/livez` on
   `development-deploy`, or `/healthz`) is set there (7.5 item 2).
5. Check that `/healthz` `version` equals `<M>`, the main commit of step 4; run `tools/demo_phase5.py` against Fly
   with the public receiver; run the manual checks M1 to M7; hand the other team the trace
   (M6) and the refreshed `tools/verify_ledger.py` (three new types, `--anchor`).

### 12. Decision records

When approved, these are copied into `docs/decisions.md` under a new heading "Phase 5
specification decisions (numbers 234 to 253)", Dk being decision 233 + k. This continues
after integration decision 229 and the reserved block 214 to 233. Part blocks follow in 11.4.
"Decided" means SS chose the option in the project thread on 2 November 2026. **Every other
record (D3 to D10, D13 to D16, D19, D20) was approved as proposed by SS on 2 November 2026**,
D7 and D15 with the conditions written in them.

**D1 (234). Report and submission status live on the report record; Event.state keeps four values.**
*Options:* A, statuses on the report (derived); B, add REPORT_READY and SUBMITTED to Event.state.
*Choice:* **A (decided).** *Reason:* the contract fixes the state enum and wins over Model 1;
an additive listing is easier to agree than an enum change the other team already tests.
*Consequence:* after a submission, `event.state` stays **DISPOSITIONED**. "Submitted" is
exposed by `GET /v1/events/{event_id}/reports` (`status = "SUBMITTED"` and the `submission`
object) and by the `REPORT_SUBMITTED` audit entry. The Event object does not change. Model 1
draws the two statuses in a report region (8.2).

**D2 (235). A NOT_SERIOUS disposition in force gets a closure report that cannot be submitted.**
*Options:* A, closure report; B, 409 NOT_REPORTABLE. *Check required by SS:* AC-16, REQ-12 and the
contract's report endpoint were re-read (0.2, 0.3). None of them limits reports to serious tiers:
REQ-12 says "when severity is confirmed"; AC-16 requires only that the not-serious path
"terminate without a deadline"; the contract's only report error besides 404 is "409 if no
disposition recorded yet". *Choice:* **A (decided).** *Reason:* the Phase 5 text says the
other team will "trace the not-serious path and download the report". *Consequence:* the
closure report carries `report_kind = "NOT_SERIOUS_CLOSURE"`, the JSON field `"submission_marking":
"NOT FOR SUBMISSION"`, and a visible "NOT FOR SUBMISSION" banner at the top of every PDF page
(3.3.5); a submission is 409 NOT_SUBMITTABLE. Model 5 Ex-2 and Model 1 differ (Conflict C2).

**D3 (236). Report generation is the reviewer's GET; assembly is lazy, once per disposition row.**
*Options:* A, assemble on the first GET after a disposition or revision; B, assemble inside the
disposition transaction; C, a new POST to generate. *Choice:* A. *Reason:* the contract has only
the GET; B makes `assembled_at` equal the determination time (AC-13 "distinct") and couples to
Phase 4 code; C is a contract change with no gain. *Consequence:* the first GET writes; later GETs
read only.

**D4 (237). Report bytes are stored in PostgreSQL, sealed, with plaintext hashes on the row and in the ledger.**
*Options:* A, database bytea; B, object storage (Model 2's `pdf_ref`). *Choice:* A. *Reason:* no
object store in the Fly deployment, and one backup, reset and erasure path. *Consequence:*
Model 2 changes (8.3); database size grows by a few kB per report.

**D5 (238). PDF library: reportlab, in invariant mode (Open; recommendation).**
*Options:* reportlab (BSD), fpdf2 (LGPL-3.0), WeasyPrint (needs system libraries). *Choice:*
reportlab. *Reason:* pure Python wheels, a permissive licence, and a documented invariant
mode for reproducible output. *Consequence:* one runtime dependency; the stored bytes remain the
record regardless (3.4).

**D6 (239). Submission: REVIEWER only, hash-checked, once per report, follow-ups allowed after a revision.**
*Choice:* 3.7. *Reason:* "mark a report as submitted" (6.3); Art. 73(5) initial and complete
reports (summary, UNVERIFIED); the contract allows revisions at any time. *Consequence:* new
endpoint (Δ3) and four 409 codes.

**D7 (240). Erasure by per-record keys (crypto-shredding) for append-only stores; null for mutable columns.**
*Options:* A, commitments plus an erasable store; B, per-record keys; C, payload digests
(rejected). *Choice:* **B (approved by SS)**, with two statements SS required: **the keys are
unwrapped and stored in the same database, so the design provides erasure, not
confidentiality**; and **only rows written from migration 0009 on are erasable**. *Reason:*
section 4.2 table: no updates to append-only tables, ledger hashes unchanged (INV-11 as
written), one erasure point covers the report bytes, and the verifier is unchanged. Keys are
not wrapped, so no secret is needed. *Consequence:* `cryptography` dependency; readers of
rationale change; pre-0009 rows are not erasable (4.7); merged last and first to drop (1.4).

**D8 (241). Erasure is reviewer-triggered after `retain_until`, never automatic.**
*Choice:* 4.4. *Reason:* REQ-16 says "support"; no legal-hold rule exists. *Consequence:* alert
A9 lists due records; an operator acts.

**D9 (242). No purge in Phase 5.** *Reason:* AC-05 accepts presence at 6mo1d; a purge needs
referential rules that no artifact gives. *Consequence:* storage grows; the operations note says
so.

**D10 (243). The clock and sample-time bound is 9999-07-01T00:00:00Z.** *Reason:* the last
instant whose `retain_until` is representable (5.4); closes LIM-12. *Consequence:* changes
decision 226's bound and its tests.

**D11 (244). The nightly verification runs in the worker; the dashboard is an API endpoint and a console view.**
*Choice:* **A (decided).** *Consequence:* no new process; results go to `ledger_verification_run`,
not the ledger (6.1).

**D12 (245). External chain head: a scheduled GitHub Actions workflow commits each head to the `chain-heads` branch.**
*Choice:* **A (decided)**, with SS's condition: **each run compares the new head with the
previous committed head and fails if the chain is shorter or the old head is missing** (6.2).
*Consequence:* GitHub secret `POD_VIEWER_KEY` (approval needed); a legitimate test reset also
fails the run, and a person restarts the series by hand (6.2).

**D13 (246). Event-table edits are detected by a nightly projection check against the ledger.**
*Options:* A, projection check; B, add more fields to ledger payloads; C, document only.
*Choice:* A, plus a documented list of uncovered columns. *Reason:* no change to pinned
payloads. *Consequence:* `detected_at`, `retain_until`, the evidence bundle and harm evidence
text stay uncovered (Q-7: SS decided not to add them).

**D14 (247). Alerts are delivered through the dashboard and logs, not a new webhook type.** *Reason:*
the contract fixes `event_types`. *Consequence:* no alert webhook (Q-8: SS decided no).

**D15 (248). `/healthz` reports `version` from a build-time `APP_VERSION`; deployment config stays on `development-deploy`.**
*Choice:* approved, with OD-1 decided by SS: `fly.toml` and `/livez` stay on
`development-deploy` and are not added to main or the base commit. The Dockerfile `ARG GIT_SHA`
and the `/healthz` `version` field stay on main (Part 3). *Consequence:* the deployer merges main
into `development-deploy`, deploys from there and passes `--build-arg GIT_SHA=<the main commit
being deployed>` explicitly; 11.6 step 5 checks `version` equals that commit (6.5, 7.5, 11.6).

**D16 (249). `WEBHOOK_REGISTERED` carries `url_sha256` instead of `url` (LIM-13).**
*Consequence:* changes a Phase 3 payload and its tests; the export no longer shows URLs.

**D17 (250). A second reviewer `rev-2` is seeded from `REVIEWER_2_KEY` when set.** *Choice:*
**A (decided).** *Consequence:* **setting `REVIEWER_2_KEY` as a Fly secret is a deployer step that
needs the team's approval** (11.6 step 4).

**D18 (251). The Article 73 field mapping comes from the Commission's reporting template, newest version.**
*Choice:* **C (decided):** SS uploaded the template and the draft guidance on 2 November 2026.
The newest version found is "Reporting Template Version: 1.0.0" (SB-10407), a draft of
26 September 2025; a later final version is open question Q-13. *Consequence:* section 3.3.4
maps every template field with a status, the report carries the mapping in
`article73_template`, submission records the template's 1.2 e type, and manual check M3 uses
the guidance-based checks U1 to U8. Template-versus-requirements conflicts are C17 to C21.

**D19 (252). Reading a stored report appends nothing; assembly, submission and erasure append.**
*Reason:* Part 2 addendum rule 9 (only detail and audit reads are logged) is not changed by this
phase. *Consequence:* Conflict C4 with REQ-14's "views", for the pod to confirm.

**D20 (253). Actor ids are pseudonymous and are not erased.** *Reason:* `actor_id` is a canonical
hash field, and no mapping to a person is stored. *Consequence:* assumption A-3.

### 13. Conflicts

"Document says" quotes the authoritative or higher-precedence text. None of these is resolved by
this document. The proposed resolution waits for the team (and, where marked, the pod).

| # | Document says | Repo or other artifact does | Proposed resolution |
| --- | --- | --- | --- |
| C1 | Contract EVENT OBJECT: `state — "FLAGGED" \| "UNDER_REVIEW" \| "DISPOSITIONED" \| "CLOSED"` | Model 1 (Phase 2 figure) has REPORT_READY and SUBMITTED states; Phase 4 report Q5.1 lists them as "not implemented (Phase 5)" | D1 (decided): keep four states; statuses on the report record; Model 1 redrawn |
| C2 | Phase 5 description: "They also trace the not-serious path and download the report" | Model 5 Ex-2: "Event CLOSED_NOT_SERIOUS; deadline_utc NULL; no escalation rows; no report"; Model 1 has no report edge from CLOSED_NOT_SERIOUS | D2 (decided): closure report marked NOT FOR SUBMISSION; Model 5 Ex-2 and Model 1 updated (8.2, 8.6) |
| C3 | Contract: "Success: 200, Report object" | The contract never defines the Report object; the team brief asks for the shape "taken from the Report schema in Contracts_Schemas.docx" | Propose Δ1 (3.3.1) for pod agreement |
| C4 | REQ-14: "When any user views or changes an event record, the system shall log that user's identity and the timestamp of the action" | Part 2 addendum rule 9: only the detail and audit reads are logged (INV-8 "Differs", `docs/models/phase4_models.md`) | D19 keeps rule 9 for report reads; pod to confirm whether a report download is a "view" |
| C5 | Contract report errors: "409 if no disposition recorded yet …, 404 unknown event" | The service also returns 401, 403 (Phase 4), and Phase 5 adds 400 MALFORMED_QUERY and 410 RECORD_ERASED | Δ2 lists them; additive |
| C6 | Phase 5 description: the contract "gains the report, submission, and ledger-verification endpoints" | The contract already has `GET …/report` and `GET /v1/ledger/verify`; it lacks submission | Δ2 to Δ5: define the report, add submission and verification history |
| C7 | Contract 5.1 Auth: "viewer can read events/audit/ledger but not write" | Service lets VIEWER register webhooks and call test endpoints (Phase 4 report Q3.2) | Not changed here; flagged for the pod (9.3). New Phase 5 writes are REVIEWER-only |
| C8 | REQ-10: deadline "from the assignment timestamp" | Art. 73(2) (verbatim, secondary source): "not later than 15 days after the provider or, where applicable, the deployer, becomes aware of the serious incident" | REQ-10 not reinterpreted; the report shows `detected_at`, `determined_at` and the template 1.2 d derivation (3.3.4, C20); guidance (38) says the same; pod to confirm (Phase 4 report Q7.3 "New: deadline anchor") |
| C9 | Contract disposition success: "updated Event with computed deadline_utc (absent for NOT_SERIOUS)" | Event schema: `deadline_utc — string or null`; service returns null (Phase 4 report Q3.1) | Unchanged; the closure report also uses null |
| C10 | Contract: `event_id — string (uuid)`; `"flagged_event_id": "<uuid>"` | Service ids are `evt-<32 hex>` (`app/formats.py:8-10`); the contract's own examples use `"evt-7f3a"` | Flag to the pod with Q6.2 (9.3); new ids follow the service |
| C11 | `Contracts Schemas.docx` includes the Data Schema (Event, HarmField) and Examples 1 to 3 | `pod_spec.txt` (the repo copy, cited as the contract in Phase 4 report Q9 [3]) has only the endpoint list | Export the full agreed contract at Phase 5 (9.4) |
| C12 | Contract clock advance: `{ seconds: <int> }` → 200, no error listed | Service returns 400 for out-of-range values (decision 226); D10 moves the bound | Δ9 |
| C13 | Model 4 INV-8: "Every read or write … produces exactly one ledger entry" | Phase 4: a reviewer's first view appends two entries; reports add assembly and submission entries; report reads append none | Stays "Differs" (8.4) |
| C14 | Model 2: `report.pdf_ref object key`; ledger `payload jsonb (PII encrypted, per record key)` | Phase 4: payload plain JSONB; this spec stores PDF bytes in the database | Model 2 updated to the as-built design (8.3); D4, D7 |
| C15 | Contract has no submission endpoint; Model 1: "SUBMITTED: reviewer exports manually (out of scope: auto-transmission)" | Phase 5 description: "allow reviewers to mark a report as submitted" | Δ3: recording only, no transmission; consistent with Model 1's out-of-scope note |
| C16 | AC-16 lists "notify" and "parameterized report" as stages | The Phase 4 demo checks notification at the receiver; the parameterized report is the pre-filled Event, not a separate document | **Decided (SS, Q-2):** the Event detail with pre-filled fields is the AC-16 "parameterized report" (10.3 S4) |
| C17 | REQ-08b: "Critical infrastructure disruption / widespread infringement (2-day deadline)"; contract tier `"CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD"` | Template 1.2 g "Classification of serious incident" has no widespread-infringement option; guidance (32) says a widespread infringement "must qualify as a serious incident" | 1.2 g candidates for this tier are the two critical-infrastructure options plus the fundamental-rights option when that field is CONFIRMED (3.3.4); team to confirm |
| C18 | REQ-07: one field "property/environmental damage"; contract HarmField `property_environmental_damage` | Template 1.2 g has two options, "Harm to property" and "Harm to environment" | Both are offered as candidates and the reviewer picks (3.3.4); no new harm field proposed; team to confirm |
| C19 | REQ-07: "health/safety harm", "critical-infrastructure disruption"; REQ-08b: "Death" is a tier | Template 1.2 g splits "Death" from "Harm to a person’s health", and "Disruption of the management of critical infrastructure" from "Disruption of the operation of critical infrastructure" | Candidates follow the tier and the confirmed fields (3.3.4); the reviewer picks the exact option |
| C20 | REQ-10: deadline computed "from the assignment timestamp" | Template 1.2 d "Manufacturer awareness date of reportability"; guidance (38): "not later than 15 days after the provider becomes aware of the serious incident" | 1.2 d is derived from the first serious-tier `assigned_at` and confirmed by the reviewer (3.3.4); REQ-10 unchanged; joins C8 for the pod |
| C21 | D2 (SS): every NOT_SERIOUS closure report is "NOT FOR SUBMISSION" (3.3.5); REQ-08b: "Assessed — not a serious incident" closes the event | Template 1.2 e offers "Final (Non-reportable incident)" and 4.2 b "Fill out rationale for why this is considered not reportable", meant for an incident already reported and then found not reportable | **Decided (SS, Q-14):** submission of closure reports stays refused (3.7); a known limitation (3.3.5, 16) |

### 14. Open questions

SS answered Q-2, Q-3, Q-7, Q-8, Q-9, Q-14 and OD-1 on 2 November 2026 (marked below; Q-1 was
resolved by the template upload). **Q-4, Q-5, Q-10 and the other pod-level questions stay open**
for the team to confirm with the other team. Q-12 is the team's own check, before the AC-13
usability review.

| # | Question | Needed by |
| --- | --- | --- |
| Q-1 | The Commission template's version, date and field list (D18; 3.3.4). **Resolved 2 November 2026:** template v1.0.0 (SB-10407), draft of 26 September 2025, read in full. | — |
| Q-2 | Is the Event detail with pre-filled fields the AC-16 "parameterized report" (C16)? **Decided (SS): yes.** | — |
| Q-3 | On the real clock, may `determined_at` and `assembled_at` share a second (AC-13 "distinct where applicable")? **Decided (SS):** distinct at microsecond resolution is enough; assembly time is read after the disposition commit and report timestamps keep their fractions (3.3.3). | — |
| Q-4 | Is a report download a "view" under REQ-14 (C4)? | Pod |
| Q-5 | The deadline anchor of Art. 73(2) versus REQ-10 (C8) | Pod, before the M3 review |
| Q-6 | Fly Postgres backup retention (4.7) | Operations note |
| Q-7 | Should Phase 5 add `detected_at`, `retain_until` or `evidence_sha256` to the `EVENT_FLAGGED` payload to close the remaining gaps of 6.3? **Decided (SS): no.** | — |
| Q-8 | Should alerts reach a webhook (needs a contract change) (D14)? **Decided (SS): no.** | — |
| Q-9 | AC-01 and AC-03 are in neither `REQUIRED` nor `PHASE4_REQUIRED` (`tools/check_acceptance.py:13-91`). Add them to the gate? **Decided (SS): yes** (10.4). | — |
| Q-10 | Opaque versus UUID ids and list envelopes (Phase 4 Q6.2; C10) | Pod, 2026-11-04 plan |
| Q-11 | The Phase 2 plan's Appendix B (0.5) | Team |
| Q-12 | The official text of Art. 73(2) to (6) and Art. 3(49), checked against the Official Journal; today they come from the draft guidance and secondary sources (0.5). **Ours (SS):** the team does it. | The team, before the AC-13 usability review (M3) |
| Q-13 | Has the Commission adopted a final version of the reporting template or the Art. 73 guidance after the consultation closed on 7 November 2025? None is assumed; if one exists, 3.3.4 is redone against it (D18) | Part 1 schema freeze |
| Q-14 | When a submitted serious report's event is revised to NOT_SERIOUS, should the closure report be submittable as the template's "Final (Non-reportable incident)" (C21)? **Decided (SS): no; keep refusing, listed as a known limitation** (3.3.5). | — |
| OD-1 | Bring `fly.toml` and `/livez` into main, or point the Fly health check at `/healthz` (6.5)? **Decided (SS):** they stay on `development-deploy`; release process in 11.6 and 7.5. | — |

### 15. Assumptions to confirm

- **A-1:** four parts and owners, as in Phase 4.
- **A-2:** the deployed database holds only test data and can be reset at the Phase 5 deploy
  (4.7, 11.6).
- **A-3:** actor ids stay pseudonymous; no human names are ever seeded (D20).
- **A-4:** metric values and evidence bundles hold no personal data (P10).
- **A-5:** the repository may hold a `chain-heads` branch written by CI, and its history is
  public to whoever can read the repository.
- **A-6:** reportlab, cryptography, jsonschema and pypdf are acceptable dependencies (licences
  BSD, Apache-2.0/BSD, MIT, BSD).
- **A-7:** `NIGHTLY_VERIFY_HOUR_UTC` = 2 and the anchor cron at 03:17 UTC are acceptable.
- **A-8:** the operator uses a reviewer key for erasure; no admin role is added.

### 16. Risks and unknowns

| Uncertainty (6.3) | Check that reduces it | When |
| --- | --- | --- |
| Is the report usable as a real Art. 73 submission? | Template mapping (3.3.4, done against v1.0.0); `test_report_carries_the_article73_template_mapping`; M3 checks U1 to U8; the other team's review of the downloaded report (M6) | Mapping before Part 1 freezes the schema; M3 and M6 after deployment |
| Can personal data be made unreadable without breaking the chain? | AC-18 tests (4.5): export before and after erasure, byte-identical earlier entries, both verifiers valid; M5 | Part 2 (fail-first), integration, and on Fly |
| Does the whole chain hold together end to end? | AC-16 trace, both paths (10.3), in CI and on Fly; projection check clean after the trace; nightly run and anchor clean the next day | Integration, then deployment day + 1 |

**What we cannot do (template v1.0.0, 3.3.4).** The system cannot fill these, so the provider's
organisation or the reviewer must, and the report says so field by field:

- **Provider-organisation data, not in our system:** 1.1 a "Name of receiving market
  surveillance authority"; all of 1.3.1 to 1.3.5 (submitter, provider, authorised
  representative and other submitter contact details, and other incident obligations); all of
  §2 (2.1 a "EU Database registration id", 2.2 a to h system name, description, model and version
  fields); 3.2 b "Operator of system at the time of the incident"; 3.2 c "Remedial actions taken
  subsequent to the incident"; 3.3 "Initial reporter" contact details; 4.1 B, 4.1 c, 4.2 a and
  4.2 c; and the declaration and its "Date". Contact details are personal data the system
  deliberately does not hold (D20).
- **Reviewer input, recorded but not decided by the system:** 1.1 b, 1.2 b end date, 1.2 e,
  1.2 f, the final choice in 1.2 g, 3.1 a (3) "likely causality", 3.2 a "Estimated number of
  users affected", and 4.1 a.
- **Transmission.** The system records a submission; it does not send anything to an authority
  (C15), and it does not decide which Member State's authority is competent (guidance (37)).
- **The 2.2 a "System name"** is not the `system_id`; the report prints `system_id` as our
  identifier only.
- **Known limitation (Q-14, decided by SS):** closure reports cannot be submitted, so the
  template's "Final (Non-reportable incident)" cannot be recorded in the system, even for an
  event whose serious report was already submitted (C21).

| Risk | Check that reduces it | When |
| --- | --- | --- |
| R-6: the system does not know a monitored system's Annex III category or whether it is a medical-device component, so it cannot apply the sectoral limits of guidance (40) and (56) to (61) (for example reporting only fundamental-rights cases) | M3 check U7; the reviewer decides before submitting | M3 |
| R-7: the template and guidance are drafts (Q-13); a final version may rename or add fields | `article73_template.template_version` in every report; 3.3.4 redone if a final version appears | Before the Part 1 schema freeze, and at the demonstration |

Other risks: a six-month clock advance on the shared environment disturbs other
users (LIM-03, booking rule); the anchor workflow fails after every legitimate test reset until a
person restarts the series (D12); `cryptography` wheels must build in the Docker image (Part 2
checks in CI's image job).

### Appendix D. Changes to the Phase 4 specification and earlier rules

| Earlier rule | Phase 5 | Why |
| --- | --- | --- |
| Phase 4 spec 2.8: report 501 after a disposition (D1 of Phase 4) | 200 Report, assembled once per disposition row | REQ-12, AC-13, AC-14 |
| Phase 3 `WEBHOOK_REGISTERED` payload `url` | `url_sha256` | LIM-13 (D16) |
| `DISPOSITION_*` payload `rationale` plain | Sealed string from 0009 | REQ-16 (D7) |
| `disposition.rationale` plain | Sealed string from 0009 | REQ-16 (D7) |
| Decision 226 bound `datetime.max` | 9999-07-01 | LIM-12 (D10) |
| `/healthz` exactly four fields | Eight fields | LIM-02 (D15) |
| Phase 4: deployment config only on `development-deploy` (decision 229); deployed commit not readable (LIM-02) | Config stays there (not added to main or the base commit); main is merged into `development-deploy`, `GIT_SHA` = the main commit being deployed, passed explicitly; step 5 checks `/healthz` `version` against it | OD-1 (D15) |
| `PHASE4_REQUIRED` without AC-01 and AC-03 | `PHASE5_REQUIRED` adds them | Q-9 (10.4) |
| `ENTRY_TYPES` 14 | 17 | 3.8, 4.4 |
| Migration head 0007 | 0010 | 3.5, 4.3, 6.1 |
