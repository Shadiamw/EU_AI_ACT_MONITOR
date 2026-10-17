# Part 1 context and missing directions

Reviewed on 2026-10-17: README.md, integration_notes.md, docs/decisions.md,
the existing application, migrations and test fixtures, and both supplied PDFs:

- Pod_H_API_Convention.pdf (6 pages, dated October 5, 2026).
- compliance-monitoring-architecture-options.md.pdf (13 pages).

## Scope supported by the available material

The repository calls Part 1 the API owner. Its apparent implementation scope is:

- `POST /v1/metrics`: validate and authenticate a metric sample, store it,
  evaluate a versioned threshold, and create or join a breach event.
- `GET /v1/events`: authenticated filtering and pagination, including worker-created
  monitoring-gap events.
- `GET /v1/events/{event_id}`: return the Event object and record EVENT_VIEWED
  in the same transaction, committed before returning.
- Replace the authentication stub and the ledger-based event-existence stub;
  preserve the shared Principal, require, ApiError and session interfaces.
- Mount the existing ledger and webhook routers alongside the new endpoints.
- Call `gaps.close_if_open` for resumed monitoring and `notifications.enqueue`
  for a newly flagged breach, inside the ingestion transaction.
- Add Part 1 persistence and tests while preserving the existing ledger and worker behavior.

The attached documents do not explicitly assign a section named
"Part 1: Detection and Ingestion (API)". That assignment needs the separate specification.
Disposition and report operations are deferred to Phase 4 or 5 by the API Convention.
Reset, simulated clock, baseline fixtures, and platform tables are assigned to Part 4
by the repository's integration notes.

## How the sources relate

The API Convention supplies the agreed external request and response contract.
The architecture document supplies recommendations and alternatives, rather than a
complete implementation specification. For example, its OIDC recommendation differs
from the convention's three seeded API credentials; its suggested snapshot identifier
is absent from the convention's ingestion request. Those recommendations should not
silently change the agreed HTTP contract.

The repository records settled ledger behavior and shared integration interfaces.
It repeatedly cites an external Phase 3 specification, including a pinned exact
ingestion algorithm that is absent from both PDFs and the repository.

## Most useful missing resource

Please locate the Phase 3 implementation specification containing these headings
(quoted by integration_notes.md):

- Part 1 / Detection and Ingestion / API owner.
- POST /v1/metrics (exact algorithm).
- Shared rules (Pinned).
- Transactions and lock order (Pinned).
- Shared interfaces between parts (Pinned).
- Configuration and tests/acceptance criteria.

The original requirements and acceptance criteria would also resolve the REQ/AC
references, which are not provided in full by either PDF.

## Details to recover from that specification or confirm internally

1. Duplicate identity, replay response, and behavior when the same identity carries
   a different value; the API Convention explicitly leaves duplicate semantics open.
2. Threshold schema, baseline values for each metric, comparator and equality rules,
   version selection time, and behavior when no threshold matches.
3. Open-series identity, occurrence storage, recovery/closure rules, and treatment
   of late or out-of-order samples.
4. Part 4's `api_key` and `threshold_version` schemas and fixture handoff, including
   the credential header and hash format. Neither table currently exists.
5. Event-list summary shape, pagination fields/defaults, stable ordering, and SYSTEM
   visibility. The convention allows every authenticated role to list events.
6. Initial harm-field evidence and status. The existing worker stores null values
   for gaps, while the API schema describes a HarmField object for every field.
7. Ingest response ambiguity: page 2 says "the stored sample", but the fixed example
   on page 5 returns only `sample_id` and `flagged_event_id` for a breach.

## Existing integration constraints

- Use Decimal internally and plain decimal strings in ledger payloads; HTTP Event
  values are JSON numbers under the API Convention.
- Lock the monitored-system row before event/notification rows and the ledger.
- Preserve the documented append order: optional GAP_CLOSED, then EVENT_FLAGGED
  and NOTIFICATION_REQUESTED for a new breach. Duplicate and series-join paths
  append no breach or notification entries.
- Preserve authentication before authorization before resource lookup, and the
  existing generic error bodies and rollback behavior.
- Keep network delivery outside ingestion transactions; the worker owns delivery.
- Update the existing API fixtures when event existence switches to the event table:
  those fixtures currently create only ledger entries for breach events.

This was the initial context review. The user subsequently authorized implementation
using the available discussion. See [part1_api.md](part1_api.md) for the implemented
behavior, setup, and explicit assumptions that still need team confirmation.
