from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from syndcrawler.config import CrawlConfig
from syndcrawler.core.frontier import FrontierRequest
from syndcrawler.core.url import canonicalize_url
from syndcrawler.models import PageRecord
from syndcrawler.runtime.local import LocalCrawler
from syndcrawler.storage import CrawlManifest, SQLiteCrawlStore, SQLiteFrontier

_CRAWL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class CrawlRunResult:
    crawl_id: str
    state_path: Path
    records: tuple[PageRecord, ...]
    stats: dict[str, int]
    completed: bool


class ResumableCrawler:
    """Crash-resumable local crawl coordinator backed by SQLite.

    SQLite is the source of truth for work state. Crawlee remains the bounded batch
    fetch engine, so HTTP autoscaling, browser escalation and network-route policy
    stay identical to ordinary local crawls.
    """

    def __init__(
        self,
        config: CrawlConfig | None = None,
        *,
        state_dir: str | Path = ".syndcrawler",
    ) -> None:
        self.config = config or CrawlConfig()
        self.state_dir = Path(state_dir)

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
        crawl_id = crawl_id or f"cr_{uuid.uuid4().hex[:16]}"
        _validate_crawl_id(crawl_id)
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
        finally:
            await store.close()
            await frontier.close()

        return await self.resume(crawl_id)

    async def resume(self, crawl_id: str) -> CrawlRunResult:
        _validate_crawl_id(crawl_id)
        path = self.state_path(crawl_id)
        if not path.exists():
            raise ValueError(f"crawl state does not exist: {crawl_id}")

        frontier = SQLiteFrontier(path)
        store = SQLiteCrawlStore(path)
        try:
            manifest = await store.get_manifest(crawl_id)
            if manifest is None:
                raise ValueError(f"crawl manifest does not exist: {crawl_id}")
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
            completed = _is_complete(stats, manifest.max_pages)
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
        _validate_crawl_id(crawl_id)
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
            completed=_is_complete(stats, manifest.max_pages),
        )

    def state_path(self, crawl_id: str) -> Path:
        _validate_crawl_id(crawl_id)
        return self.state_dir / f"{crawl_id}.sqlite3"

    async def _run_until_idle(
        self,
        frontier: SQLiteFrontier,
        store: SQLiteCrawlStore,
        fetcher: LocalCrawler,
        manifest: CrawlManifest,
    ) -> None:
        while True:
            stats = await frontier.stats(manifest.crawl_id)
            terminal = stats["done"] + stats["failed"]
            remaining = manifest.max_pages - terminal
            if remaining <= 0:
                return

            leases = await frontier.lease(
                manifest.crawl_id,
                limit=min(self.config.max_concurrency, remaining),
                lease_seconds=max(
                    60.0,
                    self.config.browser_navigation_timeout_seconds + 30.0,
                ),
            )
            if not leases:
                return

            namespace = f"durable:{manifest.crawl_id}:{uuid.uuid4().hex}"
            batch = await fetcher.crawl(
                [lease.request.url for lease in leases],
                follow_links=False,
                max_pages=len(leases),
                scope_urls=list(manifest.seeds),
                request_namespace=namespace,
            )
            by_request_url = _index_records(batch)

            for lease in leases:
                record = by_request_url.get(lease.request.url)
                if record is None:
                    await frontier.fail(
                        lease,
                        error="fetch batch produced no PageRecord",
                    )
                    continue

                await store.put_result(
                    manifest.crawl_id,
                    lease.request.url,
                    record,
                )
                if manifest.follow_links:
                    await frontier.add(
                        *(
                            FrontierRequest(
                                link,
                                crawl_id=manifest.crawl_id,
                                depth=lease.request.depth + 1,
                                metadata={"discovered_from": lease.request.url},
                            )
                            for link in record.links
                        )
                    )
                await frontier.ack(lease)


def _index_records(records: list[PageRecord]) -> dict[str, PageRecord]:
    indexed: dict[str, PageRecord] = {}
    for record in records:
        requested = record.metadata.get("requested_url")
        candidates = [requested, record.url]
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            try:
                indexed.setdefault(canonicalize_url(candidate), record)
            except (ValueError, UnicodeError):
                continue
    return indexed


def _is_complete(stats: dict[str, int], max_pages: int) -> bool:
    terminal = stats["done"] + stats["failed"]
    return terminal >= max_pages or (stats["queued"] == 0 and stats["leased"] == 0)


def _validate_crawl_id(crawl_id: str) -> None:
    if not _CRAWL_ID_RE.fullmatch(crawl_id):
        raise ValueError(
            "crawl_id must be 1-128 characters using letters, numbers, '.', '_' or '-'"
        )


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
