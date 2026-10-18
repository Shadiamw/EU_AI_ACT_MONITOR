"""Explicit local/test fixtures; production provisioning remains with Part 4.

Run `ENVIRONMENT=test python -m tools.seed_part1` after migrations.
"""

import os
from datetime import UTC, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import clock, config
from app.auth import key_digest

TEST_KEYS = {
    "SYSTEM": "local-test-system-sys42-9e4bb169f4584fa69d92",
    "REVIEWER": "local-test-reviewer-rev1-830607463f814883a285",
    "VIEWER": "local-test-viewer-1-6f57355ca3764cce9acf",
}


def seed(session: Session) -> None:
    """Idempotently provision the published test fixtures, without committing."""
    if os.environ.get("ENVIRONMENT") != "test":
        raise RuntimeError("Part 1 demo fixtures are available only in ENVIRONMENT=test")
    session.execute(
        text(
            "INSERT INTO monitored_system (system_id, monitoring_started_at)"
            " VALUES ('sys-42', :now) ON CONFLICT DO NOTHING"
        ),
        {"now": clock.now()},
    )
    for role, key in TEST_KEYS.items():
        actor = {"SYSTEM": "sys-42", "REVIEWER": "rev-1", "VIEWER": "viewer-1"}[role]
        session.execute(
            text(
                "INSERT INTO api_key (key_hash, role, actor_id, system_id)"
                " VALUES (:hash, :role, :actor, :system) ON CONFLICT DO NOTHING"
            ),
            {
                "hash": key_digest(key),
                "role": role,
                "actor": actor,
                "system": "sys-42" if role == "SYSTEM" else None,
            },
        )
    for version, name, kind, comparator, value in (
        ("thr-1", "accuracy_drift", "drift", "gt", "0.30"),
        ("thr-2", "accuracy", "performance", "lt", "0.80"),
    ):
        session.execute(
            text(
                "INSERT INTO threshold_version (threshold_version_id, system_id,"
                " metric_name, metric_type, comparator, threshold_value, effective_at)"
                " VALUES (:id, 'sys-42', :name, :kind, :comparator, :value, :start)"
                " ON CONFLICT DO NOTHING"
            ),
            {
                "id": version,
                "name": name,
                "kind": kind,
                "comparator": comparator,
                "value": value,
                "start": datetime(2000, 1, 1, tzinfo=UTC),
            },
        )


def main() -> None:
    engine = create_engine(config.database_admin_url())
    try:
        with Session(engine) as session:
            seed(session)
            session.commit()
    finally:
        engine.dispose()
    print("Part 1 local test fixtures provisioned; credentials are in docs/part1_api.md")


if __name__ == "__main__":
    main()
