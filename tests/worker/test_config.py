"""Part 3 settings (spec 5, 'Configuration (environment variables)')."""

import logging
from typing import Any

import pytest

from app import config, db, notifications, webhooks, worker


def test_part3_settings_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "NOTIFY_MAX_DELAY_SECONDS",
        "SWEEP_INTERVAL_SECONDS",
        "DELIVERY_POLL_SECONDS",
        "SAMPLING_INTERVAL_SECONDS",
        "GAP_TIMEOUT_INTERVALS",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    # Spec 5 defaults.
    assert config.notify_max_delay_seconds() == 900
    assert config.sweep_interval_seconds() == 60
    assert config.delivery_poll_seconds() == 5
    assert config.sampling_interval_seconds() == 300
    assert config.gap_timeout_intervals() == 3
    assert config.log_level() == "INFO"
    # Spec 3.5 and 3.6 fix these; they are not environment variables (plan D3).
    assert webhooks.TIMEOUT_SECONDS == 5
    assert notifications.MAX_ATTEMPTS == 6
    assert not hasattr(config, "webhook_timeout_seconds")
    assert not hasattr(config, "delivery_max_attempts")

    monkeypatch.setenv("NOTIFY_MAX_DELAY_SECONDS", "120")
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("DELIVERY_POLL_SECONDS", "2")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert config.notify_max_delay_seconds() == 120
    assert config.sweep_interval_seconds() == 30
    assert config.delivery_poll_seconds() == 2
    assert config.log_level() == "DEBUG"


def test_environment_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(RuntimeError, match="ENVIRONMENT"):
        config.environment()
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert config.is_test_environment()
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert not config.is_test_environment()


def test_the_worker_uses_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 5: LOG_LEVEL (default INFO) sets the worker's logging level."""
    seen: dict[str, Any] = {}
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: seen.update(kwargs))
    monkeypatch.setattr(worker, "run_forever", lambda engine: seen.setdefault("ran", True))
    monkeypatch.setattr(db, "get_engine", lambda: None)
    worker.main()
    assert seen["level"] == "WARNING"
    assert seen["ran"] is True
