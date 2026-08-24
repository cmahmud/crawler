from pathlib import Path

import pytest

from syndcrawler.core.change import ChangeKind, fingerprint_page
from syndcrawler.models import PageRecord
from syndcrawler.storage import SQLiteCrawlStore


def _record(body: bytes, *, title: str, etag: str) -> PageRecord:
    fingerprint = fingerprint_page(
        body,
        title=title,
        links=(),
        structured={},
        headers={"etag": etag},
    )
    return PageRecord(
        url="https://example.com/product",
        status_code=200,
        content_type="text/html",
        title=title,
        metadata={"fingerprint": fingerprint.to_dict()},
    )


@pytest.mark.asyncio
async def test_resource_state_tracks_representation_and_semantic_changes(
    tmp_path: Path,
) -> None:
    store = SQLiteCrawlStore(tmp_path / "crawl.sqlite3")
    try:
        await store.create_manifest(
            "changes",
            ("https://example.com",),
            follow_links=True,
            same_domain=True,
            max_pages=10,
            now=1.0,
        )

        first = await store.put_result(
            "changes",
            "https://example.com/product",
            _record(b"one", title="Product", etag='"v1"'),
            now=2.0,
        )
        assert first is not None
        assert first.kind is ChangeKind.NEW

        representation = await store.put_result(
            "changes",
            "https://example.com/product",
            _record(b"markup changed", title="Product", etag='"v2"'),
            now=3.0,
        )
        assert representation is not None
        assert representation.kind is ChangeKind.REPRESENTATION_CHANGED

        semantic = await store.put_result(
            "changes",
            "https://example.com/product",
            _record(b"new product", title="Product 2", etag='"v3"'),
            now=4.0,
        )
        assert semantic is not None
        assert semantic.kind is ChangeKind.SEMANTIC_CHANGED

        state = await store.get_resource_state(
            "changes",
            "https://example.com/product",
        )
        assert state is not None
        assert state.first_seen_at == 2.0
        assert state.last_seen_at == 4.0
        assert state.last_changed_at == 4.0
        assert state.change_count == 2
        assert state.fingerprint.validators.etag == '"v3"'
        assert state.last_change_kind is ChangeKind.SEMANTIC_CHANGED

        unchanged = await store.mark_not_modified(
            "changes",
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
async def test_resource_state_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "crawl.sqlite3"
    first = SQLiteCrawlStore(path)
    await first.create_manifest(
        "persist",
        ("https://example.com",),
        follow_links=False,
        same_domain=True,
        max_pages=1,
        now=1.0,
    )
    await first.put_result(
        "persist",
        "https://example.com/product",
        _record(b"one", title="Product", etag='"v1"'),
        now=2.0,
    )
    await first.close()

    second = SQLiteCrawlStore(path)
    try:
        state = await second.get_resource_state(
            "persist",
            "https://example.com/product",
        )
        assert state is not None
        assert state.first_seen_at == 2.0
        assert state.last_seen_at == 2.0
        assert state.change_count == 0
        assert state.last_change_kind is ChangeKind.NEW
    finally:
        await second.close()
