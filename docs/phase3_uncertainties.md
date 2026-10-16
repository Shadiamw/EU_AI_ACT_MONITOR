# Phase 3 uncertainties: AWS and the Evidently format

Part 4 owns these. Parts 2 and 3 do not decide them. Nothing here is a guess about a
mapping the spec does not state.

## AWS

This repository does not create AWS resources. There is no account, region or secret
store in the project. What can be deployed is the image in `Dockerfile` and the process
shape in `docker-compose.yml`: PostgreSQL 16, one API process, one worker process.

A Phase 3 host that matches that shape is Amazon RDS for PostgreSQL 16, plus two
processes from the same image (two ECS services, or one host running this compose file).
The compose file sets `max_connections=200`, which covers one API pool (50) and one
worker pool (50) plus owner connections and the reserved superuser connections
(integration_notes.md 5.6). A second API task needs a larger `max_connections`.

Roles are not created by migration 0001. `monitor_owner` has no CREATEROLE, and a
migration running as that role cannot create roles (integration_notes.md 5.2, open item
8). On RDS the master user runs `tests/sql/bootstrap_roles.sql` once. The passwords in
that script and in `docker-compose.yml` are the local and CI passwords from the README.
An AWS deploy substitutes its own secrets and does not reuse them.

`ENVIRONMENT` on AWS is not `test`. Outside the test environment a webhook must be
public https (decision 98). `LOCK_TIMEOUT=5s` is set on the application connections
(decision 110). The test suite leaves it unset.

Still not built, and not required for the image to start:

- `clock_state` and the test-environment offset (spec 4.5). `app/clock.py` is still the
  stub: `now()` and `wall_now()` are real UTC, and `now()` does not open a second
  connection inside `append` (open item 9).
- `POST /v1/test/reset` and `POST /v1/test/clock/advance` (spec 4.6).
- Nightly verification and an external copy of the chain head. Both are Phase 5
  (integration_notes.md 5.11).

## Evidently

There is no Evidently parser in this repository. Ingest is Part 1. CI does not accept an
Evidently report as an input, because no accepted mapping exists.

The only metric document the ledger already stores is the EVENT_FLAGGED payload from
the worked example (README section 3; addendum rule 7):

- `metric_name` (string), for example `accuracy_drift`
- `observed_value` and `threshold_value`, decimal strings such as `"0.34"` and `"0.30"`,
  not JSON numbers (addendum rules 2 and 10)
- `system_id`
- `threshold_version_id`

An Evidently report (metric results, dataset statistics, a test suite) is a different
document. Putting that report into a ledger payload is not assumed: verify reads every
payload into memory (decision 20), and a non-integer number in a payload is rejected
unless it is a decimal string. Part 1 chooses the mapping when it implements ingest.
Until that choice is written down, this file is the record that Phase 3 has not made it.
