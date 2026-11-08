"""Fixtures and helpers for the Phase 5 Part 1 tests (reports): the real app, real keys and a
real PostgreSQL database, as for the Phase 4 Part 2 tests, whose fixtures are re-exported here.

Time is the fake clock throughout (it patches app.clock.now, which the disposition, report and
submission handlers read); the HTTP test endpoints are never used. Requests carry a real
`Authorization: Bearer <key>` header.
"""

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import Engine

from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.part1.conftest import rows
from tests.part2.conftest import (
    ANCHOR as ANCHOR,
)
from tests.part2.conftest import (
    NO_DISPOSITION as NO_DISPOSITION,
)
from tests.part2.conftest import (
    SERIOUS as SERIOUS,
)
from tests.part2.conftest import (
    TIERS as TIERS,
)
from tests.part2.conftest import (
    body as body,
)
from tests.part2.conftest import (
    client as client,
)
from tests.part2.conftest import (
    decided as decided,
)
from tests.part2.conftest import (
    dispose as dispose,
)
from tests.part2.conftest import (
    fake_clock as fake_clock,
)
from tests.part2.conftest import (
    flag as flag,
)
from tests.part2.conftest import (
    ledger_rows as ledger_rows,
)
from tests.part2.conftest import (
    seeded as seeded,
)
from tests.worker.conftest import FakeClock

HOUR = timedelta(hours=1)


def fetch(
    client: ApiClient, event_id: str, fmt: str | None = None, actor: str | None = "rev-1"
) -> httpx.Response:
    """GET /v1/events/{event_id}/report, with ?format=<fmt> when given."""
    query = "" if fmt is None else f"?format={fmt}"
    headers = {} if actor is None else bearer(actor)
    return client.get(f"/v1/events/{event_id}/report{query}", headers=headers)


def disposed_event(
    client: ApiClient,
    fake_clock: FakeClock,
    tier: str = "DEATH",
    *,
    rationale: str | None = None,
    harm_fields: Any = None,
    hours: float = 4,
) -> str:
    """Flag contract Example 1 at T0, move the clock on `hours` and record a disposition."""
    event_id = flag(client)
    fake_clock.advance(hours * 3600)
    if rationale is None and tier == "NOT_SERIOUS":
        rationale = "seasonal shift, no user impact"
    response = dispose(client, event_id, tier, rationale, harm_fields=harm_fields)
    assert response.status_code == 200, response.text
    return event_id


def get_report(client: ApiClient, event_id: str) -> dict[str, Any]:
    """The assembled report (JSON) of an event, after 1 hour on the fake clock."""
    response = fetch(client, event_id)
    assert response.status_code == 200, response.text
    report: dict[str, Any] = response.json()
    return report


def submit(
    client: ApiClient,
    event_id: str,
    report_id: str,
    json_sha256: str,
    report_type: str = "Initial",
    *,
    actor: str | None = "rev-1",
    **extra: Any,
) -> httpx.Response:
    headers = {} if actor is None else bearer(actor)
    return client.request(
        "POST",
        f"/v1/events/{event_id}/report/submission",
        headers=headers,
        json={"report_id": report_id, "json_sha256": json_sha256, "report_type": report_type}
        | extra,
    )


def listing(client: ApiClient, event_id: str, actor: str | None = "viewer-1") -> httpx.Response:
    headers = {} if actor is None else bearer(actor)
    return client.get(f"/v1/events/{event_id}/reports", headers=headers)


def table_rows(owner_engine: Engine, table: str) -> list[dict[str, Any]]:
    return rows(owner_engine, f"SELECT * FROM {table}")
