from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from syndcrawler.config import CrawlConfig
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

    async def render(
        self,
        urls: list[str],
        *,
        seed_urls: tuple[str, ...],
    ) -> dict[str, PageRecord]:
        if not urls:
            return {}

        try:
            from crawlee import ConcurrencySettings
            from crawlee.crawlers import (
                BasicCrawlingContext,
                PlaywrightCrawler,
                PlaywrightCrawlingContext,
            )
            from crawlee.proxy_configuration import ProxyConfiguration
        except ImportError as exc:
            raise RuntimeError(
                "Browser escalation requires `pip install 'syndcrawler[browser]'` "
                "and a Playwright browser installation."
            ) from exc

        safe_urls = [await self.egress.validate(url) for url in urls]
        records: dict[str, PageRecord] = {}
        proxy_configuration = None

        if self.proxy_pool is not None and self.config.proxy_mode is not ProxyMode.DIRECT:

            async def choose_proxy(session_id, request):
                if request is None:
                    if self.config.proxy_mode is ProxyMode.REQUIRED:
                        return self.proxy_pool.next(session_id)
                    return None
                request_key = f"browser:{request.unique_key}"
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

        crawler = PlaywrightCrawler(
            headless=True,
            browser_type=self.config.browser_type,
            browser_launch_options=launch_options or None,
            max_requests_per_crawl=len(safe_urls),
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
        )

        @crawler.router.default_handler
        async def handler(context: PlaywrightCrawlingContext) -> None:
            original_url = canonicalize_url(context.request.url)
            request_key = f"browser:{context.request.unique_key}"
            selected = self.broker.selected_action(request_key) or _BROWSER_DIRECT
            status_code = context.response.status

            if status_code in _BLOCKED_STATUS_CODES:
                self.broker.observe(request_key, success=False, quality=0.0)
                context.log.warning(
                    f"Browser rendering stopped at access-controlled response: {status_code}"
                )
                return

            try:
                source_url = await self.egress.validate(context.page.url)
            except UnsafeTargetError as exc:
                self.broker.observe(request_key, success=False, quality=0.0)
                context.log.error(f"Browser navigation violated egress policy: {exc}")
                return

            if self.config.browser_settle_seconds:
                await context.page.wait_for_timeout(
                    self.config.browser_settle_seconds * 1000
                )

            html = await context.page.content()
            parsed = parse_html(html, source_url)
            links = await self._safe_links(parsed.links, seed_urls)
            self.broker.observe(request_key, success=True, quality=1.0 if html else 0.25)

            content_type = context.response.headers.get("content-type")
            record = PageRecord(
                url=source_url,
                status_code=status_code,
                content_type=content_type,
                title=parsed.title,
                links=tuple(links),
                engine=selected.engine.value,
                route=selected.route.value,
                metadata={"rendered": True, "requested_url": original_url},
            )
            records[original_url] = record
            await context.push_data(record.to_dict())

        @crawler.failed_request_handler
        async def failed_handler(
            context: BasicCrawlingContext,
            error: Exception,
        ) -> None:
            request_key = f"browser:{context.request.unique_key}"
            self.broker.observe(request_key, success=False, quality=0.0)
            context.log.error(f"Browser render failed: {context.request.url}: {error}")

        await crawler.run(safe_urls)
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
