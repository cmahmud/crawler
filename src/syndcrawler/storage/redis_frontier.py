from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from syndcrawler.core.frontier import (
    FrontierLease,
    FrontierRequest,
    QueuePolicy,
    RequestState,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

_ADD_SCRIPT = r"""
local requests = cjson.decode(ARGV[1])
local now = tonumber(ARGV[2])
local added = 0
for _, entry in ipairs(requests) do
    if redis.call('HEXISTS', KEYS[1], entry.url) == 0 then
        entry.sequence = redis.call('INCR', KEYS[5])
        entry.ready_member = string.format('%020d|%s', entry.sequence, entry.url)
        entry.state = 'queued'
        entry.attempt = 0
        entry.lease_token = cjson.null
        entry.lease_expires_at = 0
        entry.error = cjson.null
        redis.call('HSET', KEYS[1], entry.url, cjson.encode(entry))
        if tonumber(entry.available_at) <= now then
            redis.call('ZADD', KEYS[2], -tonumber(entry.priority or 0), entry.ready_member)
        else
            redis.call('ZADD', KEYS[3], tonumber(entry.available_at), entry.url)
        end
        redis.call('HINCRBY', KEYS[6], 'queued', 1)
        if redis.call('HEXISTS', KEYS[4], entry.queue_key) == 0 then
            local queue = {
                max_concurrency = 1,
                delay_seconds = 0,
                blocked_until = 0,
                active = 0,
                issued = 0,
                next_allowed_at = 0
            }
            redis.call('HSET', KEYS[4], entry.queue_key, cjson.encode(queue))
        end
        added = added + 1
    end
end
return added
"""

_SET_QUEUE_POLICY_SCRIPT = r"""
local raw = redis.call('HGET', KEYS[1], ARGV[1])
local queue
if raw then
    queue = cjson.decode(raw)
else
    queue = {
        max_concurrency = 1,
        delay_seconds = 0,
        blocked_until = 0,
        active = 0,
        issued = 0,
        next_allowed_at = 0
    }
end
queue.max_concurrency = tonumber(ARGV[2])
queue.delay_seconds = tonumber(ARGV[3])
queue.blocked_until = tonumber(ARGV[4])
if ARGV[5] == '' then
    queue.crawl_limit = cjson.null
else
    queue.crawl_limit = tonumber(ARGV[5])
end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(queue))
return 1
"""

_LEASE_SCRIPT = r"""
local now = tonumber(ARGV[1])
local lease_seconds = tonumber(ARGV[2])
local wanted = tonumber(ARGV[3])
local scan_limit = tonumber(ARGV[4])
local maintenance_limit = tonumber(ARGV[5])

local expired = redis.call(
    'ZRANGEBYSCORE', KEYS[4], '-inf', now, 'LIMIT', 0, maintenance_limit
)
for _, url in ipairs(expired) do
    local raw = redis.call('HGET', KEYS[1], url)
    if raw then
        local entry = cjson.decode(raw)
        if entry.state == 'leased' then
            local qraw = redis.call('HGET', KEYS[5], entry.queue_key)
            if qraw then
                local queue = cjson.decode(qraw)
                queue.active = math.max(0, tonumber(queue.active or 0) - 1)
                redis.call('HSET', KEYS[5], entry.queue_key, cjson.encode(queue))
            end
            entry.state = 'queued'
            entry.lease_token = cjson.null
            entry.lease_expires_at = 0
            redis.call('HSET', KEYS[1], url, cjson.encode(entry))
            redis.call('ZREM', KEYS[4], url)
            redis.call(
                'ZADD', KEYS[2], -tonumber(entry.priority or 0), entry.ready_member
            )
            redis.call('HINCRBY', KEYS[7], 'leased', -1)
            redis.call('HINCRBY', KEYS[7], 'queued', 1)
        else
            redis.call('ZREM', KEYS[4], url)
        end
    else
        redis.call('ZREM', KEYS[4], url)
    end
end

local due = redis.call(
    'ZRANGEBYSCORE', KEYS[3], '-inf', now, 'LIMIT', 0, maintenance_limit
)
for _, url in ipairs(due) do
    local raw = redis.call('HGET', KEYS[1], url)
    if raw then
        local entry = cjson.decode(raw)
        if entry.state == 'queued' then
            redis.call('ZREM', KEYS[3], url)
            redis.call('ZADD', KEYS[2], -tonumber(entry.priority or 0), entry.ready_member)
        else
            redis.call('ZREM', KEYS[3], url)
        end
    else
        redis.call('ZREM', KEYS[3], url)
    end
end

local members = redis.call('ZRANGE', KEYS[2], 0, scan_limit - 1)
local leases = {}
for _, member in ipairs(members) do
    if #leases >= wanted then
        break
    end
    local separator = string.find(member, '|', 1, true)
    if separator then
        local url = string.sub(member, separator + 1)
        local raw = redis.call('HGET', KEYS[1], url)
        if raw then
            local entry = cjson.decode(raw)
            if entry.state == 'queued' then
                local qraw = redis.call('HGET', KEYS[5], entry.queue_key)
                local queue
                if qraw then
                    queue = cjson.decode(qraw)
                else
                    queue = {
                        max_concurrency = 1,
                        delay_seconds = 0,
                        blocked_until = 0,
                        active = 0,
                        issued = 0,
                        next_allowed_at = 0
                    }
                end

                local crawl_limit = queue.crawl_limit
                local under_limit = crawl_limit == nil or crawl_limit == cjson.null
                    or tonumber(queue.issued or 0) < tonumber(crawl_limit)
                local allowed = tonumber(queue.blocked_until or 0) <= now
                    and tonumber(queue.next_allowed_at or 0) <= now
                    and tonumber(queue.active or 0) < tonumber(queue.max_concurrency or 1)
                    and under_limit

                if allowed then
                    local lease_number = redis.call('INCR', KEYS[6])
                    local token = redis.sha1hex(
                        entry.url .. ':' .. tostring(now) .. ':' .. tostring(lease_number)
                    )
                    entry.state = 'leased'
                    entry.attempt = tonumber(entry.attempt or 0) + 1
                    entry.lease_token = token
                    entry.lease_expires_at = now + lease_seconds
                    redis.call('HSET', KEYS[1], entry.url, cjson.encode(entry))
                    redis.call('ZREM', KEYS[2], entry.ready_member)
                    redis.call('ZREM', KEYS[3], entry.url)
                    redis.call('ZADD', KEYS[4], entry.lease_expires_at, entry.url)

                    queue.active = tonumber(queue.active or 0) + 1
                    queue.issued = tonumber(queue.issued or 0) + 1
                    if tonumber(queue.delay_seconds or 0) > 0 then
                        queue.next_allowed_at = now + tonumber(queue.delay_seconds)
                    end
                    redis.call('HSET', KEYS[5], entry.queue_key, cjson.encode(queue))
                    redis.call('HINCRBY', KEYS[7], 'queued', -1)
                    redis.call('HINCRBY', KEYS[7], 'leased', 1)

                    table.insert(leases, entry)
                end
            else
                redis.call('ZREM', KEYS[2], member)
            end
        else
            redis.call('ZREM', KEYS[2], member)
        end
    else
        redis.call('ZREM', KEYS[2], member)
    end
end
return cjson.encode(leases)
"""

_FINISH_SCRIPT = r"""
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then
    return redis.error_reply('request not found')
end
local entry = cjson.decode(raw)
if entry.state ~= 'leased' or entry.lease_token ~= ARGV[2] then
    return redis.error_reply('lease is no longer active')
end

local qraw = redis.call('HGET', KEYS[5], entry.queue_key)
if qraw then
    local queue = cjson.decode(qraw)
    queue.active = math.max(0, tonumber(queue.active or 0) - 1)
    redis.call('HSET', KEYS[5], entry.queue_key, cjson.encode(queue))
end

redis.call('ZREM', KEYS[4], entry.url)
redis.call('ZREM', KEYS[2], entry.ready_member)
redis.call('ZREM', KEYS[3], entry.url)
redis.call('HINCRBY', KEYS[6], 'leased', -1)
entry.state = ARGV[3]
entry.lease_token = cjson.null
entry.lease_expires_at = 0
if ARGV[5] == '' then
    entry.error = cjson.null
else
    entry.error = ARGV[5]
end

if ARGV[3] == 'queued' then
    entry.available_at = tonumber(ARGV[4])
    redis.call('ZADD', KEYS[3], entry.available_at, entry.url)
    redis.call('HINCRBY', KEYS[6], 'queued', 1)
else
    redis.call('HINCRBY', KEYS[6], ARGV[3], 1)
end
redis.call('HSET', KEYS[1], entry.url, cjson.encode(entry))
return 1
"""


class RedisFrontier:
    """Redis-backed frontier with atomic host-affine leasing semantics.

    Ready ordering and delayed availability use separate sorted sets. The sorted
    sets are authoritative for scheduling state, avoiding timestamp contradictions
    from JSON floating-point round trips. State transitions are atomic Lua scripts,
    and all keys for one crawl share a Redis Cluster hash tag.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        key_prefix: str = "syndcrawler",
        owns_client: bool = False,
        candidate_multiplier: int = 50,
        min_candidate_window: int = 100,
        maintenance_window: int = 5000,
    ) -> None:
        if not key_prefix:
            raise ValueError("key_prefix cannot be empty")
        if candidate_multiplier <= 0:
            raise ValueError("candidate_multiplier must be positive")
        if min_candidate_window <= 0:
            raise ValueError("min_candidate_window must be positive")
        if maintenance_window <= 0:
            raise ValueError("maintenance_window must be positive")
        self._redis = redis
        self.key_prefix = key_prefix.rstrip(":")
        self._owns_client = owns_client
        self.candidate_multiplier = candidate_multiplier
        self.min_candidate_window = min_candidate_window
        self.maintenance_window = maintenance_window

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_prefix: str = "syndcrawler",
        **redis_kwargs: Any,
    ) -> RedisFrontier:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise RuntimeError(
                "Redis frontier requires `pip install 'syndcrawler[redis]'`."
            ) from exc
        redis_kwargs.setdefault("decode_responses", True)
        client = Redis.from_url(url, **redis_kwargs)
        return cls(client, key_prefix=key_prefix, owns_client=True)

    async def close(self) -> None:
        if not self._owns_client:
            return
        aclose = getattr(self._redis, "aclose", None)
        if aclose is not None:
            await aclose()
            return
        close = getattr(self._redis, "close", None)
        if close is not None:
            result = close()
            if result is not None:
                await result

    async def add(self, *requests: FrontierRequest) -> int:
        if not requests:
            return 0
        by_crawl: dict[str, list[dict[str, Any]]] = {}
        for raw in requests:
            request = raw.normalized()
            assert request.queue_key is not None
            by_crawl.setdefault(request.crawl_id, []).append(
                {
                    "url": request.url,
                    "queue_key": request.queue_key,
                    "priority": request.priority,
                    "depth": request.depth,
                    "available_at": request.available_at,
                    "metadata": request.metadata,
                }
            )

        added = 0
        current = time.time()
        for crawl_id, payload in by_crawl.items():
            keys = self._keys(crawl_id)
            result = await self._redis.eval(
                _ADD_SCRIPT,
                6,
                keys.requests,
                keys.ready,
                keys.delayed,
                keys.queues,
                keys.sequence,
                keys.counts,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                current,
            )
            added += int(result)
        return added

    async def set_queue_policy(
        self,
        crawl_id: str,
        queue_key: str,
        policy: QueuePolicy,
    ) -> None:
        _validate_queue_policy(policy)
        keys = self._keys(crawl_id)
        await self._redis.eval(
            _SET_QUEUE_POLICY_SCRIPT,
            1,
            keys.queues,
            queue_key,
            policy.max_concurrency,
            policy.delay_seconds,
            policy.blocked_until,
            "" if policy.crawl_limit is None else policy.crawl_limit,
        )

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
        scan_limit = max(
            self.min_candidate_window,
            limit * self.candidate_multiplier,
        )
        keys = self._keys(crawl_id)
        result = await self._redis.eval(
            _LEASE_SCRIPT,
            7,
            keys.requests,
            keys.ready,
            keys.delayed,
            keys.leased,
            keys.queues,
            keys.lease_sequence,
            keys.counts,
            current,
            lease_seconds,
            limit,
            scan_limit,
            self.maintenance_window,
        )
        decoded = json.loads(_text(result))
        return [_lease_from_payload(crawl_id, item, current) for item in decoded]

    async def ack(self, lease: FrontierLease) -> None:
        await self._finish(lease, RequestState.DONE, available_at=None, error=None)

    async def retry(
        self,
        lease: FrontierLease,
        *,
        available_at: float,
        error: str | None = None,
    ) -> None:
        await self._finish(
            lease,
            RequestState.QUEUED,
            available_at=available_at,
            error=error,
        )

    async def fail(self, lease: FrontierLease, *, error: str) -> None:
        await self._finish(lease, RequestState.FAILED, available_at=None, error=error)

    async def stats(self, crawl_id: str) -> dict[str, int]:
        keys = self._keys(crawl_id)
        raw = await self._redis.hgetall(keys.counts)
        normalized = {_text(key): int(value) for key, value in raw.items()}
        return {state.value: normalized.get(state.value, 0) for state in RequestState}

    async def purge(self, crawl_id: str) -> None:
        """Delete all Redis frontier state for one crawl namespace."""

        keys = self._keys(crawl_id)
        await self._redis.delete(
            keys.requests,
            keys.ready,
            keys.delayed,
            keys.leased,
            keys.queues,
            keys.sequence,
            keys.lease_sequence,
            keys.counts,
        )

    async def _finish(
        self,
        lease: FrontierLease,
        state: RequestState,
        *,
        available_at: float | None,
        error: str | None,
    ) -> None:
        keys = self._keys(lease.request.crawl_id)
        await self._redis.eval(
            _FINISH_SCRIPT,
            6,
            keys.requests,
            keys.ready,
            keys.delayed,
            keys.leased,
            keys.queues,
            keys.counts,
            lease.request.url,
            lease.token,
            state.value,
            "" if available_at is None else available_at,
            error or "",
        )

    def _keys(self, crawl_id: str) -> _RedisKeys:
        tag = hashlib.sha256(crawl_id.encode("utf-8")).hexdigest()[:24]
        base = f"{self.key_prefix}:frontier:{{{tag}}}"
        return _RedisKeys(
            requests=f"{base}:requests",
            ready=f"{base}:ready",
            delayed=f"{base}:delayed",
            leased=f"{base}:leased",
            queues=f"{base}:queues",
            sequence=f"{base}:sequence",
            lease_sequence=f"{base}:lease-sequence",
            counts=f"{base}:counts",
        )


class _RedisKeys:
    __slots__ = (
        "counts",
        "delayed",
        "lease_sequence",
        "leased",
        "queues",
        "ready",
        "requests",
        "sequence",
    )

    def __init__(
        self,
        *,
        requests: str,
        ready: str,
        delayed: str,
        leased: str,
        queues: str,
        sequence: str,
        lease_sequence: str,
        counts: str,
    ) -> None:
        self.requests = requests
        self.ready = ready
        self.delayed = delayed
        self.leased = leased
        self.queues = queues
        self.sequence = sequence
        self.lease_sequence = lease_sequence
        self.counts = counts


def _lease_from_payload(
    crawl_id: str,
    payload: dict[str, Any],
    leased_at: float,
) -> FrontierLease:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    request = FrontierRequest(
        url=str(payload["url"]),
        crawl_id=crawl_id,
        queue_key=str(payload["queue_key"]),
        priority=int(payload.get("priority", 0)),
        depth=int(payload.get("depth", 0)),
        available_at=float(payload.get("available_at", 0.0)),
        metadata=metadata,
    )
    return FrontierLease(
        request=request,
        token=str(payload["lease_token"]),
        attempt=int(payload["attempt"]),
        leased_at=leased_at,
        expires_at=float(payload["lease_expires_at"]),
    )


def _validate_queue_policy(policy: QueuePolicy) -> None:
    if policy.max_concurrency <= 0:
        raise ValueError("max_concurrency must be positive")
    if policy.delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")
    if policy.crawl_limit is not None and policy.crawl_limit <= 0:
        raise ValueError("crawl_limit must be positive")


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
