"""Fixtures and helpers for the Phase 5 Part 2 tests (records): the Part 1 report fixtures,
re-exported, plus helpers for the retention and erasure tests. Real app, real keys, real
PostgreSQL; time is the fake clock unless a test names the HTTP clock endpoint."""

from datetime import timedelta
from typing import Any

import httpx

from app.formats import add_calendar_months
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.part1.conftest import EXAMPLE_1 as EXAMPLE_1
from tests.part1.conftest import post as post
from tests.part1.conftest import rows as rows
from tests.reports.conftest import (
    client as client,
)
from tests.reports.conftest import (
    decided as decided,
)
from tests.reports.conftest import (
    disposed_event as disposed_event,
)
from tests.reports.conftest import (
    fake_clock as fake_clock,
)
from tests.reports.conftest import (
    fetch as fetch,
)
from tests.reports.conftest import (
    flag as flag,
)
from tests.reports.conftest import (
    ledger_rows as ledger_rows,
)
from tests.reports.conftest import (
    seeded as seeded,
)
from tests.reports.conftest import (
    submit as submit,
)
from tests.worker.conftest import T0 as T0


def erase(client: ApiClient, event_id: str, actor: str | None = "rev-1") -> httpx.Response:
    headers = {} if actor is None else bearer(actor)
    return client.request("POST", f"/v1/events/{event_id}/erasure", headers=headers, json={})


def retain_until(owner_engine: Any, event_id: str) -> Any:
    [row] = rows(owner_engine, "SELECT retain_until FROM event WHERE event_id = :e", e=event_id)
    return row["retain_until"]


def months_and_days(moment: Any, months: int, days: int) -> Any:
    """`moment` plus `months` calendar months plus `days` days (spec 5.3)."""
    return add_calendar_months(moment, months) + timedelta(days=days)
