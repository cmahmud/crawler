from __future__ import annotations

import asyncio
import heapq
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from syndcrawler.core.url import canonicalize_url, hostname


class RequestState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FrontierRequest:
    url: str
    crawl_id: str = "default"
    queue_key: str | None = None
    priority: int = 0
    depth: int = 0
    available_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> FrontierRequest:
        url = canonicalize_url(self.url)
        return replace(self, url=url, queue_key=self.queue_key or hostname(url))


@dataclass(frozen=True, slots=True)
class FrontierLease:
    request: FrontierRequest
    token: str
    attempt: int
    leased_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    max_concurrency: int = 1
    delay_seconds: float = 0.0
    blocked_until: float = 0.0
    crawl_limit: int | None = None


@dataclass(slots=True)
class _Entry:
    request: FrontierRequest
    sequence: int
    state: RequestState = RequestState.QUEUED
    attempt: int = 0
    lease_token: str | None = None
    lease_expires_at: float = 0.0
    error: str | None = None


@dataclass(slots=True)
class _QueueRuntime:
    policy: QueuePolicy = field(default_factory=QueuePolicy)
    active: int = 0
    issued: int = 0
    next_allowed_at: float = 0.0


class MemoryFrontier:
    """Lease-based local frontier with host-affine politeness semantics.

    It is intentionally storage-neutral in behavior so Redis/SQL/frontier-service
    backends can implement the same contract later without changing workers.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, _Entry]] = {}
        self._queues: dict[tuple[str, str], _QueueRuntime] = {}
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def add(self, *requests: FrontierRequest) -> int:
        async with self._lock:
            added = 0
            for raw in requests:
                request = raw.normalized()
                crawl = self._entries.setdefault(request.crawl_id, {})
                if request.url in crawl:
                    continue
                crawl[request.url] = _Entry(request=request, sequence=self._sequence)
                self._sequence += 1
                added += 1
            return added

    async def set_queue_policy(
        self,
        crawl_id: str,
        queue_key: str,
        policy: QueuePolicy,
    ) -> None:
        if policy.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if policy.delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        if policy.crawl_limit is not None and policy.crawl_limit <= 0:
            raise ValueError("crawl_limit must be positive")
        async with self._lock:
            self._queue(crawl_id, queue_key).policy = policy

    async def lease(
        self,
        crawl_id: str,
        *,
        limit: int = 1,
        lease_seconds: float = 60.0,
        now: float | None = None,
    ) -> list[FrontierLease]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        current = time.time() if now is None else now
        async with self._lock:
            crawl = self._entries.setdefault(crawl_id, {})
            self._reclaim_expired(crawl, current)

            heap: list[tuple[int, float, int, str]] = []
            for url, entry in crawl.items():
                if entry.state != RequestState.QUEUED or entry.request.available_at > current:
                    continue
                heapq.heappush(
                    heap,
                    (
                        -entry.request.priority,
                        entry.request.available_at,
                        entry.sequence,
                        url,
                    ),
                )

            leases: list[FrontierLease] = []
            while heap and len(leases) < limit:
                _, _, _, url = heapq.heappop(heap)
                entry = crawl[url]
                key = entry.request.queue_key
                assert key is not None
                queue = self._queue(crawl_id, key)
                policy = queue.policy

                if policy.blocked_until > current or queue.next_allowed_at > current:
                    continue
                if queue.active >= policy.max_concurrency:
                    continue
                if policy.crawl_limit is not None and queue.issued >= policy.crawl_limit:
                    continue

                token = uuid.uuid4().hex
                entry.state = RequestState.LEASED
                entry.attempt += 1
                entry.lease_token = token
                entry.lease_expires_at = current + lease_seconds
                queue.active += 1
                queue.issued += 1
                if policy.delay_seconds:
                    queue.next_allowed_at = current + policy.delay_seconds

                leases.append(
                    FrontierLease(
                        request=entry.request,
                        token=token,
                        attempt=entry.attempt,
                        leased_at=current,
                        expires_at=entry.lease_expires_at,
                    )
                )

            return leases

    async def ack(self, lease: FrontierLease) -> None:
        async with self._lock:
            entry = self._active_entry(lease)
            self._release_queue(entry)
            entry.state = RequestState.DONE
            entry.lease_token = None
            entry.lease_expires_at = 0.0

    async def retry(
        self,
        lease: FrontierLease,
        *,
        available_at: float,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            entry = self._active_entry(lease)
            self._release_queue(entry)
            entry.request = replace(entry.request, available_at=available_at)
            entry.state = RequestState.QUEUED
            entry.error = error
            entry.lease_token = None
            entry.lease_expires_at = 0.0

    async def fail(self, lease: FrontierLease, *, error: str) -> None:
        async with self._lock:
            entry = self._active_entry(lease)
            self._release_queue(entry)
            entry.state = RequestState.FAILED
            entry.error = error
            entry.lease_token = None
            entry.lease_expires_at = 0.0

    async def stats(self, crawl_id: str) -> dict[str, int]:
        async with self._lock:
            counts = {state.value: 0 for state in RequestState}
            for entry in self._entries.get(crawl_id, {}).values():
                counts[entry.state.value] += 1
            return counts

    def _queue(self, crawl_id: str, queue_key: str) -> _QueueRuntime:
        return self._queues.setdefault((crawl_id, queue_key), _QueueRuntime())

    def _active_entry(self, lease: FrontierLease) -> _Entry:
        entry = self._entries.get(lease.request.crawl_id, {}).get(lease.request.url)
        if entry is None or entry.state != RequestState.LEASED or entry.lease_token != lease.token:
            raise RuntimeError(f"lease is no longer active for {lease.request.url}")
        return entry

    def _release_queue(self, entry: _Entry) -> None:
        key = entry.request.queue_key
        assert key is not None
        queue = self._queue(entry.request.crawl_id, key)
        queue.active = max(0, queue.active - 1)

    def _reclaim_expired(self, crawl: dict[str, _Entry], now: float) -> None:
        for entry in crawl.values():
            if entry.state != RequestState.LEASED or entry.lease_expires_at > now:
                continue
            self._release_queue(entry)
            entry.state = RequestState.QUEUED
            entry.lease_token = None
            entry.lease_expires_at = 0.0
