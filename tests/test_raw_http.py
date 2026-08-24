from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from syndcrawler.config import CrawlConfig
from syndcrawler.core.egress import UnsafeTargetError
from syndcrawler.core.url import canonicalize_url
from syndcrawler.runtime.raw_http import RawFetchError, SafeRawHttpFetcher


class _RawHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/ok":
            body = b"raw document"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if self.path == "/unsafe-redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if self.path == "/large":
            body = b"x" * 256
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def raw_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RawHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class _SelectiveEgress:
    async def validate(self, value: str) -> str:
        normalized = canonicalize_url(value)
        if "169.254.169.254" in normalized:
            raise UnsafeTargetError("metadata destination blocked by test policy")
        return normalized

    async def validate_proxy(self, value: str) -> str:
        return value


@pytest.mark.asyncio
async def test_raw_fetcher_follows_validated_redirects(raw_site: str) -> None:
    fetcher = SafeRawHttpFetcher(
        CrawlConfig(allow_private_networks=True),
    )
    response = await fetcher.fetch(f"{raw_site}/redirect", max_bytes=1024)

    assert response.status_code == 200
    assert response.url == f"{raw_site}/ok"
    assert response.body == b"raw document"
    assert response.redirect_chain == (f"{raw_site}/ok",)
    assert response.engine == "http"
    assert response.route == "direct"


@pytest.mark.asyncio
async def test_raw_fetcher_rejects_oversized_response(raw_site: str) -> None:
    fetcher = SafeRawHttpFetcher(CrawlConfig(allow_private_networks=True))
    with pytest.raises(RawFetchError, match="exceeds"):
        await fetcher.fetch(f"{raw_site}/large", max_bytes=100)


@pytest.mark.asyncio
async def test_raw_fetcher_validates_redirect_before_next_connection(raw_site: str) -> None:
    fetcher = SafeRawHttpFetcher(
        CrawlConfig(allow_private_networks=True),
        egress=_SelectiveEgress(),  # type: ignore[arg-type]
    )
    with pytest.raises(UnsafeTargetError, match="metadata"):
        await fetcher.fetch(f"{raw_site}/unsafe-redirect", max_bytes=1024)
