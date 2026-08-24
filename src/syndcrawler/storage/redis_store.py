from __future__ import annotations

from syndcrawler.storage.redis_frontier import RedisFrontier as _BaseRedisFrontier


class RedisFrontier(_BaseRedisFrontier):
    """Redis frontier with bounded namespace retention for lifecycle cleanup."""

    async def expire(self, crawl_id: str, ttl_seconds: int) -> None:
        """Apply the same TTL to every key belonging to one crawl namespace.

        This is used when a crawl is cancelled while workers still hold active
        leases. The namespace remains available long enough for those workers to
        ACK safely, while the TTL guarantees eventual cleanup if a worker dies.
        """

        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        keys = self._keys(crawl_id)
        values = (
            keys.requests,
            keys.ready,
            keys.delayed,
            keys.leased,
            keys.queues,
            keys.sequence,
            keys.lease_sequence,
            keys.counts,
            keys.crawl,
        )
        async with self._redis.pipeline(transaction=True) as pipeline:
            for key in values:
                pipeline.expire(key, ttl_seconds)
            await pipeline.execute()
