from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from syndcrawler.core.change import fingerprint_page
from syndcrawler.models import PageRecord
from syndcrawler.storage import PostgresCrawlStore, PostgresRecrawlClaims


def _postgres_dsn() -> str:
    value = os.environ.get("SYNCRAWLER_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("SYNCRAWLER_TEST_POSTGRES_DSN is not configured")
    return value


def _record() -> PageRecord:
    fingerprint = fingerprint_page(
        b"<html><title>Product</title><body>one</body></html>",
        title="Product",
        links=(),
        structured={},
        headers={"etag": '"v1"'},
    )
    return PageRecord(
        url="https://example.com/product",
        status_code=200,
        content_type="text/html",
        title="Product",
        metadata={"fingerprint": fingerprint.to_dict()},
    )


@pytest.mark.asyncio
async def test_recrawl_claims_are_disjoint_expire_and_reschedule() -> None:
    crawl_id = f"claims-{uuid.uuid4().hex}"
    dsn = _postgres_dsn()
    first_store = await PostgresCrawlStore.from_dsn(dsn, min_size=1, max_size=2)
    second_store = await PostgresCrawlStore.from_dsn(dsn, min_size=1, max_size=2)
    first = PostgresRecrawlClaims(first_store)
    second = PostgresRecrawlClaims(second_store)
    try:
        await first_store.create_manifest(
            crawl_id,
            ("https://example.com",),
            follow_links=False,
            same_domain=True,
            max_pages=1,
            now=1.0,
        )
        await first_store.put_result(
            crawl_id,
            "https://example.com/product",
            _record(),
            now=2.0,
        )
        await first_store.schedule_resource(
            crawl_id,
            "https://example.com/product",
            interval_seconds=8.0,
            next_fetch_at=10.0,
        )
        await first_store.set_lifecycle(crawl_id, "completed", now=3.0)

        left, right = await asyncio.gather(
            first.claim_due(limit=1, lease_seconds=5.0, now=10.0),
            second.claim_due(limit=1, lease_seconds=5.0, now=10.0),
        )
        claimed = left + right
        assert len(claimed) == 1
        original = claimed[0]

        assert await first.claim_due(limit=1, lease_seconds=5.0, now=14.0) == []
        reclaimed = await second.claim_due(limit=1, lease_seconds=5.0, now=16.0)
        assert len(reclaimed) == 1
        assert reclaimed[0].state.request_url == original.state.request_url
        assert reclaimed[0].token != original.token

        await second.reschedule(
            reclaimed[0],
            interval_seconds=34.0,
            next_fetch_at=50.0,
        )
        assert await first.claim_due(limit=1, now=49.0) == []
        at_due = await first.claim_due(limit=1, now=50.0)
        assert len(at_due) == 1
        assert await first.release(at_due[0]) is True

        await first_store.set_lifecycle(crawl_id, "paused", now=51.0)
        assert await first.claim_due(limit=1, now=100.0) == []
    finally:
        await first_store.close()
        await second_store.close()
