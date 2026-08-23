import pytest

from syndcrawler.core.frontier import FrontierRequest, MemoryFrontier, QueuePolicy


@pytest.mark.asyncio
async def test_frontier_dedupes_and_leases_by_priority() -> None:
    frontier = MemoryFrontier()
    assert await frontier.add(
        FrontierRequest("https://example.com/a", priority=1),
        FrontierRequest("https://example.com/b", priority=10),
        FrontierRequest("https://example.com/a", priority=99),
    ) == 2

    await frontier.set_queue_policy(
        "default",
        "example.com",
        QueuePolicy(max_concurrency=2),
    )
    leases = await frontier.lease("default", limit=2, now=100, lease_seconds=30)
    assert [lease.request.url for lease in leases] == [
        "https://example.com/b",
        "https://example.com/a",
    ]


@pytest.mark.asyncio
async def test_frontier_reclaims_expired_lease() -> None:
    frontier = MemoryFrontier()
    await frontier.add(FrontierRequest("https://example.com/a"))
    first = (await frontier.lease("default", now=100, lease_seconds=5))[0]
    second = (await frontier.lease("default", now=106, lease_seconds=5))[0]
    assert first.token != second.token
    assert second.attempt == 2


@pytest.mark.asyncio
async def test_queue_policy_enforces_concurrency_and_delay() -> None:
    frontier = MemoryFrontier()
    await frontier.add(
        FrontierRequest("https://example.com/a"),
        FrontierRequest("https://example.com/b"),
    )
    await frontier.set_queue_policy(
        "default",
        "example.com",
        QueuePolicy(max_concurrency=1, delay_seconds=2),
    )

    first = await frontier.lease("default", limit=2, now=100, lease_seconds=10)
    assert len(first) == 1
    await frontier.ack(first[0])
    assert await frontier.lease("default", now=101, lease_seconds=10) == []
    assert len(await frontier.lease("default", now=102, lease_seconds=10)) == 1
