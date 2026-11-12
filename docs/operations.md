# Operations note (Phase 5)

The operations note of Phase 5 spec 7.5 (Part 3). The ten headings are pinned by the spec. Facts
about deployment are read from `origin/main` at `d9205f2` (`fly.toml`, `scripts/deploy-fly.sh`,
`scripts/fly-wake.sh`, `scripts/fly-sleep.sh`, `scripts/cloud-bootstrap-db.sql`); anything not
read from the repository or checked by a run is marked **UNVERIFIED**. Secret values and keys are
never written here, only their names (item 10).

## 1. Processes and where they run

The image (`Dockerfile`, entrypoint `python -m app.runtime`) has three commands:

| Command | What it does |
| --- | --- |
| `prepare` | Waits for PostgreSQL, migrates to the script head as the owner role, installs the seed fixtures. |
| `api` | Waits until the database is at the head, seeds again (no-op when present), serves the API on port 8000. |
| `worker` | Same wait and seed, then the worker loop: gap sweeper, escalations, deliveries, the nightly verification (item 5) and the alert step (item 4). |

The webhook **sink** (`tools/webhook_sink.py`, port 8765) is a test receiver. It runs in the
compose stack only.

| Where | What runs |
| --- | --- |
| Fly.io (`fly.toml` on main) | App `eu-ai-act-monitor-api`: processes `api` and `worker`, one `shared-cpu-1x` 512 MB machine each; `prepare` as the release command. PostgreSQL is a separate Fly Postgres app (`eu-ai-act-monitor-db` by default in `scripts/deploy-fly.sh`). No sink. |
| Vercel | The static console only (`vercel.json` builds `web/` from `app/static` with `scripts/vercel-build.mjs`; `scripts/deploy-vercel.sh`). It needs `API_BASE_URL` and runs no Python, database or worker. |
| Docker Compose (local and CI) | `postgres` (16), `migrate` (`prepare`), `api`, `worker` and `sink`. CI's `image` job starts this stack with `docker compose up -d --build --wait`. |
| GitHub Actions | `ci.yml` (tests and checks) and `chain-head.yml` (item 5). |

## 2. Release process and version

**What main holds (read from `origin/main` at `d9205f2`).** Decision 295: the OD-1 premise of the
spec (deployment config only on `development-deploy`) was overtaken, because main now carries the
Fly config and scripts. `development-deploy` was not read (hard rule); whether deploys are still
made from it is **UNVERIFIED**.

- `fly.toml`: `app = "eu-ai-act-monitor-api"`, `primary_region = "iad"`. `[processes]`
  `api = "api"` and `worker = "worker"`. `[deploy] release_command = "prepare"`, so every deploy
  migrates before the new machines start. `[http_service]`: internal port 8000, HTTPS forced,
  `auto_stop_machines = "off"`, `auto_start_machines = true`, `min_machines_running = 1`,
  for the `api` process only. Health check: `GET /livez` every 15 s, timeout 5 s, grace period
  30 s (the file's comment: `/healthz` can return 503 when the sweeper is stale, which Fly would
  treat as down). `[[restart]] policy = "always"`, 10 retries. `[env]` sets
  `ENVIRONMENT = "test"`, the published test keys, `SUITE_BASE_URL`, `TESTER_TARGET_URL`,
  `LOCK_TIMEOUT`, `LOG_LEVEL`, `CORS_ORIGINS` and the deadline and escalation settings. `[build]`
  is empty: no build argument is set there.
- `scripts/deploy-fly.sh`: needs `fly auth login`. Creates the app (`FLY_APP`, default
  `eu-ai-act-monitor-api`) and a single-node Fly Postgres app (`FLY_POSTGRES_APP`, default
  `eu-ai-act-monitor-db`, region `FLY_REGION`, default `iad`, `shared-cpu-1x`, 1 GB volume) if
  they do not exist; attaches the database; opens `fly proxy 15432:5432`; runs
  `scripts/cloud-bootstrap-db.sql` through `fly postgres connect`; sets the Fly secrets
  `DATABASE_URL`, `DATABASE_ADMIN_URL` (both to `<pg-app>.internal:5432/monitor_test`) and
  `SUITE_BASE_URL`; then runs `fly deploy -a "$APP" --ha=false` and prints the API URL.
  **It passes no build argument**, so an image deployed with it has no `APP_VERSION` and
  `/healthz` shows `"version": null`.
