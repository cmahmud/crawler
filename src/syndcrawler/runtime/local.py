from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from syndcrawler.config import CrawlConfig
from syndcrawler.core.egress import EgressPolicy, UnsafeTargetError
from syndcrawler.core.policy import FetchAction, FetchEngine, NetworkRoute
from syndcrawler.core.rendering import assess_rendering
from syndcrawler.core.routes import ProxyMode, ProxyPool, RouteBroker
from syndcrawler.core.url import hostname, same_hostname
from syndcrawler.models import PageRecord
from syndcrawler.runtime.browser import BrowserRenderer
from syndcrawler.runtime.html import parse_html

_DIRECT_HTTP = FetchAction(FetchEngine.HTTP, NetworkRoute.DIRECT)


class LocalCrawler:
    """Single-process adaptive crawler using Crawlee's Impit HTTP runtime."""

    def __init__(self, config: CrawlConfig | None = None) -> None:
        self.config = config or CrawlConfig()
        self.egress = EgressPolicy(
            allow_private_networks=self.config.allow_private_networks,
        )

    async def scrape(self, url: str) -> PageRecord | None:
        records = await self.crawl([url], follow_links=False, max_pages=1)
        return records[0] if records else None

    async def crawl(
        self,
        seeds: list[str],
        *,
        follow_links: bool = True,
        max_pages: int | None = None,
        scope_urls: list[str] | None = None,
        request_namespace: str | None = None,
    ) -> list[PageRecord]:
        from crawlee import ConcurrencySettings, Request
        from crawlee.crawlers import BasicCrawlingContext, HttpCrawler, HttpCrawlingContext
        from crawlee.proxy_configuration import ProxyConfiguration
        from pydantic import ValidationError

        limit = max_pages or self.config.max_pages
        safe_seeds = [await self.egress.validate(seed) for seed in seeds]
        raw_scope = scope_urls if scope_urls is not None else seeds
        safe_scope = [await self.egress.validate(url) for url in raw_scope]
        seed_hosts = tuple(safe_scope)
        records: list[PageRecord] = []
        browser_candidates: dict[str, PageRecord] = {}

        safe_proxies = tuple(
            [await self.egress.validate_proxy(url) for url in self.config.proxy_urls]
        )
        proxy_pool = ProxyPool(safe_proxies) if safe_proxies else None
        broker = RouteBroker(
            proxy_pool=proxy_pool,
            mode=self.config.proxy_mode,
        )

        proxy_configuration = None
        if proxy_pool is not None and self.config.proxy_mode is not ProxyMode.DIRECT:

            async def choose_proxy(session_id, request):
                if request is None:
                    if self.config.proxy_mode is ProxyMode.REQUIRED:
                        return proxy_pool.next(session_id)
                    return None
                decision = broker.pending_decision(request.unique_key)
                if decision is None:
                    decision = broker.choose(
                        f"{hostname(request.url)}:unknown",
                        request.unique_key,
                        session_id=session_id,
                    )
                return decision.proxy_url

            proxy_configuration = ProxyConfiguration(new_url_function=choose_proxy)

        crawler = HttpCrawler(
            max_requests_per_crawl=limit,
            max_request_retries=self.config.max_retries,
            concurrency_settings=ConcurrencySettings(
                min_concurrency=1,
                desired_concurrency=min(4, self.config.max_concurrency),
                max_concurrency=self.config.max_concurrency,
            ),
            respect_robots_txt_file=self.config.respect_robots_txt,
            proxy_configuration=proxy_configuration,
        )

        @crawler.pre_navigation_hook
        async def select_route(context: BasicCrawlingContext) -> None:
            session_id = context.session.id if context.session is not None else None
            broker.choose(
                f"{hostname(context.request.url)}:unknown",
                context.request.unique_key,
                session_id=session_id,
            )

        @crawler.router.default_handler
        async def handler(context: HttpCrawlingContext) -> None:
            loaded_url = context.request.loaded_url or context.request.url
            source_url = await self.egress.validate(loaded_url)
            body = await context.http_response.read()
            parsed = parse_html(body, source_url)
            links = await self._safe_links(parsed.links, seed_hosts)
            rendering = assess_rendering(body)

            status_code = getattr(context.http_response, "status_code", None)
            headers = getattr(context.http_response, "headers", {})
            content_type = headers.get("content-type") if hasattr(headers, "get") else None

            selected = broker.selected_action(context.request.unique_key) or _DIRECT_HTTP
            should_render = self.config.browser_enabled and rendering.requires_browser
            broker.observe(
                context.request.unique_key,
                success=True,
                quality=0.2 if should_render else (1.0 if body else 0.25),
            )

            record = PageRecord(
                url=source_url,
                status_code=status_code if isinstance(status_code, int) else None,
                content_type=str(content_type) if content_type is not None else None,
                title=parsed.title,
                links=tuple(links),
                engine=selected.engine.value,
                route=selected.route.value,
                metadata={
                    "requested_url": context.request.url,
                    "structured": parsed.structured.to_dict(),
                    "rendering_assessment": {
                        "requires_browser": rendering.requires_browser,
                        "score": rendering.score,
                        "visible_text_length": rendering.visible_text_length,
                        "script_count": rendering.script_count,
                        "reasons": list(rendering.reasons),
                    },
                },
            )

            if should_render:
                browser_candidates[source_url] = record
                context.log.info(
                    f"Deferring client-rendered shell to browser: {source_url}"
                )
                return

            records.append(record)
            await context.push_data(record.to_dict())

            if not follow_links:
                return

            requests = []
            for link in links:
                try:
                    requests.append(
                        _request_from_url(
                            Request,
                            link,
                            request_namespace=request_namespace,
                        )
                    )
                except ValidationError:
                    context.log.debug(f"Skipping invalid URL: {link}")
            if requests:
                await context.add_requests(requests)

        @crawler.failed_request_handler
        async def failed_handler(
            context: BasicCrawlingContext,
            error: Exception,
        ) -> None:
            broker.observe(
                context.request.unique_key,
                success=False,
                quality=0.0,
            )
            context.log.error(
                f"Request failed after retries: {context.request.url}: {error}"
            )

        initial_requests = [
            _request_from_url(
                Request,
                url,
                request_namespace=request_namespace,
            )
            for url in safe_seeds
        ]
        await crawler.run(initial_requests)

        if browser_candidates:
            renderer = BrowserRenderer(
                self.config,
                egress=self.egress,
                broker=broker,
                proxy_pool=proxy_pool,
            )
            rendered = await renderer.render(
                list(browser_candidates),
                seed_urls=seed_hosts,
            )
            for url, static_record in browser_candidates.items():
                browser_record = rendered.get(url)
                if browser_record is not None:
                    records.append(
                        replace(
                            browser_record,
                            metadata={
                                **static_record.metadata,
                                **browser_record.metadata,
                                "requested_url": static_record.metadata.get(
                                    "requested_url",
                                    browser_record.metadata.get("requested_url"),
                                ),
                                "browser_requested_url": browser_record.metadata.get(
                                    "requested_url"
                                ),
                                "escalated_from": "http",
                            },
                        )
                    )
                    continue
                records.append(
                    replace(
                        static_record,
                        metadata={
                            **static_record.metadata,
                            "browser_escalation": {
                                "attempted": True,
                                "succeeded": False,
                                "error": renderer.failures.get(url),
                            },
                        },
                    )
                )

        if self.config.output is not None:
            _write_records(self.config.output, records)
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


def _request_from_url(Request, url: str, *, request_namespace: str | None):
    if request_namespace is None:
        return Request.from_url(url)
    return Request.from_url(url, unique_key=f"{request_namespace}:{url}")


def _write_records(path: Path, records: list[PageRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".jsonl":
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return
    path.write_text(
        json.dumps(
            [record.to_dict() for record in records],
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
