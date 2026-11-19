# Phase 5 manual checks (spec 10.6)

A person performs each check and fills in the result. These checks are not automated: they
cover what a test cannot judge (a real public receiver, a screen, the usability of a report,
the other team's review). Record the date, who did it, the deployed version (`/healthz`
`version`) and where the evidence (screenshots, receiver log, exports) is stored.

Deployed service: `https://eu-ai-act-monitor-api.fly.dev` (ENVIRONMENT=test), console at
`/ui/`. Keys: the test keys in `docker-compose.yml`, given to the other team out of band.

| Id | Check | Pass when | Result (pass/fail), date, who, version, evidence |
| --- | --- | --- | --- |
| M1 | **AC-02 real delivery.** Register a public receiver (the Fly sink app) with `POST /v1/webhooks`; withhold data for 3 intervals using the test clock; watch the receiver. Repeat with 2 intervals. | A gap `flag_notification` arrives within 15 min of the 3rd missed interval; withholding 2 intervals sends nothing; screenshots and receiver log saved | |
| M2 | **AC-09 screen review.** Open the console's Event review panel. | Exactly four options with the REQ-08b labels and day counts; NOT_SERIOUS without a rationale is refused on screen; with one, the event closes with no deadline; screenshot saved | |
| M3 | **AC-13 usability review**, ideally by a person from the other team: compare a downloaded PDF and JSON (path S, and the path-N closure report for U8) with the Commission template v1.0.0 and the draft guidance (spec 3.3.4). Use the console's Report and submission panel. | Pass or fail recorded for each of U1 to U8 (spec 3.3.4); a one-paragraph judgement on whether the provider could file an "Initial" report from it (guidance (39)) | U1: · U2: · U3: · U4: · U5: · U6: · U7: · U8: · Judgement: |
| M4 | **AC-07 confirmation record review.** For an event a reviewer has dispositioned, open it in the console. | Each of the four harm fields shows status, confirmer and time, and matches what the reviewer did | |
| M5 | **AC-18 chain integrity after erasure.** Export the ledger, erase an event past `retain_until` (advance the test clock), export again; run `tools/verify_ledger.py` on both. | Every earlier entry identical; both verifiers valid; the erased values unreadable through the API (report 410, evidence and rationale null) | |
| M6 | **AC-16 trace review** by the other team on the deployed service: run `BASE_URL=https://eu-ai-act-monitor-api.fly.dev python tools/demo_phase5.py` with a public receiver as `WEBHOOK_SINK_URL`, and review the printed trace. | One event id per path, consistent across every stage; the path-S report downloaded and reviewed; path N shows no deadline, no reminder, and a refused submission | |
| M7 | **AC-06 storage-layer permissions.** Re-run the review of [`ac06_storage_evidence.md`](ac06_storage_evidence.md) for the three new insert-only tables (`report`, `report_submission`, `ledger_verification_run`). | The application role has INSERT and SELECT only, and the trigger rejects UPDATE and DELETE, on all three (as `test_phase5_tables_and_grants` and `test_report_tables_are_insert_only` show in CI) | |
