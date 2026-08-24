from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from syndcrawler.core.change import ChangeAssessment
from syndcrawler.core.frontier import Frontier, FrontierRequest
from syndcrawler.core.results import ResultStore
from syndcrawler.core.url import canonicalize_url
from syndcrawler.models import PageRecord


class BatchFetcher(Protocol):
    """Fetch surface required by a crawl worker.

    `LocalCrawler` satisfies this protocol, but tests and future remote fetch
    executors can provide the same method without inheriting from it.
    """

    async def crawl(
        self,
        seeds: list[str],
        *,
        follow_links: bool = True,
        max_pages: int | None = None,
        scope_urls: list[str] | None = None,
        request_namespace: str | None = None,
    ) -> list[PageRecord]: ...


PersistedHook = Callable[
    [str, PageRecord, ChangeAssessment | None, float],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class WorkerBatchResult:
    leased: int
    persisted: int
    failed: int
    discovered: int

    @property
    def idle(self) -> bool:
        return self.leased == 0


class CrawlWorker:
    """Process one bounded frontier batch without owning crawl orchestration.

    The ordering is intentionally crash-safe and replay-friendly:

    1. lease requests from the frontier,
    2. fetch the batch,
    3. persist each result,
    4. run the optional post-persist hook,
    5. enqueue discovered links,
    6. acknowledge the lease.

    If persistence, scheduling, discovery, or acknowledgement raises, the active
    lease is left for the frontier's stale-lease recovery. Result writes and URL
    insertion are expected to be idempotent, so another worker can safely replay
    the request after lease expiry.
    """

    def __init__(
        self,
        frontier: Frontier,
        store: ResultStore,
        fetcher: BatchFetcher,
        *,
        on_persisted: PersistedHook | None = None,
    ) -> None:
        self.frontier = frontier
        self.store = store
        self.fetcher = fetcher
        self.on_persisted = on_persisted

    async def run_batch(
        self,
        crawl_id: str,
        *,
        scope_urls: tuple[str, ...],
        follow_links: bool,
        limit: int,
        lease_seconds: float,
        namespace_prefix: str = "worker",
    ) -> WorkerBatchResult:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not scope_urls:
            raise ValueError("scope_urls cannot be empty")

        leases = await self.frontier.lease(
            crawl_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        if not leases:
            return WorkerBatchResult(leased=0, persisted=0, failed=0, discovered=0)

        namespace = f"{namespace_prefix}:{crawl_id}:{uuid.uuid4().hex}"
        batch = await self.fetcher.crawl(
            [lease.request.url for lease in leases],
            follow_links=False,
            max_pages=len(leases),
            scope_urls=list(scope_urls),
            request_namespace=namespace,
        )
        by_request_url = _index_records(batch)

        persisted = 0
        failed = 0
        discovered = 0
        for lease in leases:
            record = by_request_url.get(lease.request.url)
            if record is None:
                await self.frontier.fail(
                    lease,
                    error="fetch batch produced no PageRecord",
                )
                failed += 1
                continue

            observed_at = time.time()
            assessment = await self.store.put_result(
                crawl_id,
                lease.request.url,
                record,
                now=observed_at,
            )
            persisted += 1

            if self.on_persisted is not None:
                await self.on_persisted(
                    lease.request.url,
                    record,
                    assessment,
                    observed_at,
                )

            if follow_links and record.links:
                discovered += await self.frontier.add(
                    *(
                        FrontierRequest(
                            link,
                            crawl_id=crawl_id,
                            depth=lease.request.depth + 1,
                            metadata={"discovered_from": lease.request.url},
                        )
                        for link in record.links
                    )
                )
            await self.frontier.ack(lease)

        return WorkerBatchResult(
            leased=len(leases),
            persisted=persisted,
            failed=failed,
            discovered=discovered,
        )


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
