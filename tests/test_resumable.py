from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from syndcrawler.config import CrawlConfig
from syndcrawler.runtime import ResumableCrawler


class _SiteHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        origin = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        if self.path == "/robots.txt":
            body = f"User-agent: *\nSitemap: {origin}/sitemap.xml\n"
            self._send(200, body, "text/plain")
            return
        if self.path == "/sitemap.xml":
            body = (
                "<urlset>"
                f"<url><loc>{origin}/hidden</loc></url>"
                "</urlset>"
            )
            self._send(200, body, "application/xml")
            return

        pages = {
            "/": (
                "<html><head><title>Root</title></head><body>"
                '<a href="/a">A</a><a href="/b">B</a>'
                "</body></html>"
            ),
            "/a": "<html><head><title>A</title></head><body>A page</body></html>",
            "/b": "<html><head><title>B</title></head><body>B page</body></html>",
            "/sitemap-only": (
                "<html><head><title>Sitemap Seed</title></head>"
                "<body>No links here.</body></html>"
            ),
            "/hidden": (
                "<html><head><title>Hidden</title></head>"
                "<body>Only listed in sitemap.</body></html>"
            ),
        }
        body = pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self._send(200, body, "text/html; charset=utf-8")

    def _send(self, status: int, body: str, content_type: str) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
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
async def test_resumable_crawl_persists_results_and_resume_is_idempotent(
    tmp_path: Path,
    local_site: str,
) -> None:
    crawler = ResumableCrawler(
        CrawlConfig(
            max_pages=3,
            max_concurrency=2,
            max_retries=0,
            respect_robots_txt=False,
            allow_private_networks=True,
            sitemap_discovery_enabled=False,
        ),
        state_dir=tmp_path,
    )

    first = await crawler.start(
        [local_site],
        crawl_id="resume-test",
        follow_links=True,
        max_pages=3,
    )
    assert first.completed is True
    assert first.stats == {"queued": 0, "leased": 0, "done": 3, "failed": 0}
    assert {record.title for record in first.records} == {"Root", "A", "B"}
    assert first.state_path.exists()

    second = await crawler.resume("resume-test")
    assert second.completed is True
    assert second.stats == first.stats
    assert [record.to_dict() for record in second.records] == [
        record.to_dict() for record in first.records
    ]


@pytest.mark.asyncio
async def test_resumable_crawl_fetches_sitemap_only_url(
    tmp_path: Path,
    local_site: str,
) -> None:
    crawler = ResumableCrawler(
        CrawlConfig(
            max_pages=2,
            max_concurrency=1,
            max_retries=0,
            respect_robots_txt=False,
            allow_private_networks=True,
            sitemap_discovery_enabled=True,
        ),
        state_dir=tmp_path,
    )

    result = await crawler.start(
        [f"{local_site}sitemap-only"],
        crawl_id="sitemap-seed-test",
        max_pages=2,
    )

    assert result.completed is True
    assert result.stats == {"queued": 0, "leased": 0, "done": 2, "failed": 0}
    assert {record.title for record in result.records} == {"Sitemap Seed", "Hidden"}


@pytest.mark.asyncio
async def test_resumable_crawler_rejects_unsafe_crawl_ids(tmp_path: Path) -> None:
    crawler = ResumableCrawler(state_dir=tmp_path)
    with pytest.raises(ValueError, match="crawl_id"):
        await crawler.status("../escape")
