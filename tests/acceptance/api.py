"""HTTP helpers for the acceptance runner. Only httpx and the contract's endpoints are used.

Every expected value in tests/acceptance comes from the pod contract (Contracts Schemas.docx),
the Phase 4 specification, the Phase 3 specification, or the facts Parts 1 and 2 publish in
README 10.3, 10.5, 11.2 and 11.6. None is read from the application code.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

SYSTEM_ID = "sys-42"

# Contract, HarmField object: the four keys in contract order.
HARM_KEYS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)

# Spec section 6: the four contract tier strings and the three windows.
TIERS = ("DEATH", "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "OTHER_SERIOUS", "NOT_SERIOUS")
TIER_DAYS = {"CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": 2, "DEATH": 10, "OTHER_SERIOUS": 15}

# Phase 3 spec, shared rules: seeded thresholds (version 1).
DRIFT_METRIC, DRIFT_LIMIT = "accuracy_drift", Decimal("0.30")  # breaches when > 0.30
PERF_METRIC, PERF_LIMIT = "accuracy", Decimal("0.70")  # breaches when < 0.70

# Spec 4.4 seed row and README 10.3: the one harm_context row, for the rights field.
RIGHTS_CONTEXT = (
    "sys-42 makes automated decisions about natural persons, so degraded performance can "
    "infringe their fundamental rights (Art. 3(49)(c))."
)

# Error bodies: Phase 3 spec shared rules, contract Example 3, spec 7, README 11.2.
MISSING_KEY = {"error_code": "MISSING_OR_INVALID_KEY", "message": "missing or invalid API key"}
FORBIDDEN = {"error_code": "FORBIDDEN_ROLE", "message": "this role may not call this endpoint"}
MALFORMED = {"error_code": "MALFORMED_BODY", "message": "the request is malformed"}
NOT_FOUND = {"error_code": "NOT_FOUND", "message": "resource not found"}
RATIONALE_REQUIRED = {
    "error_code": "RATIONALE_REQUIRED",
    "message": "rationale is required when tier is NOT_SERIOUS",
}
EVENT_NOT_REVIEWABLE = {
    "error_code": "EVENT_NOT_REVIEWABLE",
    "message": "a monitoring gap event cannot be dispositioned",
}
HARM_FIELDS_UNCONFIRMED = {
    "error_code": "HARM_FIELDS_UNCONFIRMED",
    "message": "all four harm fields must be CONFIRMED or NOT_APPLICABLE",
}
DEADLINE_EXTENSION = {
    "error_code": "DEADLINE_EXTENSION_REQUIRES_RATIONALE",
    "message": "extending a running deadline requires a rationale",
}
NO_DISPOSITION = {
    "error_code": "NO_DISPOSITION",
    "message": "no disposition has been recorded for this event",
}
NOT_IMPLEMENTED = {"error_code": "NOT_IMPLEMENTED", "message": "not implemented in this phase"}

_API_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,6})?Z$")
_LEDGER_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def parse_time(text: str) -> datetime:
    """An API timestamp (YYYY-MM-DDTHH:MM:SSZ, fraction only when present) as aware UTC."""
    match = _API_TIME.match(text)
    assert match, f"not an API timestamp: {text!r}"
    fraction = (match.group(2) or ".0")[1:].ljust(6, "0")
    whole = datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
    return whole + timedelta(microseconds=int(fraction))


def parse_ledger_time(text: str) -> datetime:
    """A ledger recorded_at (always three fractional digits, always Z; Phase 3 spec 2.3)."""
    assert _LEDGER_TIME.match(text), f"not a ledger timestamp: {text!r}"
    return parse_time(text)


def api_time(moment: datetime) -> str:
    """Whole-second API form; samples are sent at whole seconds so detected_at has no fraction."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(anchor: datetime, deadline: datetime, pct: int) -> datetime:
    """Spec section 6: anchor + ceil((deadline - anchor) * pct / 100), in whole microseconds."""
    window = (deadline - anchor) // timedelta(microseconds=1)
    return anchor + timedelta(microseconds=-(-window * pct // 100))


def decimal_json(response: httpx.Response) -> Any:
    """The body with JSON numbers as Decimal, so 0.30 and 0.3 compare by value."""
    return json.loads(response.text, parse_float=Decimal, parse_int=Decimal)


def error(response: httpx.Response) -> Any:
    return response.json()


def decisions(
    rights: str = "CONFIRMED", others: str = "NOT_APPLICABLE", **overrides: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """A disposition harm_fields object (spec 1.5): {key: {status, evidence}} for all four.

    By default the rights field is CONFIRMED with evidence null (keeps the pre-fill) and the
    other three are NOT_APPLICABLE, as in demo step 7.
    """
    fields: dict[str, dict[str, Any]] = {
        key: {"status": others, "evidence": None} for key in HARM_KEYS
    }
    fields["fundamental_rights_infringement"] = {"status": rights, "evidence": None}
    for key, value in overrides.items():
        fields[key] = value
    return fields


class Api:
    """One runner session against BASE_URL with the three seeded keys."""

    def __init__(self, http: httpx.Client, headers: dict[str, dict[str, str]]) -> None:
        self.http = http
        self.headers = headers

    # Clock -------------------------------------------------------------------------------
    def advance(self, seconds: int, role: str = "reviewer") -> datetime:
        response = self.http.post(
            "/v1/test/clock/advance", headers=self.headers[role], json={"seconds": seconds}
        )
        assert response.status_code == 200, response.text
        simulated: str = response.json()["simulated_time"]
        return parse_time(simulated)

    def now(self) -> datetime:
        """Addendum rule 7: read simulated now with a clock advance of 0."""
        return self.advance(0)

    def advance_to(self, target: datetime) -> datetime:
        """Advance by whole seconds until simulated now is at or after target."""
        current = self.now()
        if current < target:
            seconds = -(-(target - current) // timedelta(microseconds=1) // 1_000_000)
            current = self.advance(int(seconds))
        assert current >= target, (current, target)
        return current

    # Ingest ------------------------------------------------------------------------------
    def ingest(
        self,
        metric_name: str,
        value: str,
        *,
        at: datetime | None = None,
        metric_type: str | None = None,
        system_id: str = SYSTEM_ID,
        role: str = "system",
    ) -> httpx.Response:
        """POST /v1/metrics with value sent as the given JSON number text (digits kept)."""
        if metric_type is None:
            metric_type = "drift" if metric_name == DRIFT_METRIC else "performance"
        when = at if at is not None else self.now()
        body = json.dumps(
            {
                "system_id": system_id,
                "metric_name": metric_name,
                "metric_type": metric_type,
                "value": "__VALUE__",
                "timestamp": api_time(when),
            }
        ).replace('"__VALUE__"', value)
        return self.http.post(
            "/v1/metrics",
            headers={**self.headers[role], "Content-Type": "application/json"},
            content=body,
        )

    def flag(self, value: str = "0.61", metric_name: str = PERF_METRIC) -> tuple[str, datetime]:
        """A new breach event at simulated now (whole seconds): (event_id, detected_at)."""
        at = self.now().replace(microsecond=0)
        response = self.ingest(metric_name, value, at=at)
        assert response.status_code == 201, response.text
        event_id = response.json().get("flagged_event_id")
        assert isinstance(event_id, str), response.text
        return event_id, at

    # Events ------------------------------------------------------------------------------
    def event(self, event_id: str, role: str = "viewer") -> dict[str, Any]:
        """GET the event detail. The viewer key never changes state (D3)."""
        response = self.http.get(f"/v1/events/{event_id}", headers=self.headers[role])
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def events(self, **params: str) -> list[dict[str, Any]]:
        response = self.http.get("/v1/events", headers=self.headers["viewer"], params=params)
        assert response.status_code == 200, response.text
        body: list[dict[str, Any]] = response.json()
        return body

    def dispose(
        self,
        event_id: str,
        tier: str,
        *,
        rationale: str | None = None,
        harm_fields: Any = None,
        role: str = "reviewer",
        omit: tuple[str, ...] = (),
    ) -> httpx.Response:
        body: dict[str, Any] = {
            "tier": tier,
            "rationale": rationale,
            "harm_fields": decisions() if harm_fields is None else harm_fields,
        }
        for key in omit:
            body.pop(key, None)
        return self.http.post(
            f"/v1/events/{event_id}/disposition", headers=self.headers[role], json=body
        )

    def disposed(self, event_id: str, tier: str, **kwargs: Any) -> dict[str, Any]:
        response = self.dispose(event_id, tier, **kwargs)
        assert response.status_code == 200, f"{response.status_code} {response.text}"
        body: dict[str, Any] = response.json()
        return body

    def report(self, event_id: str, role: str = "reviewer") -> httpx.Response:
        return self.http.get(f"/v1/events/{event_id}/report", headers=self.headers[role])

    # Ledger ------------------------------------------------------------------------------
    def audit(self, event_id: str, role: str = "viewer") -> list[dict[str, Any]]:
        response = self.http.get(f"/v1/events/{event_id}/audit", headers=self.headers[role])
        assert response.status_code == 200, response.text
        body: list[dict[str, Any]] = response.json()
        return body

    def export(self) -> list[dict[str, Any]]:
        response = self.http.get("/v1/ledger/export", headers=self.headers["viewer"])
        assert response.status_code == 200, response.text
        body: list[dict[str, Any]] = response.json()
        return body

    def verify(self) -> dict[str, Any]:
        response = self.http.get("/v1/ledger/verify", headers=self.headers["viewer"])
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def entries(self) -> int:
        count: int = self.verify()["entries_checked"]
        return count

    # Webhooks ----------------------------------------------------------------------------
    def register_webhook(self, url: str, event_types: list[str]) -> str:
        response = self.http.post(
            "/v1/webhooks",
            headers=self.headers["reviewer"],
            json={"url": url, "event_types": event_types},
        )
        assert response.status_code == 201, response.text
        webhook_id: str = response.json()["webhook_id"]
        return webhook_id


def bodies(items: list[dict[str, Any]], notification_type: str) -> list[dict[str, Any]]:
    """Bodies the sink received of one notification_type (Addendum rule 7: always filter)."""
    found = []
    for item in items:
        body = item.get("body")
        if isinstance(body, dict) and body.get("notification_type") == notification_type:
            found.append(body)
    return found


def wait_until(
    probe: Callable[[], list[dict[str, Any]]],
    done: Callable[[list[dict[str, Any]]], bool],
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Poll probe() until done(result) or timeout; return the last result either way."""
    deadline = time.monotonic() + timeout
    result = probe()
    while not done(result) and time.monotonic() < deadline:
        time.sleep(0.5)
        result = probe()
    return result


@dataclass(frozen=True)
class Sink:
    """One channel of tools/webhook_sink.py, seen from the API (url) and from here (read_url)."""

    url: str
    read_url: str

    def clear(self) -> None:
        httpx.delete(self.read_url, timeout=30.0).raise_for_status()

    def received(self) -> list[dict[str, Any]]:
        response = httpx.get(self.read_url, timeout=30.0)
        response.raise_for_status()
        items: list[dict[str, Any]] = response.json()
        return items


def delivered_payload(api: Api, notification_id: str) -> dict[str, Any]:
    """The NOTIFICATION_DELIVERED payload of one notification (Phase 3 spec 2.4), polled."""

    def probe() -> list[dict[str, Any]]:
        return [
            entry["payload"]
            for entry in api.export()
            if entry["entry_type"] == "NOTIFICATION_DELIVERED"
            and entry["payload"].get("notification_id") == notification_id
        ]

    found = wait_until(probe, lambda rows: len(rows) >= 1)
    assert len(found) == 1, found
    return found[0]
