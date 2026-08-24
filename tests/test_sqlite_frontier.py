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


@pytest.mark.asyncio
async def test_sqlite_crawl_limit_counts_first_leases_not_retries(tmp_path: Path) -> None:
    path = tmp_path / "frontier.sqlite3"
    frontier = SQLiteFrontier(path)
    await frontier.set_crawl_limit("limited", 2)
    await frontier.add(
        FrontierRequest("https://a.example/page", crawl_id="limited"),
        FrontierRequest("https://b.example/page", crawl_id="limited"),
        FrontierRequest("https://c.example/page", crawl_id="limited"),
    )

    first = await frontier.lease("limited", limit=2, now=100, lease_seconds=10)
    assert len(first) == 2
    await frontier.retry(first[0], available_at=100, error="temporary")
    await frontier.ack(first[1])

    retried = await frontier.lease("limited", limit=2, now=100, lease_seconds=10)
    assert len(retried) == 1
    assert retried[0].request.url == first[0].request.url
    assert retried[0].attempt == 2
    await frontier.ack(retried[0])

    assert await frontier.lease("limited", limit=2, now=100, lease_seconds=10) == []
    assert (await frontier.stats("limited"))["queued"] == 1
    await frontier.close()


@pytest.mark.asyncio
async def test_sqlite_crawl_limit_reconstructs_usage_for_existing_state(tmp_path: Path) -> None:
    path = tmp_path / "frontier.sqlite3"
    frontier = SQLiteFrontier(path)
    await frontier.add(
        FrontierRequest("https://a.example/page", crawl_id="migrate"),
        FrontierRequest("https://b.example/page", crawl_id="migrate"),
    )
    first = (await frontier.lease("migrate", limit=1, now=100, lease_seconds=10))[0]
    await frontier.ack(first)
    await frontier.close()

    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM frontier_crawls WHERE crawl_id = ?", ("migrate",))
    connection.commit()
    connection.close()

    resumed = SQLiteFrontier(path)
    await resumed.set_crawl_limit("migrate", 1)
    assert await resumed.lease("migrate", limit=1, now=100, lease_seconds=10) == []
    assert (await resumed.stats("migrate"))["queued"] == 1
    await resumed.close()
