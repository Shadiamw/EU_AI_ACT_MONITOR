# Phase 5 Model 5 fixtures

`model5_examples.json` is the Phase 4 file (`tests/fixtures/phase4/`, unchanged) with the
changes of Phase 5 spec 8.6. `tools/run_model5.py` runs it over HTTP exactly as before; the
step format, the variables and the Ex-4 sources are those of the
[Phase 4 README](../phase4/README.md), plus one `save` form:

- `"@header:<Name>"` saves a response header, e.g. `"@header:X-Report-Sha256"` for the
  report's JSON hash, which the submission step sends back as `json_sha256`.

An example with an `in_process` key has no HTTP steps: the runner prints `skipping <id>` and
the named pytest test runs it.

## What changed from Phase 4, and where each expected value comes from

**Ex-1, serious incident revised upward.** Two steps are added at +3d1h, after the revision to
DEATH (+3d) and before the reminders:

| Step | Expected | Source |
| --- | --- | --- |
| report (reviewer) | 200 `SERIOUS_INCIDENT`, version 1, schema `pod-h-report-1`; `determination.tier` DEATH, `deadline_utc` the revised deadline; `timestamps.detected_at` d0, `determined_at` the first `assigned_at` (d0 + 4 h), `last_revised_at` d0 + 3 d, `assembled_at` the request time (d0 + 3 d 1 h, within 60 s); `regulation.reporting_window_days` 10; `submission_marking` null; one `REPORT_ASSEMBLED` by rev-1 whose `json_sha256` is the `X-Report-Sha256` header | Phase 5 spec 8.6 Ex-1; 3.3.1 field table; 3.8 payload |
| submission (reviewer) | 201 `INITIAL`, `report_type` "Initial", `submitted_by` rev-1; one `REPORT_SUBMITTED` with the same report id and hash | 8.6 Ex-1 "then a submission (201)"; 3.7 |

The reminder steps after them are unchanged: a submission does not stop reminders (8.5).

**Ex-2, assessed not serious.** The report step changes from the Phase 4 501 to 200 with
`report_kind` `NOT_SERIOUS_CLOSURE`, `determination.deadline_utc` null, the rationale "seasonal
shift, no user impact", `reporting_window_days` null and `submission_marking` "NOT FOR
SUBMISSION", and one `REPORT_ASSEMBLED` (D2; 8.6 Ex-2; 3.3.5). A submission of that report
follows: 409 `{"error_code": "NOT_SUBMITTABLE", "message": "a not-serious closure report cannot
be submitted"}` and nothing appended (D2; 3.7). Every other step is the Phase 4 one, including
"no reminders ever" at +16 d.

**Ex-4, unauthorized and premature advance.** Unchanged (8.6).

**Ex-5, nightly verifier finds a payload edit.** In-process only, because it edits a ledger
row as the owner role: `tests/ledger_ops/test_nightly.py::test_model5_ex5_nightly_verify_finds_a_payload_edit`
(Part 3; 8.6 Ex-5).

**Ex-3** stays covered by the AC-02 tests (Phase 4 decision).
