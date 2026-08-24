from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from syndcrawler.core.change import ChangeKind, fingerprint_page
from syndcrawler.models import PageRecord
from syndcrawler.storage import PostgresCrawlStore


def _postgres_dsn() -> str:
    value = os.environ.get("SYNCRAWLER_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("SYNCRAWLER_TEST_POSTGRES_DSN is not configured")
    return value


def _record(body: bytes, *, title: str, etag: str) -> PageRecord:
    fingerprint = fingerprint_page(
        body,
        title=title,
        links=("https://example.com/child",),
        structured={"kind": "product"},
        headers={"etag": etag},
    )
    return PageRecord(
        url="https://example.com/product",
        status_code=200,
        content_type="text/html",
        title=title,
        links=("https://example.com/child",),
        metadata={"fingerprint": fingerprint.to_dict()},
    )


@pytest.mark.asyncio
async def test_postgres_store_concurrent_initialization_is_safe() -> None:
    first, second = await asyncio.gather(
        PostgresCrawlStore.from_dsn(_postgres_dsn(), min_size=1, max_size=1),
        PostgresCrawlStore.from_dsn(_postgres_dsn(), min_size=1, max_size=1),
    )
    try:
        assert await first.ping() is True
        assert await second.ping() is True
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_postgres_store_persists_manifest_results_and_schedule() -> None:
    crawl_id = f"pg-{uuid.uuid4().hex}"
    dsn = _postgres_dsn()
    first = await PostgresCrawlStore.from_dsn(dsn, min_size=1, max_size=2)
    try:
        assert await first.ping() is True
        manifest = await first.create_manifest(
            crawl_id,
            ("https://example.com",),
            follow_links=True,
            same_domain=True,
            max_pages=10,
            now=1.0,
        )
        assert manifest.crawl_id == crawl_id
        assert await first.get_lifecycle(crawl_id) == "active"

        assessment = await first.put_result(
            crawl_id,
            "https://example.com/product",
            _record(b"one", title="Product", etag='"v1"'),
            now=2.0,
        )
        assert assessment is not None
        assert assessment.kind is ChangeKind.NEW

        scheduled = await first.schedule_resource(
            crawl_id,
            "https://example.com/product",
            interval_seconds=30.0,
            next_fetch_at=32.0,
        )
        assert scheduled.recrawl_interval_seconds == 30.0
        assert scheduled.next_fetch_at == 32.0
        assert await first.due_resources(crawl_id, now=31.0) == []
        due = await first.due_resources(crawl_id, now=32.0)
        assert [state.request_url for state in due] == ["https://example.com/product"]
    finally:
        await first.close()

    second = await PostgresCrawlStore.from_dsn(dsn, min_size=1, max_size=2)
    try:
        manifest = await second.get_manifest(crawl_id)
        assert manifest is not None
        assert manifest.seeds == ("https://example.com",)
        assert manifest.follow_links is True
        assert manifest.max_pages == 10
        assert await second.get_lifecycle(crawl_id) == "active"

        state = await second.get_resource_state(
            crawl_id,
            "https://example.com/product",
        )
        assert state is not None
        assert state.fingerprint.validators.etag == '"v1"'
        assert state.next_fetch_at == 32.0

        results = await second.results(crawl_id)
        assert len(results) == 1
        assert results[0].title == "Product"
        assert await second.result_count(crawl_id) == 1
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_postgres_store_tracks_changes_and_not_modified() -> None:
    crawl_id = f"pg-{uuid.uuid4().hex}"
    store = await PostgresCrawlStore.from_dsn(_postgres_dsn(), min_size=1, max_size=2)
    try:
        await store.create_manifest(
            crawl_id,
            ("https://example.com",),
            follow_links=False,
            same_domain=True,
            max_pages=1,
            now=1.0,
        )

        first = await store.put_result(
            crawl_id,
            "https://example.com/product",
            _record(b"one", title="Product", etag='"v1"'),
            now=2.0,
        )
        assert first is not None and first.kind is ChangeKind.NEW

        representation = await store.put_result(
            crawl_id,
            "https://example.com/product",
            _record(b"markup changed", title="Product", etag='"v2"'),
            now=3.0,
        )
        assert representation is not None
        assert representation.kind is ChangeKind.REPRESENTATION_CHANGED

        semantic = await store.put_result(
            crawl_id,
            "https://example.com/product",
            _record(b"new product", title="Product 2", etag='"v3"'),
            now=4.0,
        )
        assert semantic is not None
        assert semantic.kind is ChangeKind.SEMANTIC_CHANGED

        state = await store.get_resource_state(
            crawl_id,
            "https://example.com/product",
        )
        assert state is not None
        assert state.first_seen_at == 2.0
        assert state.last_seen_at == 4.0
        assert state.last_changed_at == 4.0
        assert state.change_count == 2
        assert state.last_change_kind is ChangeKind.SEMANTIC_CHANGED

        unchanged = await store.mark_not_modified(
            crawl_id,
            "https://example.com/product",
            now=5.0,
        )
        assert unchanged.last_seen_at == 5.0
        assert unchanged.last_changed_at == 4.0
        assert unchanged.change_count == 2
        assert unchanged.last_change_kind is ChangeKind.NOT_MODIFIED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_store_lifecycle_and_runnable_query() -> None:
    crawl_id = f"pg-{uuid.uuid4().hex}"
    store = await PostgresCrawlStore.from_dsn(_postgres_dsn(), min_size=1, max_size=2)
    try:
        await store.create_manifest(
            crawl_id,
            ("https://example.com",),
            follow_links=True,
            same_domain=True,
            max_pages=5,
            now=1.0,
        )
        assert crawl_id in await store.runnable_crawl_ids(limit=1000)

        assert await store.set_lifecycle(crawl_id, "paused", now=2.0) == "paused"
        assert crawl_id not in await store.runnable_crawl_ids(limit=1000)

        assert await store.set_lifecycle(crawl_id, "active", now=3.0) == "active"
        assert crawl_id in await store.runnable_crawl_ids(limit=1000)

        assert await store.set_lifecycle(crawl_id, "completed", now=4.0) == "completed"
        assert crawl_id not in await store.runnable_crawl_ids(limit=1000)
        with pytest.raises(ValueError, match="unsupported"):
            await store.set_lifecycle(crawl_id, "unknown")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_store_paginates_results() -> None:
    crawl_id = f"pg-{uuid.uuid4().hex}"
    store = await PostgresCrawlStore.from_dsn(_postgres_dsn(), min_size=1, max_size=2)
    try:
        await store.create_manifest(
            crawl_id,
            ("https://example.com",),
            follow_links=False,
            same_domain=True,
            max_pages=3,
            now=1.0,
        )
        for index in range(3):
            await store.put_result(
                crawl_id,
                f"https://example.com/{index}",
                _record(
                    f"body-{index}".encode(),
                    title=f"Product {index}",
                    etag=f'"v{index}"',
                ),
                now=2.0 + index,
            )

        first = await store.results_page(crawl_id, limit=2, offset=0)
        second = await store.results_page(crawl_id, limit=2, offset=2)
        assert [record.title for record in first] == ["Product 0", "Product 1"]
        assert [record.title for record in second] == ["Product 2"]
        with pytest.raises(ValueError, match="between 1 and 1000"):
            await store.results_page(crawl_id, limit=1001)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_store_rejects_duplicate_manifest() -> None:
    crawl_id = f"pg-{uuid.uuid4().hex}"
    store = await PostgresCrawlStore.from_dsn(_postgres_dsn(), min_size=1, max_size=2)
    try:
        await store.create_manifest(
            crawl_id,
            ("https://example.com",),
            follow_links=False,
            same_domain=True,
            max_pages=1,
        )
        with pytest.raises(ValueError, match="crawl already exists"):
            await store.create_manifest(
                crawl_id,
                ("https://example.com",),
                follow_links=False,
                same_domain=True,
                max_pages=1,
            )
    finally:
        await store.close()
