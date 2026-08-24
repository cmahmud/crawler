from __future__ import annotations

from dataclasses import dataclass, replace

from syndcrawler.config import CrawlConfig
from syndcrawler.core.change import ChangeAssessment
from syndcrawler.core.crawl import crawl_is_complete, new_crawl_id, validate_crawl_id
from syndcrawler.core.frontier import FrontierRequest
from syndcrawler.core.recrawl import RecrawlPolicy
from syndcrawler.discovery import SitemapDiscoveryClient
from syndcrawler.models import PageRecord
from syndcrawler.runtime.local import LocalCrawler
from syndcrawler.runtime.worker import CrawlWorker, WorkerBatchResult
from syndcrawler.storage import CrawlManifest, PostgresCrawlStore, RedisFrontier

_SITEMAP_PRIORITY = -10


@dataclass(frozen=True, slots=True)
class ServerCrawlStatus:
    crawl_id: str
    result_count: int
    stats: dict[str, int]
    completed: bool


class ServerRuntime:
    """Single-VPS or multi-worker runtime backed by Redis and PostgreSQL.

    Redis owns work leasing, host-affine politeness and the atomic crawl-wide page
    budget. PostgreSQL owns manifests, latest records, validators and recrawl state.
    Any number of runtime instances can call `run_batch()` for the same crawl.
    """

    def __init__(
        self,
        frontier: RedisFrontier,
        store: PostgresCrawlStore,
        config: CrawlConfig | None = None,
        *,
        recrawl_policy: RecrawlPolicy | None = None,
        owns_resources: bool = False,
    ) -> None:
        self.frontier = frontier
        self.store = store
        self.config = config or CrawlConfig()
        self.recrawl_policy = recrawl_policy or RecrawlPolicy()
        self._owns_resources = owns_resources

    @classmethod
    async def from_urls(
        cls,
        *,
        redis_url: str,
        postgres_dsn: str,
        config: CrawlConfig | None = None,
        redis_key_prefix: str = "syndcrawler",
        postgres_min_pool_size: int = 1,
        postgres_max_pool_size: int = 10,
        recrawl_policy: RecrawlPolicy | None = None,
    ) -> ServerRuntime:
        frontier = RedisFrontier.from_url(redis_url, key_prefix=redis_key_prefix)
        try:
            store = await PostgresCrawlStore.from_dsn(
                postgres_dsn,
                min_size=postgres_min_pool_size,
                max_size=postgres_max_pool_size,
            )
        except Exception:
            await frontier.close()
            raise
        return cls(
            frontier,
            store,
            config,
            recrawl_policy=recrawl_policy,
            owns_resources=True,
        )

    async def close(self) -> None:
        if not self._owns_resources:
            return
        try:
            await self.frontier.close()
        finally:
            await self.store.close()

    async def submit(
        self,
        seeds: list[str],
        *,
        crawl_id: str | None = None,
        follow_links: bool = True,
        max_pages: int | None = None,
    ) -> ServerCrawlStatus:
        if not seeds:
            raise ValueError("at least one seed is required")
        crawl_id = validate_crawl_id(crawl_id or new_crawl_id())
        page_limit = max_pages or self.config.max_pages

        validator = LocalCrawler(replace(self.config, output=None))
        safe_seeds = tuple([await validator.egress.validate(seed) for seed in seeds])
        manifest = await self.store.get_manifest(crawl_id)
        if manifest is None:
            manifest = await self.store.create_manifest(
                crawl_id,
                safe_seeds,
                follow_links=follow_links,
                same_domain=self.config.same_domain,
                max_pages=page_limit,
            )
        else:
            _require_matching_manifest(
                manifest,
                safe_seeds,
                follow_links=follow_links,
                same_domain=self.config.same_domain,
                max_pages=page_limit,
            )

        await self.frontier.set_crawl_limit(crawl_id, manifest.max_pages)
        await self.frontier.add(
            *(
                FrontierRequest(
                    seed,
                    crawl_id=crawl_id,
                    metadata={"seed": True},
                )
                for seed in manifest.seeds
            )
        )
        if manifest.follow_links and self.config.sitemap_discovery_enabled:
            await self._seed_from_sitemaps(manifest)
        return await self.status(crawl_id)

    async def status(self, crawl_id: str) -> ServerCrawlStatus:
        crawl_id = validate_crawl_id(crawl_id)
        manifest = await self._manifest(crawl_id)
        stats = await self.frontier.stats(crawl_id)
        return ServerCrawlStatus(
            crawl_id=crawl_id,
            result_count=await self.store.result_count(crawl_id),
            stats=stats,
            completed=crawl_is_complete(stats, manifest.max_pages),
        )

    async def results(self, crawl_id: str) -> list[PageRecord]:
        await self._manifest(validate_crawl_id(crawl_id))
        return await self.store.results(crawl_id)

    async def run_batch(
        self,
        crawl_id: str,
        *,
        limit: int | None = None,
    ) -> WorkerBatchResult:
        crawl_id = validate_crawl_id(crawl_id)
        manifest = await self._manifest(crawl_id)
        await self.frontier.set_crawl_limit(crawl_id, manifest.max_pages)
        fetcher = LocalCrawler(
            replace(
                self.config,
                output=None,
                same_domain=manifest.same_domain,
            )
        )

        async def schedule_recrawl(
            request_url: str,
            _record: PageRecord,
            assessment: ChangeAssessment | None,
            observed_at: float,
        ) -> None:
            if assessment is None:
                return
            state = await self.store.get_resource_state(crawl_id, request_url)
            previous_interval = state.recrawl_interval_seconds if state is not None else None
            interval, next_fetch_at = self.recrawl_policy.next_fetch_at(
                observed_at,
                assessment.kind,
                previous_interval,
            )
            await self.store.schedule_resource(
                crawl_id,
                request_url,
                interval_seconds=interval,
                next_fetch_at=next_fetch_at,
            )

        worker = CrawlWorker(
            self.frontier,
            self.store,
            fetcher,
            on_persisted=schedule_recrawl,
        )
        return await worker.run_batch(
            crawl_id,
            scope_urls=manifest.seeds,
            follow_links=manifest.follow_links,
            limit=limit or self.config.max_concurrency,
            lease_seconds=max(
                60.0,
                self.config.browser_navigation_timeout_seconds + 30.0,
            ),
            namespace_prefix="server",
        )

    async def run_until_idle(self, crawl_id: str) -> ServerCrawlStatus:
        crawl_id = validate_crawl_id(crawl_id)
        while True:
            status = await self.status(crawl_id)
            if status.completed:
                return status
            batch = await self.run_batch(crawl_id)
            if batch.idle:
                return await self.status(crawl_id)

    async def _manifest(self, crawl_id: str) -> CrawlManifest:
        manifest = await self.store.get_manifest(crawl_id)
        if manifest is None:
            raise ValueError(f"crawl manifest does not exist: {crawl_id}")
        return manifest

    async def _seed_from_sitemaps(self, manifest: CrawlManifest) -> None:
        discovery = SitemapDiscoveryClient(self.config)
        remaining_documents = self.config.sitemap_max_documents
        remaining_urls = self.config.sitemap_max_urls
        for seed in manifest.seeds:
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
            await self.frontier.add(
                *(
                    FrontierRequest(
                        item.url,
                        crawl_id=manifest.crawl_id,
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


def _require_matching_manifest(
    manifest: CrawlManifest,
    seeds: tuple[str, ...],
    *,
    follow_links: bool,
    same_domain: bool,
    max_pages: int,
) -> None:
    if (
        manifest.seeds != seeds
        or manifest.follow_links != follow_links
        or manifest.same_domain != same_domain
        or manifest.max_pages != max_pages
    ):
        raise ValueError(
            f"crawl already exists with different configuration: {manifest.crawl_id}"
        )