- `scripts/cloud-bootstrap-db.sql`: idempotent; creates the login roles `monitor_owner` and
  `monitor_app` if missing, the database `monitor_test` owned by `monitor_owner`, makes
  `monitor_owner` the owner of schema `public`, revokes `CREATE` on it from `PUBLIC`, and grants
  `USAGE` and `CONNECT` to the app role.
- `scripts/fly-wake.sh`: starts the Postgres, API and worker machines by machine id (defaults
  are written in the script; `FLY_*_MACHINE` overrides them) and polls `/healthz` for up to
  120 s until it returns 200.
- `scripts/fly-sleep.sh`: stops the worker, API and Postgres machines. **While they are stopped
  there is no sweeper, no reminder (LIM-08), no nightly verification (alert A2 after 26 hours),
  and the anchor workflow fails at the download step**, because the export needs the database.
  Whether `auto_start_machines = true` would start the API machine for the workflow's request is
  **UNVERIFIED**; the Postgres machine is in another app and stays stopped, so the download
  fails either way.

**Deploying with a version.** After the one-time setup of `deploy-fly.sh`, deploy from the repository root with the
main commit being deployed written out (not `git rev-parse HEAD` of a merge branch):

```
fly deploy -a eu-ai-act-monitor-api --ha=false --build-arg GIT_SHA=<commit>
```

The Dockerfile's `ARG GIT_SHA` becomes `ENV APP_VERSION`, which `/healthz` and the dashboard
report as `version` (decision 295). After the deploy, `GET /healthz` must show
`"version": "<commit>"`; if it shows null, the image was built without the argument. Before a
deploy that adds migrations, back up the database (item 7): an image at an older head cannot
start on a newer database.

**Health-check path.** Fly checks `GET /livez`, which answers 200 while the process serves HTTP
and touches no database. `GET /healthz` is 503 when the database is unreachable or the sweeper
has gone quiet for more than 3 × `SWEEP_INTERVAL_SECONDS`, so as a Fly check it would restart
the API for a worker or database problem. Neither path checks ledger integrity: a tampered
ledger is not fixed by a restart, so it is an alert (A1), never a failed health check (spec 6.5).

## 3. Health signals

**`GET /healthz`** (no key; 200, or 503 as above). The first four fields are unchanged from
Phase 4; the last four are Phase 5 (spec 6.5).

| Field | Meaning |
| --- | --- |
| `sweeper_last_ran_seconds_ago` | Wall-clock seconds since the gap sweeper last ran; null before its first run. Above 3 × `SWEEP_INTERVAL_SECONDS` gives 503 and alert A3. |
| `pending_deliveries` | Notification deliveries not yet delivered or failed. |
| `oldest_pending_delivery_seconds` | Age of the oldest pending delivery, or null. Above `NOTIFY_MAX_DELAY_SECONDS` raises A4. |
| `overdue_escalations` | Reminders past due and not fired. Above 0 raises A5. |
| `version` | `APP_VERSION` from the image build (item 2), or null. |
| `last_verification` | `{finished_at, valid, head_check}` of the latest verification run of any trigger, or null before the first run. |
| `last_verification_age_seconds` | Whole wall-clock seconds since that run finished, or null. |
| `reset_count` | How many `POST /v1/test/reset` calls the database has seen (`platform_counter`). |

When the database is unreachable the 503 body keeps `version` and sets the other three Phase 5
fields to null (decision 306).

**`GET /v1/ops/dashboard`** (REVIEWER or VIEWER; SYSTEM 403; no key 401). Ids, counts, codes and
timestamps only, never a payload value, hash, webhook URL or rationale.

| Field | Meaning |
| --- | --- |
| `generated_at` | Wall-clock time of the response. |
| `version`, `environment` | `APP_VERSION` (or null) and `ENVIRONMENT`. |
| `worker.*` | The four `/healthz` worker values plus `failed_deliveries_24h`: FAILED deliveries whose last attempt (the `NOTIFICATION_FAILED` entry) is within 24 hours of the application clock (decision 307). |
| `ledger.entries`, `ledger.head_seq` | Count and highest seq of the ledger. |
| `ledger.last_verification` | The latest run: run id, trigger, times, the three verify fields, head seq and check, and the projection and report mismatches as counts. No hash (decision 313). |
| `ledger.last_verification_age_seconds` | As on `/healthz`. |
| `events.by_state` | Count of events per state (FLAGGED, UNDER_REVIEW, DISPOSITIONED, CLOSED). |
| `events.open_gaps` | Monitoring-gap events not CLOSED. |
| `deadlines.due_within_24h_unsubmitted` | DISPOSITIONED events due within 24 hours with no submitted report bound to the latest disposition. |
| `deadlines.passed_unsubmitted`, `passed_submitted` | DISPOSITIONED events past their deadline, without and with such a submission. |
| `reports.assembled`, `submitted`, `superseded` | Three independent totals: all report rows, rows with a submission, rows not bound to their event's latest disposition (decision 313). |
| `retention.past_retain_until_not_erased`, `erased` | Events past `retain_until` and not erased; events erased. |
| `clock` | `{simulated_time, offset_seconds}`; present only when `ENVIRONMENT=test`. |
| `alerts` | The alerts that hold now (item 4), each `{code, severity, since, detail}`. |

