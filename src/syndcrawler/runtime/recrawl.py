from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from syndcrawler.config import CrawlConfig
from syndcrawler.core.change import ChangeKind, fingerprint_page
from syndcrawler.core.frontier import FrontierRequest
from syndcrawler.core.recrawl import RecrawlPolicy
from syndcrawler.core.rendering import assess_rendering
from syndcrawler.core.url import canonicalize_url, same_hostname
from syndcrawler.models import PageRecord
from syndcrawler.runtime.html import parse_html
from syndcrawler.runtime.local import LocalCrawler
from syndcrawler.runtime.raw_http import SafeRawHttpFetcher
from syndcrawler.storage import SQLiteCrawlStore, SQLiteFrontier

_CRAWL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RECRAWL_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RecrawlItemResult:
    url: str
    change_kind: ChangeKind | None
    status_code: int | None
    next_fetch_at: float | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RecrawlRunResult:
    crawl_id: str
    checked: int
    not_modified: int
    unchanged: int
    representation_changed: int
    semantic_changed: int
    failures: int
    items: tuple[RecrawlItemResult, ...]


class RecrawlRunner:
    """Run bounded conditional recrawls against durable local crawl state."""

    def __init__(
        self,
        config: CrawlConfig | None = None,
        *,
        state_dir: str | Path = ".syndcrawler",
        policy: RecrawlPolicy | None = None,
    ) -> None:
        self.config = config or CrawlConfig()
        self.state_dir = Path(state_dir)
        self.policy = policy or RecrawlPolicy()

    async def run(
        self,
        crawl_id: str,
        *,
        limit: int = 100,
        force: bool = False,
        now: float | None = None,
    ) -> RecrawlRunResult:
        _validate_crawl_id(crawl_id)
        if limit <= 0:
            raise ValueError("limit must be positive")
        current = time.time() if now is None else now
        path = self.state_dir / f"{crawl_id}.sqlite3"
        if not path.exists():
            raise ValueError(f"crawl state does not exist: {crawl_id}")

        store = SQLiteCrawlStore(path)
        frontier = SQLiteFrontier(path)
        items: list[RecrawlItemResult] = []
        try:
            manifest = await store.get_manifest(crawl_id)
            if manifest is None:
                raise ValueError(f"crawl manifest does not exist: {crawl_id}")
            states = (
                (await store.resource_states(crawl_id))[:limit]
                if force
                else await store.due_resources(crawl_id, now=current, limit=limit)
            )
            latest = _index_records(await store.results(crawl_id))
            fetch_config = replace(
                self.config,
                output=None,
                same_domain=manifest.same_domain,
            )
            raw_fetcher = SafeRawHttpFetcher(fetch_config)

            for state in states:
                previous_record = latest.get(state.request_url)
                try:
                    if previous_record is not None and previous_record.engine == "browser":
                        record = await self._refetch_browser(
                            state.request_url,
                            fetch_config,
                        )
                        if record is None:
                            raise RuntimeError("browser recrawl produced no PageRecord")
                        assessment = await store.put_result(
                            crawl_id,
                            state.request_url,
                            record,
                            now=current,
                        )
                        if assessment is None:
                            raise RuntimeError("browser recrawl produced no fingerprint")
                        await self._schedule(
                            store,
                            crawl_id,
                            state.request_url,
                            assessment.kind,
                            state.recrawl_interval_seconds,
                            current,
                        )
                        await self._enqueue_new_links(
                            frontier,
                            crawl_id,
                            record,
                            manifest.follow_links,
                        )
                        scheduled = await store.get_resource_state(
                            crawl_id,
                            state.request_url,
                        )
                        items.append(
                            RecrawlItemResult(
                                url=state.request_url,
                                change_kind=assessment.kind,
                                status_code=record.status_code,
                                next_fetch_at=(
                                    scheduled.next_fetch_at if scheduled else None
                                ),
                            )
                        )
                        continue

                    response = await raw_fetcher.fetch(
                        state.request_url,
                        max_bytes=_RECRAWL_MAX_BYTES,
                        validators=state.fingerprint.validators,
                    )
                    if response.not_modified:
                        await store.mark_not_modified(
                            crawl_id,
                            state.request_url,
                            now=current,
                        )
                        await self._schedule(
                            store,
                            crawl_id,
                            state.request_url,
                            ChangeKind.NOT_MODIFIED,
                            state.recrawl_interval_seconds,
                            current,
                        )
                        scheduled = await store.get_resource_state(
                            crawl_id,
                            state.request_url,
                        )
                        items.append(
                            RecrawlItemResult(
                                url=state.request_url,
                                change_kind=ChangeKind.NOT_MODIFIED,
                                status_code=304,
                                next_fetch_at=(
                                    scheduled.next_fetch_at if scheduled else None
                                ),
                            )
                        )
                        continue

                    if not 200 <= response.status_code < 300:
                        raise RuntimeError(f"recrawl returned HTTP {response.status_code}")

                    record = await self._record_from_http(
                        response,
                        manifest.seeds,
                        fetch_config,
                    )
                    if (
                        self.config.browser_enabled
                        and record.metadata["rendering_assessment"]["requires_browser"]
                    ):
                        browser_record = await self._refetch_browser(
                            state.request_url,
                            fetch_config,
                        )
                        if browser_record is not None:
                            record = browser_record

                    assessment = await store.put_result(
                        crawl_id,
                        state.request_url,
                        record,
                        now=current,
                    )
                    if assessment is None:
                        raise RuntimeError("recrawl produced no fingerprint")
                    await self._schedule(
                        store,
                        crawl_id,
                        state.request_url,
                        assessment.kind,
                        state.recrawl_interval_seconds,
                        current,
                    )
                    await self._enqueue_new_links(
                        frontier,
                        crawl_id,
                        record,
                        manifest.follow_links,
                    )
                    scheduled = await store.get_resource_state(
                        crawl_id,
                        state.request_url,
                    )
                    items.append(
                        RecrawlItemResult(
                            url=state.request_url,
                            change_kind=assessment.kind,
                            status_code=record.status_code,
                            next_fetch_at=(scheduled.next_fetch_at if scheduled else None),
                        )
                    )
                except Exception as exc:
                    interval = _failure_interval(
                        self.policy,
                        state.recrawl_interval_seconds,
                    )
                    await store.schedule_resource(
                        crawl_id,
                        state.request_url,
                        interval_seconds=interval,
                        next_fetch_at=current + interval,
                    )
                    items.append(
                        RecrawlItemResult(
                            url=state.request_url,
                            change_kind=None,
                            status_code=None,
                            next_fetch_at=current + interval,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
        finally:
            await store.close()
            await frontier.close()

        counts = {kind: 0 for kind in ChangeKind}
        failures = 0
        for item in items:
            if item.change_kind is None:
                failures += 1
            else:
                counts[item.change_kind] += 1
        return RecrawlRunResult(
            crawl_id=crawl_id,
            checked=len(items),
            not_modified=counts[ChangeKind.NOT_MODIFIED],
            unchanged=counts[ChangeKind.UNCHANGED],
            representation_changed=counts[ChangeKind.REPRESENTATION_CHANGED],
            semantic_changed=counts[ChangeKind.SEMANTIC_CHANGED],
            failures=failures,
            items=tuple(items),
        )

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

    async def _schedule(
        self,
        store: SQLiteCrawlStore,
        crawl_id: str,
        url: str,
        kind: ChangeKind,
        previous_interval: float | None,
        now: float,
    ) -> None:
        interval, next_fetch_at = self.policy.next_fetch_at(
            now,
            kind,
            previous_interval,
        )
        await store.schedule_resource(
            crawl_id,
            url,
            interval_seconds=interval,
            next_fetch_at=next_fetch_at,
        )

    async def _enqueue_new_links(
        self,
        frontier: SQLiteFrontier,
        crawl_id: str,
        record: PageRecord,
        follow_links: bool,
    ) -> None:
        if not follow_links or not record.links:
            return
        await frontier.add(
            *(
                FrontierRequest(
                    link,
                    crawl_id=crawl_id,
                    metadata={"discovered_from_recrawl": record.url},
                )
                for link in record.links
            )
        )


def _index_records(records: list[PageRecord]) -> dict[str, PageRecord]:
    indexed: dict[str, PageRecord] = {}
    for record in records:
        requested = record.metadata.get("requested_url")
        for candidate in (requested, record.url):
            if not isinstance(candidate, str):
                continue
            try:
                indexed[canonicalize_url(candidate)] = record
            except (ValueError, UnicodeError):
                continue
    return indexed


def _failure_interval(
    policy: RecrawlPolicy,
    previous_interval: float | None,
) -> float:
    previous = previous_interval or policy.base_interval_seconds
    return max(
        policy.min_interval_seconds,
        min(policy.base_interval_seconds, previous),
    )


def _validate_crawl_id(crawl_id: str) -> None:
    if not _CRAWL_ID_RE.fullmatch(crawl_id):
        raise ValueError(
            "crawl_id must be 1-128 characters using letters, numbers, '.', '_' or '-'"
        )
