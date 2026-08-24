from __future__ import annotations

import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")
pytest.importorskip("redis")

from syndcrawler.config import CrawlConfig
from syndcrawler.runtime import ServerRuntime
from syndcrawler.runtime.server_recrawl import ServerRecrawlScheduler


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


@pytest.fixture
def recrawl_site():
    state = {
        "etag": '"v1"',
        "title": "Product v1",
        "if_none_match": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state["if_none_match"] = self.headers.get("If-None-Match")
            etag = str(state["etag"])
            if state["if_none_match"] == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            body = (
                f"<html><head><title>{state['title']}</title></head>"
                f"<body>{state['title']}</body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/", state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _config() -> CrawlConfig:
    return CrawlConfig(
        max_pages=1,
        max_concurrency=1,
        max_retries=0,
        respect_robots_txt=False,
        allow_private_networks=True,
        sitemap_discovery_enabled=False,
    )


@pytest.mark.asyncio
async def test_server_recrawl_uses_validators_and_respects_lifecycle(recrawl_site) -> None:
    url, site = recrawl_site
    crawl_id = f"recrawl-{uuid.uuid4().hex}"
    runtime = await ServerRuntime.from_urls(
        redis_url=_redis_url(),
        postgres_dsn=_postgres_dsn(),
        config=_config(),
        redis_key_prefix=f"syndcrawler-recrawl-test:{uuid.uuid4().hex}",
    )
    scheduler = ServerRecrawlScheduler(
        runtime.store,
        runtime.config,
        policy=runtime.recrawl_policy,
    )
    clock = time.time() + 100.0
    try:
        await runtime.submit([url], crawl_id=crawl_id, follow_links=False, max_pages=1)
        status = await runtime.run_until_idle(crawl_id)
        assert status.lifecycle == "completed"
        assert status.result_count == 1

        await runtime.store.schedule_resource(
            crawl_id,
            url,
            interval_seconds=10.0,
            next_fetch_at=clock,
        )
        unchanged = await scheduler.run_once(limit=10, now=clock)
        assert unchanged.claimed == 1
        assert unchanged.not_modified == 1
        assert site["if_none_match"] == '"v1"'

        state = await runtime.store.get_resource_state(crawl_id, url)
        assert state is not None
        assert state.next_fetch_at is not None and state.next_fetch_at > clock

        await runtime.store.schedule_resource(
            crawl_id,
            url,
            interval_seconds=10.0,
            next_fetch_at=clock + 1.0,
        )
        site["etag"] = '"v2"'
        site["title"] = "Product v2"
        changed = await scheduler.run_once(limit=10, now=clock + 1.0)
        assert changed.claimed == 1
        assert changed.semantic_changed == 1
        records = await runtime.results(crawl_id)
        assert records[0].title == "Product v2"

        await runtime.store.schedule_resource(
            crawl_id,
            url,
            interval_seconds=10.0,
            next_fetch_at=clock + 2.0,
        )
        paused = await runtime.pause(crawl_id)
        assert paused.lifecycle == "paused"
        assert (await scheduler.run_once(limit=10, now=clock + 2.0)).claimed == 0

        resumed = await runtime.resume_crawl(crawl_id)
        assert resumed.lifecycle == "completed"
        after_resume = await scheduler.run_once(limit=10, now=clock + 2.0)
        assert after_resume.claimed == 1
        assert after_resume.not_modified == 1

        cancelled = await runtime.cancel(crawl_id)
        assert cancelled.lifecycle == "cancelled"
    finally:
        await runtime.frontier.purge(crawl_id)
        await runtime.close()
