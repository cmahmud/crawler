from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from syndcrawler.config import CrawlConfig
from syndcrawler.core.change import HttpValidators
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
        if self.path == "/conditional":
            etag = '"v1"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            body = b"conditional document"
            self.send_response(200)
            self.send_header("ETag", etag)
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


class _CaptureHandler(BaseHTTPRequestHandler):
    observed_etag: str | None = None
    observed_modified: str | None = None

    def do_GET(self) -> None:
        type(self).observed_etag = self.headers.get("If-None-Match")
        type(self).observed_modified = self.headers.get("If-Modified-Since")
        body = b"destination"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _CrossOriginRedirectHandler(BaseHTTPRequestHandler):
    destination = ""

    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", type(self).destination)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def cross_origin_redirect():
    destination = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    destination_thread = Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()
    destination_host, destination_port = destination.server_address
    _CaptureHandler.observed_etag = None
    _CaptureHandler.observed_modified = None
    _CrossOriginRedirectHandler.destination = (
        f"http://{destination_host}:{destination_port}/destination"
    )

    source = ThreadingHTTPServer(("127.0.0.1", 0), _CrossOriginRedirectHandler)
    source_thread = Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    source_host, source_port = source.server_address
    try:
        yield f"http://{source_host}:{source_port}/redirect"
    finally:
        source.shutdown()
        source_thread.join(timeout=5)
        source.server_close()
        destination.shutdown()
        destination_thread.join(timeout=5)
        destination.server_close()


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
async def test_raw_fetcher_supports_conditional_304(raw_site: str) -> None:
    fetcher = SafeRawHttpFetcher(CrawlConfig(allow_private_networks=True))
    response = await fetcher.fetch(
        f"{raw_site}/conditional",
        max_bytes=1024,
        validators=HttpValidators(etag='"v1"'),
    )

    assert response.status_code == 304
    assert response.not_modified is True
    assert response.body == b""
    assert response.headers["etag"] == '"v1"'


@pytest.mark.asyncio
async def test_raw_fetcher_drops_validators_on_cross_origin_redirect(
    cross_origin_redirect: str,
) -> None:
    fetcher = SafeRawHttpFetcher(CrawlConfig(allow_private_networks=True))
    response = await fetcher.fetch(
        cross_origin_redirect,
        max_bytes=1024,
        validators=HttpValidators(
            etag='"origin-specific"',
            last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        ),
    )

    assert response.status_code == 200
    assert _CaptureHandler.observed_etag is None
    assert _CaptureHandler.observed_modified is None


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
