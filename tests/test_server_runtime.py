from __future__ import annotations

import asyncio
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")
pytest.importorskip("redis")

from syndcrawler.config import CrawlConfig
from syndcrawler.core.frontier import QueuePolicy
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


class _ServerSiteHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            body = (
                "<html><head><title>Root</title></head><body>"
                + "".join(f'<a href="/{name}">{name}</a>' for name in "abcde")
                + "</body></html>"
            )
            self._send(body)
            return
        if self.path in {"/a", "/b", "/c", "/d", "/e"}:
            name = self.path[1:].upper()
            self._send(
                f"<html><head><title>{name}</title></head><body>{name} page</body></html>"
            )
            return
        self.send_response(404)
        self.end_headers()

    def _send(self, body: str) -> None:
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def server_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ServerSiteHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _config() -> CrawlConfig:
    return CrawlConfig(
        max_pages=3,
        max_concurrency=5,
        max_retries=0,
        respect_robots_txt=False,
        allow_private_networks=True,
        sitemap_discovery_enabled=False,
    )


@pytest.mark.asyncio
async def test_server_runtime_shares_crawl_across_workers_and_honors_budget(
    server_site: str,
) -> None:
    prefix = f"syndcrawler-server-test:{uuid.uuid4().hex}"
    crawl_id = f"server-{uuid.uuid4().hex}"
    first = await ServerRuntime.from_urls(
        redis_url=_redis_url(),
        postgres_dsn=_postgres_dsn(),
        config=_config(),
        redis_key_prefix=prefix,
        postgres_max_pool_size=4,
    )
    second = await ServerRuntime.from_urls(
        redis_url=_redis_url(),
        postgres_dsn=_postgres_dsn(),
        config=_config(),
        redis_key_prefix=prefix,
        postgres_max_pool_size=4,
    )
    try:
        assert await first.health() == {"ok": True, "postgres": True, "redis": True}
        submitted = await first.submit(
            [server_site],
            crawl_id=crawl_id,
            follow_links=True,
            max_pages=3,
        )
        assert submitted.lifecycle == "active"
        assert submitted.result_count == 0
        assert submitted.completed is False

        replay = await second.submit(
            [server_site],
            crawl_id=crawl_id,
            follow_links=True,
            max_pages=3,
        )
        assert replay.crawl_id == crawl_id

        root = await first.run_batch(crawl_id, limit=1)
        assert root.persisted == 1
        assert root.discovered == 5

        await first.frontier.set_queue_policy(
            crawl_id,
            "127.0.0.1",
            QueuePolicy(max_concurrency=10),
        )
        left, right = await asyncio.gather(
            first.run_batch(crawl_id, limit=5),
            second.run_batch(crawl_id, limit=5),
        )
        assert left.persisted + right.persisted == 2

        status = await first.status(crawl_id)
        assert status.lifecycle == "completed"
        assert status.completed is True
        assert status.result_count == 3
        assert status.stats["done"] == 3
        assert status.stats["leased"] == 0
        assert status.stats["queued"] == 3

        records = await second.results(crawl_id)
        assert len(records) == 3
        assert "Root" in {record.title for record in records}
        page = await second.results_page(crawl_id, limit=2)
        assert len(page) == 2
        assert await second.run_batch(crawl_id, limit=5) == left.__class__(
            leased=0,
            persisted=0,
            failed=0,
            discovered=0,
        )
    finally:
        await first.frontier.purge(crawl_id)
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_server_pause_resume_and_worker_sweep(server_site: str) -> None:
    prefix = f"syndcrawler-server-test:{uuid.uuid4().hex}"
    crawl_id = f"server-{uuid.uuid4().hex}"
    runtime = await ServerRuntime.from_urls(
        redis_url=_redis_url(),
        postgres_dsn=_postgres_dsn(),
        config=_config(),
        redis_key_prefix=prefix,
    )
    try:
        await runtime.submit([server_site], crawl_id=crawl_id, max_pages=2)
        paused = await runtime.pause(crawl_id)
        assert paused.lifecycle == "paused"
        assert (await runtime.run_batch(crawl_id, limit=5)).leased == 0
        assert crawl_id not in await runtime.store.runnable_crawl_ids(limit=1000)

        resumed = await runtime.resume_crawl(crawl_id)
        assert resumed.lifecycle == "active"
        assert crawl_id in await runtime.store.runnable_crawl_ids(limit=1000)

        first = await runtime.run_runnable_once(max_crawls=1000, per_crawl_limit=1)
        assert first.persisted == 1
        assert first.discovered == 5

        await runtime.frontier.set_queue_policy(
            crawl_id,
            "127.0.0.1",
            QueuePolicy(max_concurrency=10),
        )
        second = await runtime.run_runnable_once(max_crawls=1000, per_crawl_limit=5)
        assert second.persisted == 1
        status = await runtime.status(crawl_id)
        assert status.lifecycle == "completed"
        assert status.result_count == 2
        assert crawl_id not in await runtime.store.runnable_crawl_ids(limit=1000)
    finally:
        await runtime.frontier.purge(crawl_id)
        await runtime.close()


