from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from syndcrawler.storage import PostgresCrawlStore


def _postgres_dsn() -> str:
    value = os.environ.get("SYNCRAWLER_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("SYNCRAWLER_TEST_POSTGRES_DSN is not configured")
    return value


@pytest.mark.asyncio
async def test_postgres_store_lists_recent_crawls_with_pagination() -> None:
    first_id = f"listing-a-{uuid.uuid4().hex}"
    second_id = f"listing-b-{uuid.uuid4().hex}"
    store = await PostgresCrawlStore.from_dsn(_postgres_dsn(), min_size=1, max_size=2)
    try:
        await store.create_manifest(
            first_id,
            ("https://example.com/a",),
            follow_links=True,
            same_domain=True,
            max_pages=10,
            now=10_000_000_000.0,
        )
        await store.create_manifest(
            second_id,
            ("https://example.com/b",),
            follow_links=True,
            same_domain=True,
            max_pages=10,
            now=10_000_000_001.0,
        )

        first_page = await store.recent_crawl_ids(limit=1, offset=0)
        second_page = await store.recent_crawl_ids(limit=1, offset=1)
        assert first_page == [second_id]
        assert second_page == [first_id]
        assert await store.manifest_count() >= 2

        with pytest.raises(ValueError, match="between 1 and 1000"):
            await store.recent_crawl_ids(limit=1001)
        with pytest.raises(ValueError, match="offset"):
            await store.recent_crawl_ids(offset=-1)
    finally:
        await store.close()
