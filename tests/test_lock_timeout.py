"""LOCK_TIMEOUT is applied only when it is set, and only as a single timeout (decision 110)."""

import pytest

from app.db import connect_args


def test_no_lock_timeout_unless_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCK_TIMEOUT", raising=False)
    assert connect_args() == {}


def test_lock_timeout_is_one_postgres_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCK_TIMEOUT", "5s")
    assert connect_args() == {"options": "-c lock_timeout=5s"}


@pytest.mark.parametrize("value", ["0s", "5", "5 seconds", "5s -c statement_timeout=1s"])
def test_lock_timeout_rejects_anything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LOCK_TIMEOUT", value)
    with pytest.raises(RuntimeError):
        connect_args()