Tables that are not on a branch yet (Part 1's `report` and `report_submission`, Part 2's
`event.erased_at`) count as zero there (decision 305).

## 4. Alerts A1 to A9

Alerts are computed on each dashboard call and by the worker's alert step after the nightly step
in every worker iteration. The worker logs each alert once per code and subject per wall-clock
UTC day on the `ops` logger: `alert: code=<code> <name>=<value> …`, at ERROR for critical and
WARNING otherwise. The day's memory is in process, so a worker restart can repeat a line
(decision 309). There is no alert webhook (Q-8). `now` is the application clock (`clock.now()`)
except where the wall clock is named.

| Code | Severity | Condition | `since` | What to check, what to do |
| --- | --- | --- | --- | --- |
| A1 | critical | Latest verification run invalid, `head_check = REGRESSED`, or any projection or report mismatch | The run's `finished_at` | Treat as tampering until shown otherwise; follow the tamper response below. |
| A2 | warning | No verification run for more than 26 hours (wall clock), or no run at all | 26 h after the last run (null with no run) | Is the worker running (`sweeper_last_ran_seconds_ago`)? Were the machines stopped (item 2)? Check the worker log for `nightly verification failed`. Run one by hand (item 5). |
| A3 | critical | `sweeper_last_ran_seconds_ago > 3 × SWEEP_INTERVAL_SECONDS` (wall clock) | When the bound was crossed | The worker is down or stuck: check `fly status` and the worker log, restart the worker machine. `/healthz` is 503 meanwhile. |
| A4 | warning | `oldest_pending_delivery_seconds > NOTIFY_MAX_DELAY_SECONDS` (900) | When the bound was crossed | Receiver unreachable or worker behind: check the webhook receiver and the delivery log lines. |
| A5 | warning | `overdue_escalations > 0` | When the first one became overdue | Usually machines were stopped (LIM-08); reminders fire late on wake. Check the worker is running. |
| A6 | critical | A DISPOSITIONED event whose deadline has passed has no submitted report bound to its latest disposition | The deadline | The reporting deadline is missed: assemble and submit the report now, and tell the team. |
| A7 | warning | A DISPOSITIONED event due within `ALERT_DEADLINE_WARNING_HOURS` (default 24), not yet passed, with no submitted current report | The deadline less the window | Assemble and submit the report before the deadline. After the deadline only A6 holds (decision 313). |
| A8 | warning | `failed_deliveries_24h > 0` | The earliest failure counted | Check the receiver URL and its availability; the failed notification is in the ledger as `NOTIFICATION_FAILED`. |
| A9 | info (logged at WARNING) | `past_retain_until_not_erased > 0` | The earliest `retain_until` counted | Operator task: decide on erasure (item 6). |

**A failing nightly run gives two ERROR lines.** They are written by different steps and mean
different things:

1. `alert: code=LEDGER_INTEGRITY run_id=… trigger=NIGHTLY valid=… first_mismatch=… head_check=…
   head_seq=… projection_mismatches=… event_ids=… report_mismatches=… report_ids=…` on the
   `ledger_ops` logger, written **once**, by the nightly step, when the failing run is recorded
   (spec 6.1). It is the detection record: which run, what failed, and the seqs and ids
   involved, never a value. A manual run (`POST /v1/ledger/verifications`) that fails writes the
   same line in the API process's log with `trigger=MANUAL`.
2. `alert: code=A1 run_id=… valid=… first_mismatch=… head_check=… projection_mismatches=…
   report_mismatches=…` on the `ops` logger, written by the alert step because the **latest**
   run fails. It repeats once per wall-clock UTC day while that run stays the latest failing run,
   and a new failing run gives a new line. It means "the platform is still in the A1 state".

