"""Phase 5 Part 3, M6 follow-up: concurrent resets do not deadlock (spec 6.5; decision 300).

reset() reads platform_counter.reset_count before its TRUNCATE and writes count + 1 after it.
Fifteen POST /v1/test/reset calls, released together by a barrier against the real app, must
all return 204, and reset_count must rise by exactly fifteen.
"""

import threading
from typing import Any

from sqlalchemy import Engine, text

from tests.api.conftest import ApiClient
from tests.db_helpers import bearer

RESETS = 15


def reset_count(owner_engine: Engine) -> int:
    with owner_engine.connect() as conn:
        value = conn.execute(text("SELECT reset_count FROM platform_counter")).scalar()
    return 0 if value is None else int(value)


def test_concurrent_resets_do_not_deadlock(client: ApiClient, owner_engine: Engine) -> None:
    before = reset_count(owner_engine)
    barrier = threading.Barrier(RESETS)
    results: list[Any] = [None] * RESETS

    def reset(index: int) -> None:
        try:
            barrier.wait()
            response = client.request("POST", "/v1/test/reset", headers=bearer("rev-1"))
            results[index] = response.status_code
        except BaseException as exc:  # reported below, so a failure is never silent
            results[index] = exc

    threads = [threading.Thread(target=reset, args=(i,)) for i in range(RESETS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert results == [204] * RESETS
    assert reset_count(owner_engine) == before + RESETS