@pytest.mark.asyncio
async def test_server_cancel_stops_future_work(server_site: str) -> None:
    prefix = f"syndcrawler-server-test:{uuid.uuid4().hex}"
    crawl_id = f"server-{uuid.uuid4().hex}"
    runtime = await ServerRuntime.from_urls(
        redis_url=_redis_url(),
        postgres_dsn=_postgres_dsn(),
        config=_config(),
        redis_key_prefix=prefix,
    )
    try:
        await runtime.submit([server_site], crawl_id=crawl_id, max_pages=3)
        cancelled = await runtime.cancel(crawl_id)
        assert cancelled.lifecycle == "cancelled"
        assert cancelled.completed is False
        assert cancelled.stats == {"queued": 0, "leased": 0, "done": 0, "failed": 0}
        assert (await runtime.run_batch(crawl_id)).leased == 0
        with pytest.raises(ValueError, match="cancelled crawl cannot be resumed"):
            await runtime.resume_crawl(crawl_id)
    finally:
        await runtime.frontier.purge(crawl_id)
        await runtime.close()


@pytest.mark.asyncio
async def test_server_cancel_drains_active_lease_before_purge(server_site: str) -> None:
    prefix = f"syndcrawler-server-test:{uuid.uuid4().hex}"
    crawl_id = f"server-{uuid.uuid4().hex}"
    runtime = await ServerRuntime.from_urls(
        redis_url=_redis_url(),
        postgres_dsn=_postgres_dsn(),
        config=_config(),
        redis_key_prefix=prefix,
    )
    try:
        await runtime.submit([server_site], crawl_id=crawl_id, max_pages=3)
        lease = (await runtime.frontier.lease(crawl_id, limit=1, lease_seconds=60))[0]

        cancelled = await runtime.cancel(crawl_id)
        assert cancelled.lifecycle == "cancelled"
        assert cancelled.stats["leased"] == 1
        assert cancelled.stats["queued"] == 0

        # The held lease remains valid after cancellation instead of disappearing
        # beneath an in-flight worker.
        await runtime.frontier.ack(lease)

        cleaned = await runtime.cancel(crawl_id)
        assert cleaned.lifecycle == "cancelled"
        assert cleaned.stats == {"queued": 0, "leased": 0, "done": 0, "failed": 0}
    finally:
        await runtime.frontier.purge(crawl_id)
        await runtime.close()


@pytest.mark.asyncio
async def test_server_submit_rejects_conflicting_manifest(server_site: str) -> None:
    prefix = f"syndcrawler-server-test:{uuid.uuid4().hex}"
    crawl_id = f"server-{uuid.uuid4().hex}"
    runtime = await ServerRuntime.from_urls(
        redis_url=_redis_url(),
        postgres_dsn=_postgres_dsn(),
        config=_config(),
        redis_key_prefix=prefix,
    )
    try:
        await runtime.submit([server_site], crawl_id=crawl_id, max_pages=3)
        with pytest.raises(ValueError, match="different configuration"):
            await runtime.submit([server_site], crawl_id=crawl_id, max_pages=4)
        await runtime.cancel(crawl_id)
    finally:
        await runtime.frontier.purge(crawl_id)
        await runtime.close()
