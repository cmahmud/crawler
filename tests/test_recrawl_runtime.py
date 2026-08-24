from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from syndcrawler.config import CrawlConfig
from syndcrawler.core.change import ChangeKind
from syndcrawler.core.recrawl import RecrawlPolicy
from syndcrawler.runtime import RecrawlRunner, ResumableCrawler
from syndcrawler.storage import SQLiteCrawlStore


class _MutablePage:
    etag = '"v1"'
    title = "Version 1"


class _RecrawlHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return

        if self.headers.get("If-None-Match") == _MutablePage.etag:
            self.send_response(304)
            self.send_header("ETag", _MutablePage.etag)
            self.end_headers()
            return

        body = (
            "<!doctype html><html><head>"
            f"<title>{_MutablePage.title}</title>"
            "</head><body>"
            f"<main><h1>{_MutablePage.title}</h1></main>"
            "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("ETag", _MutablePage.etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def mutable_site():
    _MutablePage.etag = '"v1"'
    _MutablePage.title = "Version 1"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecrawlHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.asyncio
async def test_recrawl_304_then_semantic_change(
    tmp_path: Path,
    mutable_site: str,
) -> None:
    config = CrawlConfig(
        max_pages=1,
        max_concurrency=1,
        max_retries=0,
        respect_robots_txt=False,
        allow_private_networks=True,
        sitemap_discovery_enabled=False,
    )
    policy = RecrawlPolicy(
        min_interval_seconds=1,
        base_interval_seconds=10,
        max_interval_seconds=100,
        stable_multiplier=2,
    )
    crawler = ResumableCrawler(
        config,
        state_dir=tmp_path,
        recrawl_policy=policy,
    )
    first = await crawler.start(
        [mutable_site],
        crawl_id="recrawl-test",
        follow_links=False,
        max_pages=1,
    )
    assert first.records[0].title == "Version 1"

    runner = RecrawlRunner(config, state_dir=tmp_path, policy=policy)
    unchanged = await runner.run("recrawl-test", force=True)
    assert unchanged.checked == 1
    assert unchanged.not_modified == 1
    assert unchanged.items[0].change_kind is ChangeKind.NOT_MODIFIED

    store = SQLiteCrawlStore(tmp_path / "recrawl-test.sqlite3")
    try:
        state = await store.get_resource_state("recrawl-test", mutable_site)
        assert state is not None
        assert state.recrawl_interval_seconds == 20
    finally:
        await store.close()

    _MutablePage.etag = '"v2"'
    _MutablePage.title = "Version 2"

    changed = await runner.run("recrawl-test", force=True)
    assert changed.checked == 1
    assert changed.semantic_changed == 1
    assert changed.items[0].change_kind is ChangeKind.SEMANTIC_CHANGED

    store = SQLiteCrawlStore(tmp_path / "recrawl-test.sqlite3")
    try:
        state = await store.get_resource_state("recrawl-test", mutable_site)
        assert state is not None
        assert state.recrawl_interval_seconds == 1
        assert state.change_count == 1
        records = await store.results("recrawl-test")
        assert len(records) == 1
        assert records[0].title == "Version 2"
        assert records[0].metadata["recrawl"] is True
    finally:
        await store.close()
