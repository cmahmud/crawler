from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin

from syndcrawler.config import CrawlConfig
from syndcrawler.core.egress import EgressPolicy, UnsafeTargetError
from syndcrawler.core.policy import FetchAction, FetchEngine, NetworkRoute
from syndcrawler.core.routes import ProxyMode, ProxyPool, RouteBroker
from syndcrawler.core.url import canonicalize_url, hostname, same_hostname
from syndcrawler.models import PageRecord

_DIRECT_HTTP = FetchAction(FetchEngine.HTTP, NetworkRoute.DIRECT)


class LocalCrawler:
    """Single-process crawler using Crawlee's Impit-backed HTTP runtime."""

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
    ) -> list[PageRecord]:
        from crawlee import ProxyConfiguration, Request
        from crawlee.crawlers import BasicCrawlingContext, HttpCrawler, HttpCrawlingContext
        from pydantic import ValidationError
        from selectolax.lexbor import LexborHTMLParser

        limit = max_pages or self.config.max_pages
        safe_seeds = [await self.egress.validate(seed) for seed in seeds]
        seed_hosts = tuple(safe_seeds)
        records: list[PageRecord] = []

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
                context_key = f"{hostname(request.url)}:unknown"
                decision = broker.choose(
                    context_key,
                    request.unique_key,
                    session_id=session_id,
                )
                return decision.proxy_url

            proxy_configuration = ProxyConfiguration(new_url_function=choose_proxy)

        crawler = HttpCrawler(
            max_requests_per_crawl=limit,
            max_request_retries=self.config.max_retries,
            max_concurrency=self.config.max_concurrency,
            respect_robots_txt_file=self.config.respect_robots_txt,
            proxy_configuration=proxy_configuration,
        )

        @crawler.router.default_handler
        async def handler(context: HttpCrawlingContext) -> None:
            source_url = canonicalize_url(context.request.url)
            body = await context.http_response.read()
            parser = LexborHTMLParser(body)
            title_node = parser.css_first("title")
            title = title_node.text(strip=True) if title_node is not None else None
            links = await self._extract_links(parser, source_url, seed_hosts)

            status_code = getattr(context.http_response, "status_code", None)
            headers = getattr(context.http_response, "headers", {})
            content_type = headers.get("content-type") if hasattr(headers, "get") else None

            selected = broker.selected_action(context.request.unique_key) or _DIRECT_HTTP
            broker.observe(
                context.request.unique_key,
                success=True,
                quality=1.0 if body else 0.25,
            )
            record = PageRecord(
                url=source_url,
                status_code=status_code if isinstance(status_code, int) else None,
                content_type=str(content_type) if content_type is not None else None,
                title=title,
                links=tuple(links),
                engine=selected.engine.value,
                route=selected.route.value,
            )
            records.append(record)
            await context.push_data(record.to_dict())

            if not follow_links:
                return

            requests = []
            for link in links:
                try:
                    requests.append(Request.from_url(link))
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

        await crawler.run(safe_seeds)

        if self.config.output is not None:
            _write_records(self.config.output, records)
        return records

    async def _extract_links(
        self,
        parser,
        source_url: str,
        seed_urls: tuple[str, ...],
    ) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for node in parser.css("a[href]"):
            href = node.attributes.get("href")
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            try:
                candidate = canonicalize_url(urljoin(source_url, href))
            except (ValueError, UnicodeError):
                continue
            if self.config.same_domain and not any(
                same_hostname(seed, candidate) for seed in seed_urls
            ):
                continue
            if candidate in seen:
                continue
            try:
                candidate = await self.egress.validate(candidate)
            except UnsafeTargetError:
                continue
            seen.add(candidate)
            result.append(candidate)
        return result


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
