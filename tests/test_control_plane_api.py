from __future__ import annotations

import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psycopg")
pytest.importorskip("redis")

from syndcrawler.config import CrawlConfig
from syndcrawler.control_plane import create_app
from syndcrawler.runtime import ServerRuntime


def _redis_url() -> str:
    value = os.environ.get("SYNCRAWLER_TEST_REDIS_URL")
    if not value:
        pytest.skip("SYNCRAWLER_TEST_REDIS_URL is not configured")
    return value


def _postgres_dsn() -> str:
    value = os.environ.get("SYNCRAWLER_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("SYNCRAWLER_TEST_POSTGRES_DSN is not configured")
    return value


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<html><head><title>API target</title></head><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def api_target():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def _runtime(prefix: str) -> ServerRuntime:
    return await ServerRuntime.from_urls(
        redis_url=_redis_url(),
        postgres_dsn=_postgres_dsn(),
        redis_key_prefix=prefix,
        config=CrawlConfig(
            max_pages=5,
            max_concurrency=2,
            max_retries=0,
            respect_robots_txt=False,
            allow_private_networks=True,
            sitemap_discovery_enabled=False,
        ),
    )


@pytest.mark.asyncio
async def test_control_plane_requires_bearer_and_maps_lifecycle(api_target: str) -> None:
    prefix = f"syndcrawler-api-test:{uuid.uuid4().hex}"
    crawl_id = f"api-{uuid.uuid4().hex}"
    runtime = await _runtime(prefix)
    app = create_app(runtime, api_token="secret")
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert health.json() == {"ok": True, "postgres": True, "redis": True}

            unauthenticated = await client.post(
                "/v1/crawls",
                json={"urls": [api_target], "crawl_id": crawl_id},
            )
            assert unauthenticated.status_code == 401

            submitted = await client.post(
                "/v1/crawls",
                headers=headers,
                json={
                    "urls": [api_target],
                    "crawl_id": crawl_id,
                    "follow_links": False,
                    "max_pages": 1,
                },
            )
            assert submitted.status_code == 202
            assert submitted.json()["lifecycle"] == "active"

            status_response = await client.get(f"/v1/crawls/{crawl_id}", headers=headers)
            assert status_response.status_code == 200
            assert status_response.json()["result_count"] == 0

            paused = await client.post(
                f"/v1/crawls/{crawl_id}/pause",
                headers=headers,
            )
            assert paused.status_code == 200
            assert paused.json()["lifecycle"] == "paused"

            resumed = await client.post(
                f"/v1/crawls/{crawl_id}/resume",
                headers=headers,
            )
            assert resumed.status_code == 200
            assert resumed.json()["lifecycle"] == "active"

            results = await client.get(
                f"/v1/crawls/{crawl_id}/results?limit=10&offset=0",
                headers=headers,
            )
            assert results.status_code == 200
            assert results.json() == {
                "crawl_id": crawl_id,
                "total": 0,
                "limit": 10,
                "offset": 0,
                "items": [],
            }

            cancelled = await client.delete(f"/v1/crawls/{crawl_id}", headers=headers)
            assert cancelled.status_code == 200
            assert cancelled.json()["lifecycle"] == "cancelled"

            conflict = await client.post(
                f"/v1/crawls/{crawl_id}/resume",
                headers=headers,
            )
            assert conflict.status_code == 409

            missing = await client.get(
                f"/v1/crawls/missing-{uuid.uuid4().hex}",
                headers=headers,
            )
            assert missing.status_code == 404
    finally:
        await runtime.frontier.purge(crawl_id)
        await runtime.close()


def test_control_plane_refuses_unauthenticated_default() -> None:
    with pytest.raises(RuntimeError, match="requires a bearer token"):
        create_app(None)
