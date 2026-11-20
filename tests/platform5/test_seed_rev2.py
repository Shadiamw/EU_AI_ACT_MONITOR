"""D17 (Phase 5 spec 11.5): rev-2 is seeded from REVIEWER_2_KEY only when the variable is set."""

import hashlib

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.main import create_app
from tests.api.conftest import ApiClient
from tests.part1.conftest import rows
from tests.platform5.conftest import REVIEWER_2_KEY
from tools.seed_part1 import seed

KEYS = "SELECT role, actor_id, system_id FROM api_key ORDER BY actor_id"


def _seed(owner_engine: Engine) -> None:
    with Session(owner_engine) as session:
        seed(session)
        session.commit()


def test_rev2_is_seeded_when_the_key_is_set(
    seeded: None, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEWER_2_KEY", REVIEWER_2_KEY)
    _seed(owner_engine)
    assert rows(owner_engine, KEYS) == [
        {"role": "REVIEWER", "actor_id": "rev-1", "system_id": None},
        {"role": "REVIEWER", "actor_id": "rev-2", "system_id": None},
        {"role": "SYSTEM", "actor_id": "sys-42", "system_id": "sys-42"},
        {"role": "VIEWER", "actor_id": "viewer-1", "system_id": None},
    ]
    [row] = rows(owner_engine, "SELECT key_hash FROM api_key WHERE actor_id = 'rev-2'")
    assert row["key_hash"] == hashlib.sha256(REVIEWER_2_KEY.encode("utf-8")).hexdigest()

    # rev-2 holds the REVIEWER role: a REVIEWER-only route answers it.
    with ApiClient(create_app()) as client:
        response = client.request(
            "POST",
            "/v1/ledger/verifications",
            headers={"Authorization": f"Bearer {REVIEWER_2_KEY}"},
        )
    assert response.status_code == 201, response.text


def test_rev2_is_absent_without_the_key_and_removed_when_the_key_goes(
    seeded: None, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REVIEWER_2_KEY", raising=False)
    assert [r["actor_id"] for r in rows(owner_engine, KEYS)] == ["rev-1", "sys-42", "viewer-1"]

    monkeypatch.setenv("REVIEWER_2_KEY", REVIEWER_2_KEY)
    _seed(owner_engine)
    assert "rev-2" in [r["actor_id"] for r in rows(owner_engine, KEYS)]

    # Decision 153: rows for keys no longer configured are deleted by the next seed.
    monkeypatch.setenv("REVIEWER_2_KEY", "")
    _seed(owner_engine)
    assert [r["actor_id"] for r in rows(owner_engine, KEYS)] == ["rev-1", "sys-42", "viewer-1"]
