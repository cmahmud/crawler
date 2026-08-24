from __future__ import annotations

import time
from dataclasses import dataclass, replace

from syndcrawler.config import CrawlConfig
from syndcrawler.core.change import ChangeKind, fingerprint_page
from syndcrawler.core.recrawl import RecrawlPolicy
from syndcrawler.core.rendering import assess_rendering
from syndcrawler.core.url import same_hostname
from syndcrawler.models import PageRecord
from syndcrawler.runtime.html import parse_html
from syndcrawler.runtime.local import LocalCrawler
from syndcrawler.runtime.raw_http import SafeRawHttpFetcher
from syndcrawler.storage import PostgresCrawlStore, PostgresRecrawlClaims, RecrawlClaim

_RECRAWL_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ServerRecrawlSweepResult:
    claimed: int
    checked: int
    not_modified: int
    unchanged: int
    representation_changed: int
    semantic_changed: int
    failures: int

    @property
    def idle(self) -> bool:
        return self.claimed == 0


class ServerRecrawlScheduler:
    """Process due conditional recrawls safely across multiple server workers."""

    def __init__(
        self,
        store: PostgresCrawlStore,
        config: CrawlConfig | None = None,
        *,
        policy: RecrawlPolicy | None = None,
    ) -> None:
        self.store = store
        self.config = config or CrawlConfig()
        self.policy = policy or RecrawlPolicy()
        self.claims = PostgresRecrawlClaims(store)

    async def run_once(
        self,
        *,
        limit: int = 100,
        lease_seconds: float = 120.0,
        now: float | None = None,
    ) -> ServerRecrawlSweepResult:
        if limit <= 0:
            raise ValueError("limit must be positive")
        current = time.time() if now is None else now
        claims = await self.claims.claim_due(
            limit=limit,
            lease_seconds=lease_seconds,
            now=current,
        )
        counts = {kind: 0 for kind in ChangeKind}
        failures = 0
        checked = 0

        for claim in claims:
            lifecycle = await self.store.get_lifecycle(claim.state.crawl_id)
            if lifecycle != "completed":
                await self.claims.release(claim)
                continue
            checked += 1
            try:
                kind = await self._process_claim(claim, current)
                counts[kind] += 1
            except Exception:
                failures += 1
                interval = _failure_interval(
                    self.policy,
                    claim.state.recrawl_interval_seconds,
                )
                await self.claims.reschedule(
                    claim,
                    interval_seconds=interval,
                    next_fetch_at=current + interval,
                )

        return ServerRecrawlSweepResult(
            claimed=len(claims),
            checked=checked,
            not_modified=counts[ChangeKind.NOT_MODIFIED],
            unchanged=counts[ChangeKind.UNCHANGED],
            representation_changed=counts[ChangeKind.REPRESENTATION_CHANGED],
            semantic_changed=counts[ChangeKind.SEMANTIC_CHANGED],
            failures=failures,
        )

    async def _process_claim(self, claim: RecrawlClaim, current: float) -> ChangeKind:
        state = claim.state
        manifest = await self.store.get_manifest(state.crawl_id)
        if manifest is None:
            raise ValueError(f"crawl manifest does not exist: {state.crawl_id}")
        fetch_config = replace(
            self.config,
            output=None,
            same_domain=manifest.same_domain,
        )
        raw_fetcher = SafeRawHttpFetcher(fetch_config)
        response = await raw_fetcher.fetch(
            state.request_url,
            max_bytes=_RECRAWL_MAX_BYTES,
            validators=state.fingerprint.validators,
        )

        if response.not_modified:
            await self.store.mark_not_modified(
                state.crawl_id,
                state.request_url,
                now=current,
            )
            kind = ChangeKind.NOT_MODIFIED
        else:
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"recrawl returned HTTP {response.status_code}")
            record = await self._record_from_http(
                response,
                manifest.seeds,
                fetch_config,
            )
            rendering = record.metadata["rendering_assessment"]
            if self.config.browser_enabled and rendering["requires_browser"]:
                browser_record = await self._refetch_browser(state.request_url, fetch_config)
                if browser_record is not None:
                    record = browser_record
            assessment = await self.store.put_result(
                state.crawl_id,
                state.request_url,
                record,
                now=current,
            )
            if assessment is None:
                raise RuntimeError("recrawl produced no fingerprint")
            kind = assessment.kind

        interval, next_fetch_at = self.policy.next_fetch_at(
            current,
            kind,
            state.recrawl_interval_seconds,
        )
        await self.claims.reschedule(
            claim,
            interval_seconds=interval,
            next_fetch_at=next_fetch_at,
        )
        return kind

    async def _record_from_http(
        self,
        response,
        seeds: tuple[str, ...],
        config: CrawlConfig,
    ) -> PageRecord:
        parsed = parse_html(response.body, response.url)
        links: list[str] = []
        fetcher = SafeRawHttpFetcher(config)
        for link in parsed.links:
            if config.same_domain and not any(same_hostname(seed, link) for seed in seeds):
                continue
            try:
                links.append(await fetcher.egress.validate(link))
            except ValueError:
                continue
        structured = parsed.structured.to_dict()
        fingerprint = fingerprint_page(
            response.body,
            title=parsed.title,
            links=parsed.links,
            structured=structured,
            headers=response.headers,
        )
        rendering = assess_rendering(response.body)
        return PageRecord(
            url=response.url,
            status_code=response.status_code,
            content_type=response.headers.get("content-type"),
            title=parsed.title,
            links=tuple(links),
            engine=response.engine,
            route=response.route,
            metadata={
                "requested_url": response.requested_url,
                "structured": structured,
                "fingerprint": fingerprint.to_dict(),
                "recrawl": True,
                "rendering_assessment": {
                    "requires_browser": rendering.requires_browser,
                    "score": rendering.score,
                    "visible_text_length": rendering.visible_text_length,
                    "script_count": rendering.script_count,
                    "reasons": list(rendering.reasons),
                },
            },
        )

    async def _refetch_browser(
        self,
        url: str,
        config: CrawlConfig,
    ) -> PageRecord | None:
        crawler = LocalCrawler(replace(config, browser_enabled=True))
        return await crawler.scrape(url)


def _failure_interval(
    policy: RecrawlPolicy,
    previous_interval: float | None,
) -> float:
    previous = previous_interval or policy.base_interval_seconds
    return max(
        policy.min_interval_seconds,
        min(policy.base_interval_seconds, previous),
    )