**Tamper response (Model 5 Ex-5).** (a) Stop writes: tell the team, stop ingestion and review
work, and stop the worker machine if needed. (b) Export the ledger (`GET /v1/ledger/export`) and
keep the file. (c) Compare it with `chain-heads`: run `python tools/verify_ledger.py export.json
--anchor heads.jsonl` with `heads.jsonl` from the `chain-heads` branch. (d) Preserve the database:
take a Fly Postgres backup or snapshot (item 7) before any repair, and do not reset. (e) Inform
the team with the run id, the seqs and ids from the two lines, and the anchor result.

## 5. Nightly verification and anchoring

**Nightly verification.** The worker runs it once per wall-clock UTC day, from
`NIGHTLY_VERIFY_HOUR_UTC` (default 2) on; a worker that was down at that hour runs it later the
same day (decision 302). It checks the hash chain, compares the head with the previous run's
head (`REGRESSED` if the chain got shorter or the old head changed), replays the ledger against
the event, disposition and harm-field rows (projection check) and re-hashes the stored report
bytes. Results go to `ledger_verification_run`, never into the ledger. To run one by hand:
`POST /v1/ledger/verifications` with the reviewer key (201 with the run); to list runs:
`GET /v1/ledger/verifications?limit=n` (REVIEWER or VIEWER, default 10, maximum 100).

**Anchoring.** `.github/workflows/chain-head.yml` runs daily at 03:17 UTC (`17 3 * * *`, after the
nightly hour) and by hand (`workflow_dispatch`). It downloads `GET /v1/ledger/export` and
`/healthz` from `vars.POD_BASE_URL` with `secrets.POD_VIEWER_KEY`, logs `reset_count` and
`version`, runs `python tools/verify_ledger.py export.json --anchor heads.jsonl` against
`heads.jsonl` on the `chain-heads` branch, appends one line `{taken_at, base_url, app_version,
seq, hash, recorded_at, verify}` and pushes it without force. It fails, and commits nothing, if
the export does not verify, the chain is shorter than the last head, or the head's entry is
missing or has another hash. A scheduled run with no head yet fails (decision 299). The export is
never printed, uploaded or committed.

The workflow runs only once it is on main, with the repository variable `POD_BASE_URL`, the
secret `POD_VIEWER_KEY` and the branch `chain-heads` in place; each of these needs the team's
approval and none exists yet (decisions 311, 312).

**What is detected:** a changed entry anywhere (hash chain); a shorter chain or a changed entry at
or below the previous run's head (head check); truncation below, or a rewrite at or below, the
last anchored head even if every later hash was recomputed (`chain-heads`); an edited
`event.state`, `event.deadline_utc`, event metric fields, the latest disposition's tier,
deadline, anchor and calendar and the disposition count, the four harm-field statuses, and the
report rows and hashes (projection check); changed stored report bytes (report check).

**What is not detected:** entries appended after the last anchor (up to about 24 hours) can be
truncated or forged until the next anchor run, and were never anchored; `event.detected_at`,
`received_at`, `retain_until`, `evidence_bundle`, `evidence_sha256`, `harm_field.evidence` and
`confirmed_at`, and `metric_sample` rows, which no ledger payload carries (Q-7).

**Restarting the anchor series after a test reset** (spec 6.2). A `POST /v1/test/reset` empties
the ledger, so the next scheduled run fails with `anchor: MISMATCH seq=<s>`, as it must.

