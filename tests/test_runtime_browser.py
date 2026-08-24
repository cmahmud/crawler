from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

pytest.importorskip("playwright")

from syndcrawler.config import CrawlConfig
from syndcrawler.runtime import LocalCrawler


class _DynamicHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/dynamic":
            body = """<!doctype html>
            <html>
              <head><title>Loading</title></head>
              <body>
                <div id="root"></div>
                <noscript>Please enable JavaScript to continue.</noscript>
                <script>
                  setTimeout(() => {
                    document.title = 'Rendered Product';
                    document.querySelector('#root').innerHTML =
                      '<main><h1>Rendered Product</h1><p>Loaded by JavaScript.</p></main>';
                  }, 50);
                </script>
                <script src="data:text/javascript,void 0"></script>
                <script src="data:text/javascript,void 0"></script>
              </body>
            </html>"""
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def dynamic_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DynamicHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/dynamic"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.asyncio
async def test_http_shell_escalates_to_browser(dynamic_site: str) -> None:
    crawler = LocalCrawler(
        CrawlConfig(
            max_pages=1,
            max_concurrency=1,
            max_retries=0,
            respect_robots_txt=False,
            allow_private_networks=True,
            browser_enabled=True,
            browser_type="chromium",
            browser_max_concurrency=1,
            browser_settle_seconds=0.15,
            browser_chromium_sandbox=False,
        )
    )

    record = await crawler.scrape(dynamic_site)
    assert record is not None
    assert record.engine == "browser", record.metadata
    assert record.title == "Rendered Product", record.metadata
    assert record.metadata["rendered"] is True
    assert record.metadata["escalated_from"] == "http"
