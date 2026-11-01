# Phase 4 Model 5 fixtures

`model5_examples.json` holds the shared HTTP examples Ex-1, Ex-2 and Ex-4 of the Phase 4
Implementation Specification, section 4.7. It is data only. `tools/run_model5.py` runs it over
HTTP against any `BASE_URL` (Part 4 decision 144):

```bash
BASE_URL=http://127.0.0.1:8000 \
WEBHOOK_SINK_URL=http://sink:8765/hook/model5 \
WEBHOOK_SINK_READ_URL=http://127.0.0.1:8765/hook/model5 \
python tools/run_model5.py              # all examples; --example ex1 runs one
```

The run prints `ok <id> step <n>: <call>` for each step, then `<id> passed` or
`<id> FAILED at step <n>: <what differed>`, and exits 1 if any example failed. The first
mismatch ends that example. `tests/acceptance/test_model5.py` runs the same examples inside
the HTTP acceptance runner, one test per example.

| Variable | Role |
| --- | --- |
| `BASE_URL` | Service under test (needs `ENVIRONMENT=test` for reset and the clock) |
| `INGEST_KEY` / `INGEST_KEY_SYS_42` | System key for `sys-42` |
| `REVIEWER_KEY` | Reviewer (rev-1) |
| `VIEWER_KEY` | Viewer (viewer-1, Model 5's analyst1) |
| `WEBHOOK_SINK_URL` | Where the **service** posts; registered as the webhook URL. Required in practice: the flag and reminder steps read it back |
| `WEBHOOK_SINK_READ_URL` | Where the **runner** reads the same channel (defaults to `WEBHOOK_SINK_URL`) |

## Step format

Each example starts with `POST /v1/test/reset`, and d0 is the simulated time right after it.

| Key | Meaning |
| --- | --- |
| `at` | Offset from d0, such as `+0s`, `+4h`, `+3d`, `+5d4h`. Offsets never go back; the runner advances the test clock by the difference before the step |
| `as` | `system`, `reviewer` or `viewer`: whose key is sent |
| `call` | `METHOD /path`. Omitted for a sink-only step |
| `body` | JSON request body |
| `expect` | Checks on the response (below) |
| `sink` | Checks on what the receiver holds (below) |
| `note` | For people; ignored |

`${name}` anywhere in `call`, `body`, `expect` or `sink` is replaced by a saved value.
`${WEBHOOK_SINK_URL}`, `${NOW}` (the simulated time at the step) and `${T0}` (d0) are always set.

`expect` keys:

- `status`: the HTTP status.
- `body`: the whole JSON body, exactly (used for the error bodies).
- `eq`: `{"$.path": value}` equality; paths walk objects and list indexes (`$.revisions.0.new_tier`).
- `len`: `{"$.path": n}` list length.
- `relative`: `{"$.path": "<base> + <duration>[ ~<tolerance>]"}`, where the base is a path in the
  same body or `${saved}`. Exact to the microsecond unless a tolerance is given.
- `save`: `{"name": "$.path"}`, or `"@NOW"` for the step's simulated time.
- `audit_delta`: the ledger entries this call appended about the event (export before and after,
  filtered by `subject_id`; `subject` overrides the default `${event}`), exactly and in order:
  `entry_type`, optionally `actor_id`, a `payload` subset and `save`. `[]` means nothing appended.

`sink` keys: `notification_type`, `event`, then `count` (flags) or `pcts` (the reminders'
`escalation_pct` values in arrival order, exactly), and `eq` on every matching body. The runner
waits up to 15 s for the expected number, then compares. Delivery is at least once (spec 3.4),
so a redelivered `notification_id` counts once.

## Where each expected value comes from

Not from the code. Seed mapping (spec 4.7, "Model 5 is context; the seed is the
specification"): loan-scorer and resume-ranker become `sys-42`; analyst1 becomes viewer-1; the
values 0.61 and 0.34 are kept.

**Ex-1, serious incident revised upward** (spec 4.7 Ex-1 row, unless stated):

| Step | Expected | Source |
| --- | --- | --- |
| webhook | 201 for `["flag_notification","reminder"]` | contract `POST /v1/webhooks`; spec 12 step 2 |
| metrics | accuracy 0.61, `performance`, 201 with `flagged_event_id` | spec 4.7 (0.61; accuracy lt 0.70 is the seed's thr-2); contract Example 1 |
| sink | one `flag_notification` for the event, `type` `threshold_breach`, `system_id` sys-42 | spec 4.7 "flag webhook received"; spec 12 step 3 |
| viewer view | FLAGGED; all four `PREFILLED_PENDING_CONFIRMATION`; rights evidence `accuracy = 0.61 is below the limit 0.70 (threshold thr-2) at <d0>. ` + the seed text; the other three null; `disposition` and `deadline_utc` null, `revisions` empty | spec 1.3 (evidence format and its worked example); spec 4.4 seed (the harm_context text); spec 12 step 4; contract event detail |
| reviewer view | UNDER_REVIEW | spec 4.7 "reviewer opens" (D3); spec 12 step 5 |
| +4h OTHER_SERIOUS | 200 DISPOSITIONED; `assigned_at` = d0 + 4 h (within 60 s: real seconds pass between calls); `deadline_utc` = `assigned_at` + 15 d; ledger: one DISPOSITION_RECORDED by rev-1, tier OTHER_SERIOUS | spec 4.7 (d15 13:00); spec 12 step 7; spec 2.10 payloads |
| +3d DEATH | 200; `assigned_at` unchanged (same anchor); `deadline_utc` = anchor + 10 d; one revision OTHER_SERIOUS → DEATH by rev-1; ledger: one DISPOSITION_REVISED whose `supersedes_id` is the first disposition | spec 4.7 (d10 13:00, "the second superseding the first"); spec 12 step 8; spec 2.10 |
| +5d4h | reminders `[50]`: d5 13:00 = anchor + 5 d | spec 4.7; spec 3.3 consequences and the 3.9 table (stricter revision before 50 %) |
| +7d16h | still `[50]`: d8 01:00, the old 15-day 50 % instant, sends nothing | spec 4.7 "none at d8 01:00" |
| +8d4h | `[50, 80]`: d8 13:00 | spec 4.7 |
| +10d4h | `[50, 80, 100]`: d10 13:00 | spec 4.7 |
| +11d4h | still `[50, 80, 100]` | spec 12 step 9 "none … after" |
| every reminder | `tier` DEATH, `assigned_at` the anchor, `deadline_utc` the revised deadline, `system_id`, `type` | spec 3.5 (reminder body; values in force when triggered) |

The report step is left out: Model 5's report expectations are Phase 5 (spec 4.7).

**Ex-2, assessed not serious** (spec 4.7 Ex-2 row): reminder-only webhook; accuracy_drift
0.34, `drift` (gt 0.30 in the seed) → FLAGGED; reviewer opens → UNDER_REVIEW; NOT_SERIOUS with
a blank rationale → 400 with the exact body `{"error_code": "RATIONALE_REQUIRED", "message":
"rationale is required when tier is NOT_SERIOUS"}` (contract Example 3; spec 2.3 and section 7) and nothing
appended, state still UNDER_REVIEW with no disposition; NOT_SERIOUS with "seasonal shift, no
user impact" → 200 CLOSED, `deadline_utc` null, no revisions; the report → 501 `{"error_code":
"NOT_IMPLEMENTED", "message": "not implemented in this phase"}` (D1; Phase 3 spec error codes; README 11.2 on phase4-integration) and nothing
appended; at +16 d (past the longest window) no reminder for the event, "no reminders ever".

**Ex-4, unauthorized and premature advance** (spec 4.7 Ex-4 row; Addendum rule 9 over
Model 5): the event in UNDER_REVIEW; viewer-1's view appends exactly one EVENT_VIEWED by
viewer-1; viewer-1's DEATH → 403 `{"error_code": "FORBIDDEN_ROLE", "message": "this role may
not call this endpoint"}` (Phase 3 spec role matrix; decision 37 in docs/decisions.md; README 11.2 on phase4-integration) and nothing appended, state UNDER_REVIEW; the reviewer's
report before any disposition → 409 `{"error_code": "NO_DISPOSITION", "message": "no disposition
has been recorded for this event"}` (spec 2.8 report guard, step 3) and nothing appended, state
UNDER_REVIEW with no disposition.

These examples need Parts 1–3: pre-fill (Part 1), disposition and deadlines (Part 2) and
reminders (Part 3). On a part branch they fail at the first missing step, by design; the image
job runs them only under the Addendum 9.5 condition.