- (a) Confirm from the failed run's log that `reset_count` rose since the last committed head
  (compare with the last passing run's log line `reset_count=…`), and that a reset was booked
  (item 8). If it did not rise, treat the failure as tampering (item 4) and do not restart.
- (b) In GitHub Actions, run `chain-head.yml` by hand (`workflow_dispatch`) with
  `new_series: true` and a `reason` naming the reset and who made it. That run verifies the
  export without `--anchor` and appends two lines: `{new_series, reason, reset_count,
  previous_seq, previous_hash, actor}` and the new head.
- (c) Check that `heads.jsonl` on `chain-heads` gained the `new_series` line followed by a new
  head, and that the next scheduled run passes (`anchor: OK seq=<s>`).

The first head of all is written the same way, by a `new_series` run.

## 6. Retention and erasure

**Rule.** `retain_until = max(detected_at + 6 calendar months, detected_at + 183 days)`, set once
when the event is created; it covers the event and everything linked to it (spec 5.1). Nothing is
purged (D9).

**Finding due records.** The dashboard's `retention.past_retain_until_not_erased` and alert A9.

**Erasing.** Operator-triggered, never automatic (D8): `POST /v1/events/{event_id}/erasure`
with the reviewer key (Part 2's route, spec 4.4; not on the Part 3 branch). 409
`RETENTION_NOT_EXPIRED` before `retain_until`, 409 `ALREADY_ERASED` the second time, otherwise
200 and one `RECORD_ERASED` ledger entry that names the erased fields, never their values.
Check first that no open investigation still needs the record (no legal hold exists in the
system).

**Limits (spec 4.7).** Only rows written from migration 0009 on can be erased. Backups and volume
snapshots keep destroyed keys until they expire (item 7). `actor_id` is not erased. Erasure gives
erasure, not confidentiality (D7).

## 7. Backups and restore

The database is a Fly Postgres app (item 2). **The backup retention of Fly Postgres is
UNVERIFIED (Q-6)**, and so is the restore procedure: no backup or restore has been run or tested
for this project. Take a backup or volume snapshot before a deploy that migrates and before any
repair after an A1. Erasure (item 6) is permanent only once every backup that holds the
destroyed key has expired, which depends on that unverified retention.

## 8. Test environment etiquette

The deployed service runs `ENVIRONMENT=test` with published test keys, shared by everyone who
tests against it (LIM-03).

- **Book a window** with the team before any reset, clock advance or suite run, and do not run
  suites at the same time as another team. `fly.toml` keeps the API always on for the staff
  project-tester window (Wed to Thu); do not stop machines then.
- **Reset** (`POST /v1/test/reset`, REVIEWER or VIEWER) empties the data tables, reseeds, sets the
  clock offset to 0 and adds 1 to `reset_count`. It breaks the anchor series: follow item 5's
  restart (a) to (c) after every reset on the deployed service.
- **Clock** (`POST /v1/test/clock/advance`) moves the application clock for everyone. Advance
  only what the test needs (LIM-12: not into year 9999), and reset afterwards.
- **Keys** are public test keys: never put real personal data or secrets into the service or a
  webhook URL (LIM-13, LIM-14).

## 9. Known limitations

Carried from Phase 4 (Phase 4 report Q7.2):

- LIM-05: reminders go only to registered webhooks; there is no e-mail or named compliance
  officer account.
- LIM-06: delivery is at least once and a retry can reorder reminders (intended design).
- LIM-07: a reminder already triggered is still delivered after a revision to NOT_SERIOUS, and a
  notification is routed once.
- LIM-08: no reminder fires while machines are stopped; each fires once, late, on wake. Alert A5
  shows it.
- LIM-09: a reviewer's GET of a FLAGGED breach event starts the review (agreed behaviour).
- LIM-14: the deployment is in Fly region `iad` (US East), not an EU region.
- LIM-15: working-day deadlines use UTC days.

Phase 5:

- Closure reports cannot be submitted, so a "Final (Non-reportable incident)" is filed outside
  the system (Q-14).
- Rows written before migration 0009 cannot be erased (spec 4.7).
- Erasure gives erasure, not confidentiality (D7): the data keys are stored unwrapped.
- Entries after the last anchor are not anchored (item 5), and the columns listed there are not
  covered by any check.
- A legitimate test reset fails the next anchor run until a person restarts the series (item 5).

## 10. Secrets inventory

Names only, never values.

| Name | Where | Notes |
| --- | --- | --- |
| `DATABASE_URL` | Fly secret (set by `deploy-fly.sh`) | Application role. |
| `DATABASE_ADMIN_URL` | Fly secret (set by `deploy-fly.sh`) | Owner role: migrations and reset. |
| `INGEST_KEY_SYS_42`, `REVIEWER_KEY`, `VIEWER_KEY` | `fly.toml` `[env]` on main | Published test keys (LIM-03), not secrets. |
| `REVIEWER_2_KEY` | Fly secret, when Part 1 is deployed | Needs the team's approval (spec 11.6). |
| `POD_VIEWER_KEY` | GitHub Actions secret | Read by `chain-head.yml` only, through `env`. Needs the team's approval; not created. |
| `POD_BASE_URL` | GitHub Actions repository variable (not a secret) | The deployed base URL for `chain-head.yml` (decision 311). Not created. |

`scripts/cloud-bootstrap-db.sql` and `deploy-fly.sh` hold the database role passwords used for
the test deployment; they are not repeated here.
