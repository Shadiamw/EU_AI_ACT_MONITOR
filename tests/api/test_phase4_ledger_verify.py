"""The standalone verifier and the server agree on a real Phase 4 ledger (integration decision 220).

A live run through the real app writes all four Phase 4 entry types: REVIEW_STARTED (a reviewer's
first view, Part 1), DISPOSITION_RECORDED and DISPOSITION_REVISED (Part 2) and REMINDER_TRIGGERED
(Part 3, fired by Part 4's clock advance). The export from GET /v1/ledger/export is then checked
three ways: GET /v1/ledger/verify, verify_ledger called directly, and tools/verify_ledger.py run
as a real process on the exported file. All three must agree that it is valid (Addendum 9.1 and
9.9: both lists carry the four types). Then one character of one payload of each new type is
changed, once in a copy of the file and once in the database, and all three must name that
entry's seq as first_mismatch (Addendum rule 8 of the Part 2 addendum; Addendum 12).

Clock: the database offset moved through the clock-advance endpoint, never FakeClock (Addendum
rule 10). Marked integration because it needs all four parts' real code (Addendum 9.4).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import platform
from app.ledger.verify import verify_ledger
from app.main import create_app
from app.platform import with_phase4_tables
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.part1.conftest import OTHER_TABLES, rows
from tests.part2.conftest import dispose, flag, parse
from tests.script_helpers import run_subprocess
from tools.seed_part1 import seed

pytestmark = pytest.mark.integration

PHASE4_TYPES = (
    "REVIEW_STARTED",
    "DISPOSITION_RECORDED",
    "DISPOSITION_REVISED",
    "REMINDER_TRIGGERED",
)
DAY = timedelta(days=1)


@pytest.fixture
def live(owner_engine: Engine) -> Iterator[ApiClient]:
    """The real app (bearer keys) on the real clock plus the database offset, from the seed."""
    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(with_phase4_tables(conn, OTHER_TABLES))}"))
    with Session(owner_engine) as session:
        seed(session)
        session.commit()
    platform.set_offset_seconds(0)
    with ApiClient(create_app()) as client:
        yield client
    platform.set_offset_seconds(0)


def advance(client: ApiClient, seconds: int = 0) -> datetime:
    response = client.request(
        "POST", "/v1/test/clock/advance", headers=bearer("rev-1"), json={"seconds": seconds}
    )
    assert response.status_code == 200, response.text
    return parse(response.json()["simulated_time"])


def write_the_four_types(client: ApiClient) -> None:
    """Flag, a reviewer's first view, OTHER_SERIOUS, DEATH, then past DEATH's 50% instant."""
    event_id = flag(client)
    assert client.get(f"/v1/events/{event_id}", headers=bearer("rev-1")).status_code == 200
    first = dispose(client, event_id, "OTHER_SERIOUS")
    assert first.status_code == 200, first.text
    revised = dispose(client, event_id, "DEATH")
    assert revised.status_code == 200, revised.text
    anchor = parse(first.json()["disposition"]["assigned_at"])
    gap = anchor + 5 * DAY - advance(client)
    advance(client, -(-gap // timedelta(seconds=1)))


def three_results(client: ApiClient, app_engine: Engine, path: Path) -> list[dict[str, Any]]:
    """GET /v1/ledger/verify, verify_ledger directly, and the script on `path`."""
    api = client.get("/v1/ledger/verify", headers=bearer("viewer-1"))
    assert api.status_code == 200, api.text
    with Session(app_engine) as session:
        direct = dict(verify_ledger(session))
        session.rollback()
    script = run_subprocess([str(path)])
    assert script.code == (0 if script.result and script.result["valid"] else 1), script.stderr
    assert script.result is not None, script.stderr
    return [api.json(), direct, script.result]


def export(client: ApiClient, path: Path) -> list[dict[str, Any]]:
    response = client.get("/v1/ledger/export", headers=bearer("viewer-1"))
    assert response.status_code == 200, response.text
    path.write_bytes(response.content)  # the file exactly as served
    exported: list[dict[str, Any]] = json.loads(response.content)
    return exported


def first_string(payload: dict[str, Any]) -> tuple[str, str]:
    """The first top-level string value of a payload, in key order."""
    for key in sorted(payload):
        if isinstance(payload[key], str) and payload[key]:
            return key, payload[key]
    raise AssertionError(f"no string value in {payload}")


def one_character_changed(value: str) -> str:
    last = "0" if value[-1] != "0" else "1"
    return value[:-1] + last


def test_the_export_of_a_phase4_ledger_verifies_the_same_in_the_script_and_the_server(
    live: ApiClient, app_engine: Engine, owner_engine: Engine, tmp_path: Path
) -> None:
    write_the_four_types(live)
    present = {r["entry_type"] for r in rows(owner_engine, "SELECT entry_type FROM ledger_entry")}
    assert set(PHASE4_TYPES) <= present, present
    path = tmp_path / "export.json"
    entries = export(live, path)
    expected = {"valid": True, "entries_checked": len(entries), "first_mismatch": None}
    assert three_results(live, app_engine, path) == [expected, expected, expected]


@pytest.mark.parametrize("entry_type", PHASE4_TYPES)
def test_one_character_changed_in_a_phase4_payload_is_found_at_the_same_seq_by_both(
    entry_type: str,
    live: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    owner_tamper: Callable[..., None],
    tmp_path: Path,
) -> None:
    write_the_four_types(live)
    entries = export(live, tmp_path / "export.json")
    target = next(entry for entry in entries if entry["entry_type"] == entry_type)
    seq = target["seq"]
    key, value = first_string(target["payload"])
    changed = one_character_changed(value)

    # Once in a copy of the exported file.
    target["payload"][key] = changed
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    file_run = run_subprocess([str(tampered)])
    assert file_run.result is not None, file_run.stderr
    assert file_run.result["valid"] is False
    assert file_run.result["first_mismatch"] == str(seq), file_run.result

    # Once in the database: the server, the direct call and the script on the new export agree.
    assert "'" not in key + changed
    owner_tamper(
        "UPDATE ledger_entry SET payload ="
        f" jsonb_set(payload, '{{{key}}}', '{json.dumps(changed)}') WHERE seq = {int(seq)}"
    )
    path = tmp_path / "export-after.json"
    export(live, path)
    api, direct, script = three_results(live, app_engine, path)
    assert api == direct == script, (api, direct, script)
    assert api["valid"] is False and api["first_mismatch"] == str(seq), api
    assert script == file_run.result
