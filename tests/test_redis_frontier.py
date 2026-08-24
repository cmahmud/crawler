from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

pytest.importorskip("redis")

from syndcrawler.core.frontier import FrontierRequest, QueuePolicy
from syndcrawler.storage import RedisFrontier


def _redis_url() -> str:
    value = os.environ.get("SYNCRAWLER_TEST_REDIS_URL")
    if not value:
        pytest.skip("SYNCRAWLER_TEST_REDIS_URL is not configured")
    return value


def _frontier() -> RedisFrontier:
    return RedisFrontier.from_url(
        _redis_url(),
        key_prefix=f"syndcrawler-test:{uuid.uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_redis_frontier_deduplicates_and_orders_by_priority() -> None:
    frontier = _frontier()
    crawl_id = "priority"
    try:
        added = await frontier.add(
            FrontierRequest(
                "https://low.example/a",
                crawl_id=crawl_id,
                priority=1,
                metadata={"kind": "low"},
            ),
            FrontierRequest(
                "https://high.example/b",
                crawl_id=crawl_id,
                priority=10,
                metadata={"kind": "high"},
            ),
            FrontierRequest(
                "https://low.example/a",
                crawl_id=crawl_id,
                priority=99,
            ),
        )
        assert added == 2

        leases = await frontier.lease(crawl_id, limit=2, now=100.0)
        assert [lease.request.url for lease in leases] == [
            "https://high.example/b",
            "https://low.example/a",
        ]
        assert leases[0].request.metadata == {"kind": "high"}

        for lease in leases:
            await frontier.ack(lease)
        assert await frontier.stats(crawl_id) == {
            "queued": 0,
            "leased": 0,
            "done": 2,
            "failed": 0,
        }
    finally:
        await frontier.purge(crawl_id)
        await frontier.close()


@pytest.mark.asyncio
async def test_due_high_priority_is_not_hidden_by_large_ready_backlog() -> None:
    frontier = _frontier()
    crawl_id = "priority-promotion"
    due_at = time.time() + 100
    low_priority = [
        FrontierRequest(
            f"https://low{i}.example/page",
            crawl_id=crawl_id,
            priority=0,
        )
        for i in range(1200)
    ]
    high_priority = FrontierRequest(
        "https://priority.example/page",
        crawl_id=crawl_id,
        priority=100,
        available_at=due_at,
    )
    try:
        assert await frontier.add(*low_priority, high_priority) == 1201
        lease = (await frontier.lease(crawl_id, limit=1, now=due_at))[0]
        assert lease.request.url == "https://priority.example/page"
        await frontier.ack(lease)
    finally:
        await frontier.purge(crawl_id)
        await frontier.close()


@pytest.mark.asyncio
async def test_redis_frontier_reclaims_expired_lease_across_clients() -> None:
    prefix = f"syndcrawler-test:{uuid.uuid4().hex}"
    first = RedisFrontier.from_url(_redis_url(), key_prefix=prefix)
    second = RedisFrontier.from_url(_redis_url(), key_prefix=prefix)
    crawl_id = "reclaim"
    try:
        await first.add(FrontierRequest("https://example.com/a", crawl_id=crawl_id))
        original = (await first.lease(crawl_id, now=100.0, lease_seconds=5.0))[0]

        assert await second.lease(crawl_id, now=104.0, lease_seconds=5.0) == []
        reclaimed = (await second.lease(crawl_id, now=106.0, lease_seconds=5.0))[0]
        assert reclaimed.request.url == original.request.url
        assert reclaimed.attempt == 2
        assert reclaimed.token != original.token

        await second.ack(reclaimed)
        assert (await second.stats(crawl_id))["done"] == 1
    finally:
        await first.purge(crawl_id)
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_redis_frontier_preserves_host_delay_and_concurrency() -> None:
    frontier = _frontier()
    crawl_id = "politeness"
    try:
        await frontier.set_queue_policy(
            crawl_id,
            "example.com",
            QueuePolicy(max_concurrency=2, delay_seconds=10.0),
        )
        await frontier.add(
            FrontierRequest("https://example.com/a", crawl_id=crawl_id),
            FrontierRequest("https://example.com/b", crawl_id=crawl_id),
        )

        first = await frontier.lease(crawl_id, limit=2, now=100.0)
        assert len(first) == 1
        assert await frontier.lease(crawl_id, limit=2, now=105.0) == []

        second = await frontier.lease(crawl_id, limit=2, now=111.0)
        assert len(second) == 1
        assert second[0].request.url != first[0].request.url

        await frontier.ack(first[0])
        await frontier.ack(second[0])
    finally:
        await frontier.purge(crawl_id)
        await frontier.close()


@pytest.mark.asyncio
async def test_redis_frontier_multi_client_leases_are_disjoint() -> None:
    prefix = f"syndcrawler-test:{uuid.uuid4().hex}"
    first = RedisFrontier.from_url(_redis_url(), key_prefix=prefix)
    second = RedisFrontier.from_url(_redis_url(), key_prefix=prefix)
    crawl_id = "multi-client"
    requests = [
        FrontierRequest(f"https://host{i}.example/page", crawl_id=crawl_id)
        for i in range(10)
    ]
    try:
        assert await first.add(*requests) == 10
        left, right = await asyncio.gather(
            first.lease(crawl_id, limit=5, now=100.0),
            second.lease(crawl_id, limit=5, now=100.0),
        )
        urls = [lease.request.url for lease in left + right]
        assert len(urls) == 10
        assert len(set(urls)) == 10

        await asyncio.gather(
            *(first.ack(lease) for lease in left),
            *(second.ack(lease) for lease in right),
        )
        assert (await first.stats(crawl_id))["done"] == 10
    finally:
        await first.purge(crawl_id)
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_redis_frontier_retry_respects_available_at() -> None:
    frontier = _frontier()
    crawl_id = "retry"
    try:
        await frontier.add(FrontierRequest("https://example.com/a", crawl_id=crawl_id))
        lease = (await frontier.lease(crawl_id, now=100.0))[0]
        await frontier.retry(lease, available_at=150.0, error="temporary")

        assert await frontier.lease(crawl_id, now=149.0) == []
        retry = (await frontier.lease(crawl_id, now=150.0))[0]
        assert retry.attempt == 2
        await frontier.fail(retry, error="permanent")
        assert await frontier.stats(crawl_id) == {
            "queued": 0,
            "leased": 0,
            "done": 0,
            "failed": 1,
        }
    finally:
        await frontier.purge(crawl_id)
        await frontier.close()


@pytest.mark.asyncio
async def test_redis_crawl_limit_is_atomic_across_clients() -> None:
    prefix = f"syndcrawler-test:{uuid.uuid4().hex}"
    first = RedisFrontier.from_url(_redis_url(), key_prefix=prefix)
    second = RedisFrontier.from_url(_redis_url(), key_prefix=prefix)
    crawl_id = "global-limit"
    requests = [
        FrontierRequest(f"https://budget{i}.example/page", crawl_id=crawl_id)
        for i in range(10)
    ]
    try:
        await first.set_crawl_limit(crawl_id, 3)
        assert await first.add(*requests) == 10
        left, right = await asyncio.gather(
            first.lease(crawl_id, limit=5, now=100.0),
            second.lease(crawl_id, limit=5, now=100.0),
        )
        leases = left + right
        assert len(leases) == 3
        assert len({lease.request.url for lease in leases}) == 3

        await asyncio.gather(
            *(first.ack(lease) for lease in left),
            *(second.ack(lease) for lease in right),
        )
        assert await first.lease(crawl_id, limit=5, now=100.0) == []
        assert (await first.stats(crawl_id))["queued"] == 7
    finally:
        await first.purge(crawl_id)
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_redis_crawl_limit_allows_retry_after_budget_is_full() -> None:
    frontier = _frontier()
    crawl_id = "global-limit-retry"
    try:
        await frontier.set_crawl_limit(crawl_id, 1)
        await frontier.add(
            FrontierRequest("https://a.example/page", crawl_id=crawl_id),
            FrontierRequest("https://b.example/page", crawl_id=crawl_id),
        )
        first = (await frontier.lease(crawl_id, limit=1, now=100.0))[0]
        await frontier.retry(first, available_at=100.0, error="temporary")

        retry = (await frontier.lease(crawl_id, limit=2, now=100.0))[0]
        assert retry.request.url == first.request.url
        assert retry.attempt == 2
        await frontier.ack(retry)

        assert await frontier.lease(crawl_id, limit=2, now=100.0) == []
        assert (await frontier.stats(crawl_id))["queued"] == 1
    finally:
        await frontier.purge(crawl_id)
        await frontier.close()
