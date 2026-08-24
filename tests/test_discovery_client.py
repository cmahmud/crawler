from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from syndcrawler.config import CrawlConfig
from syndcrawler.discovery import SitemapDiscoveryClient


class _DiscoveryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        origin = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        documents = {
            "/robots.txt": (
                "text/plain",
                f"User-agent: *\nSitemap: {origin}/sitemap-index.xml\n",
            ),
            "/sitemap-index.xml": (
                "application/xml",
                "<sitemapindex>"
                f"<sitemap><loc>{origin}/products.xml</loc></sitemap>"
                f"<sitemap><loc>{origin}/articles.xml</loc></sitemap>"
                "</sitemapindex>",
            ),
            "/products.xml": (
                "application/xml",
                "<urlset>"
                f"<url><loc>{origin}/product/1</loc></url>"
                f"<url><loc>{origin}/product/2#fragment</loc></url>"
                "<url><loc>http://127.0.0.2/out-of-scope</loc></url>"
                "</urlset>",
            ),
            "/articles.xml": (
                "application/xml",
                "<urlset>"
                f"<url><loc>{origin}/article/1</loc></url>"
                "</urlset>",
            ),
        }
        document = documents.get(self.path)
        if document is None:
            self.send_response(404)
            self.end_headers()
            return
        content_type, body = document
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def discovery_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DiscoveryHandler)
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
async def test_discovery_client_follows_bounded_sitemap_graph(discovery_site: str) -> None:
    client = SitemapDiscoveryClient(
        CrawlConfig(
            allow_private_networks=True,
            same_domain=True,
        )
    )

    result = await client.discover(discovery_site)

    assert {item.url for item in result.urls} == {
        f"{discovery_site}product/1",
        f"{discovery_site}product/2",
        f"{discovery_site}article/1",
    }
    assert result.documents_fetched == (
        f"{discovery_site}sitemap-index.xml",
        f"{discovery_site}products.xml",
        f"{discovery_site}articles.xml",
    )
    assert result.failures == ()
    assert result.truncated is False
    assert all(item.source_url.endswith(".xml") for item in result.urls)


@pytest.mark.asyncio
async def test_discovery_client_honors_document_cap(discovery_site: str) -> None:
    client = SitemapDiscoveryClient(
        CrawlConfig(allow_private_networks=True),
    )

    result = await client.discover(discovery_site, max_documents=1)

    assert result.documents_fetched == (f"{discovery_site}sitemap-index.xml",)
    assert result.urls == ()
    assert result.truncated is True
