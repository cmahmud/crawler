from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from syndcrawler.core.frontier import FrontierRequest, MemoryFrontier
from syndcrawler.models import PageRecord
from syndcrawler.runtime.worker import CrawlWorker


@dataclass
class _Store:
    records: list[tuple[str, str, PageRecord, float | None]] = field(default_factory=list)
    fail: bool = False

    async def put_result(
        self,
        crawl_id: str,
        request_url: str,
        record: PageRecord,
        *,
        now: float | None = None,
    ):
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.records.append((crawl_id, request_url, record, now))
        return None


@dataclass
class _Fetcher:
    records: list[PageRecord]

    async def crawl(
        self,
        seeds: list[str],
        *,
        follow_links: bool = True,
        max_pages: int | None = None,
        scope_urls: list[str] | None = None,
        request_namespace: str | None = None,
    ) -> list[PageRecord]:
        assert follow_links is False
        assert max_pages == len(seeds)
        assert scope_urls
        assert request_namespace and request_namespace.startswith("worker:test:")
        return self.records


@pytest.mark.asyncio
async def test_worker_persists_discovers_then_acks() -> None:
    frontier = MemoryFrontier()
    seed = "https://example.com/"
    child = "https://example.com/child"
    await frontier.add(FrontierRequest(seed, crawl_id="test"))
    store = _Store()
    worker = CrawlWorker(
        frontier,
        store,
        _Fetcher(
            [
                PageRecord(
                    url=seed,
                    title="Root",
                    links=(child,),
                    metadata={"requested_url": seed},
                )
            ]
        ),
    )

    result = await worker.run_batch(
        "test",
        scope_urls=(seed,),
        follow_links=True,
        limit=1,
        lease_seconds=60,
    )

    assert result.leased == 1
    assert result.persisted == 1
    assert result.failed == 0
    assert result.discovered == 1
    assert len(store.records) == 1
    assert await frontier.stats("test") == {
        "queued": 1,
        "leased": 0,
        "done": 1,
        "failed": 0,
    }

    child_lease = (await frontier.lease("test", limit=1))[0]
    assert child_lease.request.url == child
    assert child_lease.request.depth == 1
    assert child_lease.request.metadata == {"discovered_from": seed}


@pytest.mark.asyncio
async def test_worker_leaves_lease_recoverable_when_persistence_raises() -> None:
    frontier = MemoryFrontier()
    seed = "https://example.com/"
    await frontier.add(FrontierRequest(seed, crawl_id="test"))
    worker = CrawlWorker(
        frontier,
        _Store(fail=True),
        _Fetcher([PageRecord(url=seed, metadata={"requested_url": seed})]),
    )

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await worker.run_batch(
            "test",
            scope_urls=(seed,),
            follow_links=False,
            limit=1,
            lease_seconds=60,
        )

    assert await frontier.stats("test") == {
        "queued": 0,
        "leased": 1,
        "done": 0,
        "failed": 0,
    }


@pytest.mark.asyncio
async def test_worker_leaves_lease_recoverable_when_post_persist_hook_raises() -> None:
    frontier = MemoryFrontier()
    seed = "https://example.com/"
    await frontier.add(FrontierRequest(seed, crawl_id="test"))
    store = _Store()

    async def failing_hook(*_args) -> None:
        raise RuntimeError("scheduler unavailable")

    worker = CrawlWorker(
        frontier,
        store,
        _Fetcher([PageRecord(url=seed, metadata={"requested_url": seed})]),
        on_persisted=failing_hook,
    )

    with pytest.raises(RuntimeError, match="scheduler unavailable"):
        await worker.run_batch(
            "test",
            scope_urls=(seed,),
            follow_links=False,
            limit=1,
            lease_seconds=60,
        )

    assert len(store.records) == 1
    assert (await frontier.stats("test"))["leased"] == 1


@pytest.mark.asyncio
async def test_worker_marks_missing_fetch_result_failed() -> None:
    frontier = MemoryFrontier()
    seed = "https://example.com/"
    await frontier.add(FrontierRequest(seed, crawl_id="test"))
    worker = CrawlWorker(frontier, _Store(), _Fetcher([]))

    result = await worker.run_batch(
        "test",
        scope_urls=(seed,),
        follow_links=False,
        limit=1,
        lease_seconds=60,
    )

    assert result == result.__class__(leased=1, persisted=0, failed=1, discovered=0)
    assert await frontier.stats("test") == {
        "queued": 0,
        "leased": 0,
        "done": 0,
        "failed": 1,
    }
