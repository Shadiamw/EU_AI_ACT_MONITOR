"""STUB for Part 4 (seed): the spec's baseline fixtures, until Part 4's seed and reset exist.

Run `ENVIRONMENT=test python -m tools.seed_part1` after migrations. It uses DATABASE_ADMIN_URL
(the owner role) and refuses to run unless ENVIRONMENT is `test`. It writes the seed fixtures of
spec 4 ("Seed fixtures") from the environment (spec 5):

- monitored_system sys-42: sampling_interval_s = SAMPLING_INTERVAL_SECONDS (300),
  gap_timeout_intervals = GAP_TIMEOUT_INTERVALS (3), last_received_at = now. An existing row is
  left as it is.
- api_key: exactly three rows, the SHA-256 digests of INGEST_KEY_SYS_42 (SYSTEM, sys-42),
  REVIEWER_KEY (REVIEWER, rev-1) and VIEWER_KEY (VIEWER, viewer-1). Earlier rows are replaced,
  so the stored keys always match the environment.
- threshold_version, version 1: thr-1 accuracy_drift (drift) gt 0.30 and thr-2 accuracy
  (performance) lt 0.70. Existing rows are left as they are.
- ledger.ensure_seed with {systems: ["sys-42"], threshold_count: "2"}: SEED is appended only to
  an empty ledger (addendum rule 4).

Part 4 replaces this with its startup seed and POST /v1/test/reset (spec 4.6), which also
truncate the tables and set the clock offset to zero. Nothing here truncates.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import clock, config, ledger
from app.auth import key_digest
from app.ledger.canonical import Payload

SYSTEM_ID = "sys-42"
# (threshold_version_id, metric_name, metric_type, comparator, limit_value), all version 1.
THRESHOLDS = (
    ("thr-1", "accuracy_drift", "drift", "gt", "0.30"),
    ("thr-2", "accuracy", "performance", "lt", "0.70"),
)
SEED_PAYLOAD: Payload = {"systems": [SYSTEM_ID], "threshold_count": str(len(THRESHOLDS))}


def seed(session: Session) -> None:
    """Install the fixtures in the caller's transaction; the caller commits."""
    if not config.is_test_environment():
        raise RuntimeError("the Part 1 seed stand-in runs only in ENVIRONMENT=test")
    keys = (
        (config.ingest_key_sys_42(), "SYSTEM", SYSTEM_ID, SYSTEM_ID),
        (config.reviewer_key(), "REVIEWER", "rev-1", None),
        (config.viewer_key(), "VIEWER", "viewer-1", None),
    )
    now = clock.now()
    session.execute(
        text(
            "INSERT INTO monitored_system"
            " (system_id, sampling_interval_s, gap_timeout_intervals, last_received_at)"
            " VALUES (:system_id, :interval, :intervals, :now) ON CONFLICT DO NOTHING"
        ),
        {
            "system_id": SYSTEM_ID,
            "interval": config.sampling_interval_seconds(),
            "intervals": config.gap_timeout_intervals(),
            "now": now,
        },
    )
    session.execute(text("DELETE FROM api_key"))
    session.execute(
        text(
            "INSERT INTO api_key (key_hash, role, actor_id, system_id)"
            " VALUES (:key_hash, :role, :actor_id, :system_id)"
        ),
        [
            {"key_hash": key_digest(key), "role": role, "actor_id": actor, "system_id": system}
            for key, role, actor, system in keys
        ],
    )
    session.execute(
        text(
            "INSERT INTO threshold_version (threshold_version_id, system_id, metric_name,"
            " metric_type, comparator, limit_value, version, valid_from) VALUES"
            " (:id, :system_id, :name, :kind, :comparator, :limit, 1, :now)"
            " ON CONFLICT DO NOTHING"
        ),
        [
            {
                "id": version_id,
                "system_id": SYSTEM_ID,
                "name": name,
                "kind": kind,
                "comparator": comparator,
                "limit": limit,
                "now": now,
            }
            for version_id, name, kind, comparator, limit in THRESHOLDS
        ],
    )
    ledger.ensure_seed(session, SEED_PAYLOAD)


def main() -> None:
    engine = create_engine(config.database_admin_url())
    try:
        with Session(engine) as session:
            seed(session)
            session.commit()
    finally:
        engine.dispose()
    print("seed fixtures installed (sys-42, three keys from the environment, two thresholds)")


if __name__ == "__main__":
    main()
