from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from syndcrawler.config import CrawlConfig
from syndcrawler.core.egress import UnsafeTargetError
from syndcrawler.core.url import same_hostname
from syndcrawler.discovery.documents import (
    DiscoveryParseError,
    parse_robots_sitemaps,
    parse_sitemap,
)
from syndcrawler.runtime.raw_http import RawFetchError, SafeRawHttpFetcher

_ROBOTS_MAX_BYTES = 1024 * 1024
_SITEMAP_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DiscoveredUrl:
    url: str
    source_url: str
    via: str
    depth: int


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    url: str
    error: str


@dataclass(frozen=True, slots=True)
class SitemapDiscoveryResult:
    urls: tuple[DiscoveredUrl, ...]
    documents_fetched: tuple[str, ...]
    failures: tuple[DiscoveryFailure, ...]
    truncated: bool


class SitemapDiscoveryClient:
    """Discover public crawl targets from robots and bounded sitemap graphs."""

    def __init__(
        self,
        config: CrawlConfig | None = None,
        *,
        fetcher: SafeRawHttpFetcher | None = None,
    ) -> None:
        self.config = config or CrawlConfig()
        self.fetcher = fetcher or SafeRawHttpFetcher(self.config)

    async def discover(
        self,
        seed_url: str,
        *,
        max_documents: int = 32,
        max_depth: int = 4,
        max_urls: int = 10_000,
        try_default_sitemap: bool = True,
    ) -> SitemapDiscoveryResult:
        if max_documents <= 0:
            raise ValueError("max_documents must be positive")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative")
        if max_urls <= 0:
            raise ValueError("max_urls must be positive")

        safe_seed = await self.fetcher.egress.validate(seed_url)
        origin = _origin(safe_seed)
        robots_url = f"{origin}/robots.txt"
        initial_sitemaps: list[str] = []
        failures: list[DiscoveryFailure] = []

        try:
            robots = await self.fetcher.fetch(
                robots_url,
                max_bytes=_ROBOTS_MAX_BYTES,
            )
            if 200 <= robots.status_code < 300:
                initial_sitemaps.extend(
                    parse_robots_sitemaps(robots.body, robots.url)
                )
        except (RawFetchError, UnsafeTargetError, OSError, TimeoutError) as exc:
            failures.append(DiscoveryFailure(robots_url, _error_text(exc)))

        if not initial_sitemaps and try_default_sitemap:
            initial_sitemaps.append(f"{origin}/sitemap.xml")

        queue: deque[tuple[str, int]] = deque()
        seen_documents: set[str] = set()
        for candidate in initial_sitemaps:
            try:
                safe = await self.fetcher.egress.validate(candidate)
            except UnsafeTargetError as exc:
                failures.append(DiscoveryFailure(candidate, _error_text(exc)))
                continue
            if safe not in seen_documents:
                queue.append((safe, 0))

        discovered: dict[str, DiscoveredUrl] = {}
        documents: list[str] = []
        truncated = False

        while queue:
            if len(documents) >= max_documents or len(discovered) >= max_urls:
                truncated = True
                break

            document_url, depth = queue.popleft()
            if document_url in seen_documents:
                continue
            seen_documents.add(document_url)
            documents.append(document_url)

            try:
                response = await self.fetcher.fetch(
                    document_url,
                    max_bytes=_SITEMAP_MAX_BYTES,
                )
                if not 200 <= response.status_code < 300:
                    failures.append(
                        DiscoveryFailure(
                            document_url,
                            f"HTTP {response.status_code}",
                        )
                    )
                    continue
                parsed = parse_sitemap(
                    response.body,
                    response.url,
                    max_bytes=_SITEMAP_MAX_BYTES,
                )
            except (
                DiscoveryParseError,
                RawFetchError,
                UnsafeTargetError,
                OSError,
                TimeoutError,
            ) as exc:
                failures.append(DiscoveryFailure(document_url, _error_text(exc)))
                continue

            for candidate in parsed.urls:
                if len(discovered) >= max_urls:
                    truncated = True
                    break
                try:
                    safe = await self.fetcher.egress.validate(candidate)
                except UnsafeTargetError:
                    continue
                if self.config.same_domain and not same_hostname(safe_seed, safe):
                    continue
                discovered.setdefault(
                    safe,
                    DiscoveredUrl(
                        url=safe,
                        source_url=response.url,
                        via=parsed.kind,
                        depth=depth,
                    ),
                )

            if depth >= max_depth:
                if parsed.nested_documents:
                    truncated = True
                continue

            for nested in parsed.nested_documents:
                if len(seen_documents) + len(queue) >= max_documents:
                    truncated = True
                    break
                try:
                    safe_nested = await self.fetcher.egress.validate(nested)
                except UnsafeTargetError:
                    continue
                if safe_nested not in seen_documents:
                    queue.append((safe_nested, depth + 1))

        return SitemapDiscoveryResult(
            urls=tuple(discovered.values()),
            documents_fetched=tuple(documents),
            failures=tuple(failures),
            truncated=truncated,
        )


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"
