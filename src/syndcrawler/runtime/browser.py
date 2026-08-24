from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from syndcrawler.config import CrawlConfig
from syndcrawler.core.change import fingerprint_page
from syndcrawler.core.egress import EgressPolicy, UnsafeTargetError
from syndcrawler.core.policy import FetchAction, FetchEngine, NetworkRoute
from syndcrawler.core.routes import ProxyMode, ProxyPool, RouteBroker
from syndcrawler.core.url import canonicalize_url, hostname, same_hostname
from syndcrawler.models import PageRecord
from syndcrawler.runtime.html import parse_html

_BLOCKED_STATUS_CODES = {401, 403, 407, 423, 429}
_BROWSER_DIRECT = FetchAction(FetchEngine.BROWSER, NetworkRoute.DIRECT)


class BrowserRenderer:
    """Bounded browser-rendering pass used only after HTTP evidence requests escalation."""

    def __init__(
        self,
        config: CrawlConfig,
        *,
        egress: EgressPolicy,
        broker: RouteBroker,
        proxy_pool: ProxyPool | None,
    ) -> None:
        self.config = config
        self.egress = egress
        self.broker = broker
        self.proxy_pool = proxy_pool
        self.failures: dict[str, str] = {}

    async def render(
        self,
        urls: list[str],
        *,
        seed_urls: tuple[str, ...],
    ) -> dict[str, PageRecord]:
        if not urls:
            return {}

        try:
            from crawlee import ConcurrencySettings, Request
            from crawlee.crawlers import (
                BasicCrawlingContext,
                PlaywrightCrawler,
                PlaywrightCrawlingContext,
            )
            from crawlee.proxy_configuration import ProxyConfiguration
            from crawlee.storage_clients import MemoryStorageClient
        except ImportError as exc:
            raise RuntimeError(
                "Browser escalation requires `pip install 'syndcrawler[browser]'` "
                "and a Playwright browser installation."
            ) from exc

        safe_urls = [await self.egress.validate(url) for url in urls]
        browser_requests = [
            Request.from_url(
                url,
                unique_key=f"browser:{canonicalize_url(url)}",
                user_data={"escalated_from": "http"},
            )
            for url in safe_urls
        ]
        records: dict[str, PageRecord] = {}
        proxy_configuration = None

        if self.proxy_pool is not None and self.config.proxy_mode is not ProxyMode.DIRECT:

            async def choose_proxy(session_id, request):
                if request is None:
                    if self.config.proxy_mode is ProxyMode.REQUIRED:
                        return self.proxy_pool.next(session_id)
                    return None
                request_key = f"browser:{request.unique_key}"
                decision = self.broker.pending_decision(request_key)
                if decision is None:
                    decision = self.broker.choose(
                        f"{hostname(request.url)}:unknown",
                        request_key,
                        engine=FetchEngine.BROWSER,
                        session_id=session_id,
                    )
                return decision.proxy_url

            proxy_configuration = ProxyConfiguration(new_url_function=choose_proxy)

        launch_options: dict[str, object] = {}
        executable = self.config.browser_executable_path
        if executable is not None:
            launch_options["executable_path"] = str(Path(executable))
        if self.config.browser_type in {"chromium", "chrome"}:
            launch_options["chromium_sandbox"] = self.config.browser_chromium_sandbox

        crawler = PlaywrightCrawler(
            headless=True,
            browser_type=self.config.browser_type,
            browser_launch_options=launch_options or None,
            max_requests_per_crawl=len(browser_requests),
            max_request_retries=0,
            concurrency_settings=ConcurrencySettings(
                min_concurrency=1,
                desired_concurrency=1,
                max_concurrency=self.config.browser_max_concurrency,
            ),
            navigation_timeout=timedelta(
                seconds=self.config.browser_navigation_timeout_seconds
            ),
            respect_robots_txt_file=self.config.respect_robots_txt,
            retry_on_blocked=False,
            proxy_configuration=proxy_configuration,
            storage_client=MemoryStorageClient(),
        )

        @crawler.pre_navigation_hook
        async def select_route(context: BasicCrawlingContext) -> None:
            request_key = f"browser:{context.request.unique_key}"
            session_id = context.session.id if context.session is not None else None
            self.broker.choose(
                f"{hostname(context.request.url)}:unknown",
                request_key,
                engine=FetchEngine.BROWSER,
                session_id=session_id,
            )

        @crawler.router.default_handler
        async def handler(context: PlaywrightCrawlingContext) -> None:
            original_url = canonicalize_url(context.request.url)
            request_key = f"browser:{context.request.unique_key}"
            selected = self.broker.selected_action(request_key) or _BROWSER_DIRECT
            status_code = context.response.status

            if status_code in _BLOCKED_STATUS_CODES:
                message = f"access-controlled response: {status_code}"
                self.failures[original_url] = message
                self.broker.observe(request_key, success=False, quality=0.0)
                context.log.warning(f"Browser rendering stopped at {message}")
                return

            try:
                source_url = await self.egress.validate(context.page.url)
            except UnsafeTargetError as exc:
                self.failures[original_url] = f"egress policy: {exc}"
                self.broker.observe(request_key, success=False, quality=0.0)
                context.log.error(f"Browser navigation violated egress policy: {exc}")
                return

            html = await _wait_for_dom_settle(
                context.page,
                settle_seconds=self.config.browser_settle_seconds,
            )
            parsed = parse_html(html, source_url)
            links = await self._safe_links(parsed.links, seed_urls)
            self.broker.observe(request_key, success=True, quality=1.0 if html else 0.25)

            response_headers = context.response.headers
            content_type = response_headers.get("content-type")
            structured = parsed.structured.to_dict()
            fingerprint = fingerprint_page(
                html,
                title=parsed.title,
                links=parsed.links,
                structured=structured,
                headers=response_headers,
            )
            record = PageRecord(
                url=source_url,
                status_code=status_code,
                content_type=content_type,
                title=parsed.title,
                links=tuple(links),
                engine=selected.engine.value,
                route=selected.route.value,
                metadata={
                    "rendered": True,
                    "requested_url": original_url,
                    "structured": structured,
                    "fingerprint": fingerprint.to_dict(),
                },
            )
            records[original_url] = record
            self.failures.pop(original_url, None)
            await context.push_data(record.to_dict())

        @crawler.failed_request_handler
        async def failed_handler(
            context: BasicCrawlingContext,
            error: Exception,
        ) -> None:
            original_url = canonicalize_url(context.request.url)
            request_key = f"browser:{context.request.unique_key}"
            message = f"{type(error).__name__}: {error}"
            self.failures[original_url] = message
            self.broker.observe(request_key, success=False, quality=0.0)
            context.log.error(f"Browser render failed: {context.request.url}: {error}")

        await crawler.run(browser_requests)
        return records

    async def _safe_links(
        self,
        links: tuple[str, ...],
        seed_urls: tuple[str, ...],
    ) -> list[str]:
        safe: list[str] = []
        for candidate in links:
            if self.config.same_domain and not any(
                same_hostname(seed, candidate) for seed in seed_urls
            ):
                continue
            try:
                safe.append(await self.egress.validate(candidate))
            except UnsafeTargetError:
                continue
        return safe


async def _wait_for_dom_settle(page, *, settle_seconds: float) -> str:
    """Return HTML after a minimum settle period and a bounded stability window.

    The configured settle time is a minimum observation period, not a hard ceiling.
    Loaded CI/VPS hosts can delay short JavaScript timers, so after the minimum wait
    we sample the DOM until two consecutive snapshots match or a short safety
    ceiling expires.
    """

    if settle_seconds <= 0:
        return await page.content()

    await page.wait_for_timeout(settle_seconds * 1000)
    previous = await page.content()
    stable_samples = 0
    interval = min(0.1, max(0.025, settle_seconds / 2))
    deadline = time.monotonic() + max(0.5, settle_seconds * 4)

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        await page.wait_for_timeout(min(interval, max(0.0, remaining)) * 1000)
        current = await page.content()
        if current == previous:
            stable_samples += 1
            if stable_samples >= 2:
                return current
        else:
            previous = current
            stable_samples = 0

    return previous
