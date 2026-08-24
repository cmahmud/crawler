from pathlib import Path

import pytest

from syndcrawler.core.frontier import FrontierRequest, QueuePolicy
from syndcrawler.storage import SQLiteFrontier


@pytest.mark.asyncio
async def test_sqlite_frontier_persists_and_reclaims_leases(tmp_path: Path) -> None:
    path = tmp_path / "frontier.sqlite3"
    first = SQLiteFrontier(path)
    assert await first.add(
        FrontierRequest("https://example.com/a", priority=1, metadata={"kind": "a"}),
        FrontierRequest("https://example.com/b", priority=10),
        FrontierRequest("https://example.com/a", priority=99),
    ) == 2

    leases = await first.lease("default", limit=1, now=100, lease_seconds=5)
    assert len(leases) == 1
    assert leases[0].request.url == "https://example.com/b"
    assert leases[0].attempt == 1
    await first.close()

    resumed = SQLiteFrontier(path)
    reclaimed = await resumed.lease("default", limit=1, now=106, lease_seconds=5)
    assert len(reclaimed) == 1
    assert reclaimed[0].request.url == "https://example.com/b"
    assert reclaimed[0].attempt == 2
    await resumed.ack(reclaimed[0])

    remaining = await resumed.lease("default", limit=1, now=106, lease_seconds=5)
    assert len(remaining) == 1
    assert remaining[0].request.url == "https://example.com/a"
    assert remaining[0].request.metadata == {"kind": "a"}

    stats = await resumed.stats("default")
    assert stats == {"queued": 0, "leased": 1, "done": 1, "failed": 0}
    await resumed.close()


@pytest.mark.asyncio
async def test_sqlite_frontier_persists_queue_policy_and_retry_time(tmp_path: Path) -> None:
    path = tmp_path / "frontier.sqlite3"
    frontier = SQLiteFrontier(path)
    await frontier.add(
        FrontierRequest("https://example.com/a"),
        FrontierRequest("https://example.com/b"),
    )
    await frontier.set_queue_policy(
        "default",
        "example.com",
        QueuePolicy(max_concurrency=1, delay_seconds=2),
    )

    first = (await frontier.lease("default", limit=2, now=100, lease_seconds=10))[0]
    await frontier.retry(first, available_at=105, error="temporary")
    await frontier.close()

    resumed = SQLiteFrontier(path)
    assert await resumed.lease("default", limit=2, now=101, lease_seconds=10) == []

    second = await resumed.lease("default", limit=2, now=102, lease_seconds=10)
    assert len(second) == 1
    assert second[0].request.url == "https://example.com/b"
    await resumed.ack(second[0])

    retried = await resumed.lease("default", limit=2, now=105, lease_seconds=10)
    assert len(retried) == 1
    assert retried[0].request.url == "https://example.com/a"
    assert retried[0].attempt == 2
    await resumed.close()
