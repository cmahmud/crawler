from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

pytest.importorskip("crawlee")

from syndcrawler.config import CrawlConfig
from syndcrawler.runtime import LocalCrawler


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/":
            self._html(
                "<html><head><title>Root</title></head>"
                '<body><a href="/next">Next</a></body></html>'
            )
            return
        if self.path == "/next":
            self._html("<html><head><title>Next</title></head><body>done</body></html>")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _html(self, body: str) -> None:
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.asyncio
async def test_local_runtime_crawls_links_with_impit(local_site: str) -> None:
    crawler = LocalCrawler(
        CrawlConfig(
            max_pages=2,
            max_concurrency=1,
            max_retries=0,
            respect_robots_txt=False,
            allow_private_networks=True,
        )
    )

    records = await crawler.crawl([local_site])
    assert [record.title for record in records] == ["Root", "Next"]
    assert records[0].engine == "http"
    assert records[0].route == "direct"
    assert f"{local_site}/next" in records[0].links


@pytest.mark.asyncio
async def test_scrape_does_not_follow_links(local_site: str) -> None:
    crawler = LocalCrawler(
        CrawlConfig(
            max_pages=10,
            respect_robots_txt=False,
            allow_private_networks=True,
        )
    )

    record = await crawler.scrape(local_site)
    assert record is not None
    assert record.title == "Root"
