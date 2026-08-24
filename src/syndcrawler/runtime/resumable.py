from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from syndcrawler.config import CrawlConfig
from syndcrawler.core.change import ChangeAssessment
from syndcrawler.core.crawl import crawl_is_complete, new_crawl_id, validate_crawl_id
from syndcrawler.core.frontier import FrontierRequest
from syndcrawler.core.recrawl import RecrawlPolicy
from syndcrawler.discovery import SitemapDiscoveryClient
from syndcrawler.models import PageRecord
from syndcrawler.runtime.local import LocalCrawler
from syndcrawler.runtime.worker import CrawlWorker
from syndcrawler.storage import CrawlManifest, SQLiteCrawlStore, SQLiteFrontier

_SITEMAP_PRIORITY = -10


@dataclass(frozen=True, slots=True)
class CrawlRunResult:
    crawl_id: str
    state_path: Path
    records: tuple[PageRecord, ...]
    stats: dict[str, int]
    completed: bool


class ResumableCrawler:
    """Crash-resumable local crawl coordinator backed by SQLite.

    SQLite is the source of truth for local work state. Fetch/persist/discovery
    batches run through the same storage-neutral `CrawlWorker` contract that
    server deployments can pair with a Redis frontier and shared result store.
    """

    def __init__(
        self,
        config: CrawlConfig | None = None,
        *,
        state_dir: str | Path = ".syndcrawler",
        recrawl_policy: RecrawlPolicy | None = None,
    ) -> None:
        self.config = config or CrawlConfig()
        self.state_dir = Path(state_dir)
        self.recrawl_policy = recrawl_policy or RecrawlPolicy()

    async def start(
        self,
        seeds: list[str],
        *,
        crawl_id: str | None = None,
        follow_links: bool = True,
        max_pages: int | None = None,
    ) -> CrawlRunResult:
        if not seeds:
            raise ValueError("at least one seed is required")
        crawl_id = validate_crawl_id(crawl_id or new_crawl_id())
        page_limit = max_pages or self.config.max_pages

        fetcher = LocalCrawler(replace(self.config, output=None))
        safe_seeds = tuple([await fetcher.egress.validate(seed) for seed in seeds])
        path = self.state_path(crawl_id)
        if path.exists():
            raise ValueError(f"crawl state already exists: {crawl_id}")

        frontier = SQLiteFrontier(path)
        store = SQLiteCrawlStore(path)
        try:
            await store.create_manifest(
                crawl_id,
                safe_seeds,
                follow_links=follow_links,
                same_domain=self.config.same_domain,
                max_pages=page_limit,
            )
            await frontier.set_crawl_limit(crawl_id, page_limit)
            await frontier.add(
                *(
                    FrontierRequest(
                        seed,
                        crawl_id=crawl_id,
                        metadata={"seed": True},
                    )
                    for seed in safe_seeds
                )
            )
            if follow_links and self.config.sitemap_discovery_enabled:
                await self._seed_from_sitemaps(frontier, crawl_id, safe_seeds)
        finally:
            await store.close()
            await frontier.close()

        return await self.resume(crawl_id)

    async def resume(self, crawl_id: str) -> CrawlRunResult:
        crawl_id = validate_crawl_id(crawl_id)
        path = self.state_path(crawl_id)
        if not path.exists():
            raise ValueError(f"crawl state does not exist: {crawl_id}")

        frontier = SQLiteFrontier(path)
        store = SQLiteCrawlStore(path)
        try:
            manifest = await store.get_manifest(crawl_id)
            if manifest is None:
                raise ValueError(f"crawl manifest does not exist: {crawl_id}")
            await frontier.set_crawl_limit(crawl_id, manifest.max_pages)
            fetcher = LocalCrawler(
                replace(
                    self.config,
                    output=None,
                    same_domain=manifest.same_domain,
                )
            )
            await self._run_until_idle(frontier, store, fetcher, manifest)
            stats = await frontier.stats(crawl_id)
            records = tuple(await store.results(crawl_id))
            completed = crawl_is_complete(stats, manifest.max_pages)
        finally:
            await store.close()
            await frontier.close()

        if self.config.output is not None:
            _write_records(self.config.output, records)
        return CrawlRunResult(
            crawl_id=crawl_id,
            state_path=path,
            records=records,
            stats=stats,
            completed=completed,
        )

    async def status(self, crawl_id: str) -> CrawlRunResult:
        crawl_id = validate_crawl_id(crawl_id)
        path = self.state_path(crawl_id)
        if not path.exists():
            raise ValueError(f"crawl state does not exist: {crawl_id}")
        frontier = SQLiteFrontier(path)
        store = SQLiteCrawlStore(path)
        try:
            manifest = await store.get_manifest(crawl_id)
            if manifest is None:
                raise ValueError(f"crawl manifest does not exist: {crawl_id}")
            stats = await frontier.stats(crawl_id)
            records = tuple(await store.results(crawl_id))
        finally:
            await store.close()
            await frontier.close()
        return CrawlRunResult(
            crawl_id=crawl_id,
            state_path=path,
            records=records,
            stats=stats,
            completed=crawl_is_complete(stats, manifest.max_pages),
        )

    def state_path(self, crawl_id: str) -> Path:
        return self.state_dir / f"{validate_crawl_id(crawl_id)}.sqlite3"

    async def _seed_from_sitemaps(
        self,
        frontier: SQLiteFrontier,
        crawl_id: str,
        seeds: tuple[str, ...],
    ) -> None:
        discovery = SitemapDiscoveryClient(self.config)
        remaining_documents = self.config.sitemap_max_documents
        remaining_urls = self.config.sitemap_max_urls

        for seed in seeds:
            if remaining_documents <= 0 or remaining_urls <= 0:
                return
            result = await discovery.discover(
                seed,
                max_documents=remaining_documents,
                max_depth=self.config.sitemap_max_depth,
                max_urls=remaining_urls,
            )
            remaining_documents -= len(result.documents_fetched)
            remaining_urls -= len(result.urls)
            if not result.urls:
                continue
            await frontier.add(
                *(
                    FrontierRequest(
                        item.url,
                        crawl_id=crawl_id,
                        priority=_SITEMAP_PRIORITY,
                        metadata={
                            "discovered_via": item.via,
                            "discovery_source": item.source_url,
                            "discovery_depth": item.depth,
                        },
                    )
                    for item in result.urls
                )
            )

    async def _run_until_idle(
        self,
        frontier: SQLiteFrontier,
        store: SQLiteCrawlStore,
        fetcher: LocalCrawler,
        manifest: CrawlManifest,
    ) -> None:
        async def schedule_recrawl(
            request_url: str,
            _record: PageRecord,
            assessment: ChangeAssessment | None,
            observed_at: float,
        ) -> None:
            if assessment is None:
                return
            state = await store.get_resource_state(manifest.crawl_id, request_url)
            previous_interval = state.recrawl_interval_seconds if state is not None else None
            interval, next_fetch_at = self.recrawl_policy.next_fetch_at(
                observed_at,
                assessment.kind,
                previous_interval,
            )
            await store.schedule_resource(
                manifest.crawl_id,
                request_url,
                interval_seconds=interval,
                next_fetch_at=next_fetch_at,
            )

        worker = CrawlWorker(
            frontier,
            store,
            fetcher,
            on_persisted=schedule_recrawl,
        )
        lease_seconds = max(
            60.0,
            self.config.browser_navigation_timeout_seconds + 30.0,
        )

        while True:
            stats = await frontier.stats(manifest.crawl_id)
            terminal = stats["done"] + stats["failed"]
            remaining = manifest.max_pages - terminal
            if remaining <= 0:
                return

            batch = await worker.run_batch(
                manifest.crawl_id,
                scope_urls=manifest.seeds,
                follow_links=manifest.follow_links,
                limit=min(self.config.max_concurrency, remaining),
                lease_seconds=lease_seconds,
                namespace_prefix="durable",
            )
            if batch.idle:
                return


def _write_records(path: Path, records: tuple[PageRecord, ...]) -> None:
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
