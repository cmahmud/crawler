from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from syndcrawler.core.change import ChangeKind, ContentFingerprint, HttpValidators
from syndcrawler.storage.postgres_crawl_store import PostgresCrawlStore
from syndcrawler.storage.sqlite_crawl_store import ResourceState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS syndcrawler_recrawl_claims (
    crawl_id TEXT NOT NULL,
    request_url TEXT NOT NULL,
    token TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(crawl_id, request_url),
    FOREIGN KEY(crawl_id, request_url)
        REFERENCES syndcrawler_resource_state(crawl_id, request_url)
        ON DELETE CASCADE
)
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_syndcrawler_recrawl_claims_expiry
ON syndcrawler_recrawl_claims(expires_at, crawl_id, request_url)
"""


@dataclass(frozen=True, slots=True)
class RecrawlClaim:
    state: ResourceState
    token: str
    expires_at: float


class PostgresRecrawlClaims:
    """Lease due recrawls safely across independent server workers.

    Resource history remains in `syndcrawler_resource_state`; this table only
    carries ephemeral claim ownership. Expired claims are eligible immediately,
    so worker crashes do not strand scheduled recrawls.
    """

    def __init__(self, store: PostgresCrawlStore) -> None:
        self.store = store
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            async with self.store._pool.connection() as connection:
                async with connection.transaction():
                    await connection.execute(_SCHEMA)
                    await connection.execute(_INDEX)
            self._initialized = True

    async def claim_due(
        self,
        *,
        limit: int = 100,
        lease_seconds: float = 120.0,
        now: float | None = None,
    ) -> list[RecrawlClaim]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        await self.initialize()
        current = time.time() if now is None else now
        expires_at = current + lease_seconds

        async with self.store._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT rs.*
                    FROM syndcrawler_resource_state AS rs
                    JOIN syndcrawler_manifests AS manifest
                      ON manifest.crawl_id = rs.crawl_id
                    LEFT JOIN syndcrawler_recrawl_claims AS claim
                      ON claim.crawl_id = rs.crawl_id
                     AND claim.request_url = rs.request_url
                    WHERE manifest.lifecycle = 'completed'
                      AND rs.next_fetch_at IS NOT NULL
                      AND rs.next_fetch_at <= %s
                      AND (claim.token IS NULL OR claim.expires_at <= %s)
                    ORDER BY rs.next_fetch_at ASC, rs.crawl_id ASC, rs.request_url ASC
                    FOR UPDATE OF rs SKIP LOCKED
                    LIMIT %s
                    """,
                    (current, current, limit),
                )
                rows = await cursor.fetchall()
                claims: list[RecrawlClaim] = []
                for row in rows:
                    token = uuid.uuid4().hex
                    await connection.execute(
                        """
                        INSERT INTO syndcrawler_recrawl_claims (
                            crawl_id, request_url, token, expires_at
                        ) VALUES (%s, %s, %s, %s)
                        ON CONFLICT(crawl_id, request_url) DO UPDATE SET
                            token = EXCLUDED.token,
                            expires_at = EXCLUDED.expires_at
                        """,
                        (row["crawl_id"], row["request_url"], token, expires_at),
                    )
                    claims.append(
                        RecrawlClaim(
                            state=_resource_state_from_row(row),
                            token=token,
                            expires_at=expires_at,
                        )
                    )
        return claims

    async def reschedule(
        self,
        claim: RecrawlClaim,
        *,
        interval_seconds: float,
        next_fetch_at: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        await self.initialize()
        state = claim.state
        async with self.store._pool.connection() as connection:
            async with connection.transaction():
                deleted = await connection.execute(
                    """
                    DELETE FROM syndcrawler_recrawl_claims
                    WHERE crawl_id = %s AND request_url = %s AND token = %s
                    RETURNING 1
                    """,
                    (state.crawl_id, state.request_url, claim.token),
                )
                if await deleted.fetchone() is None:
                    raise RuntimeError(
                        f"recrawl claim is no longer active for {state.request_url}"
                    )
                updated = await connection.execute(
                    """
                    UPDATE syndcrawler_resource_state
                    SET recrawl_interval_seconds = %s, next_fetch_at = %s
                    WHERE crawl_id = %s AND request_url = %s
                    RETURNING 1
                    """,
                    (
                        interval_seconds,
                        next_fetch_at,
                        state.crawl_id,
                        state.request_url,
                    ),
                )
                if await updated.fetchone() is None:
                    raise ValueError(f"resource state does not exist: {state.request_url}")

    async def release(self, claim: RecrawlClaim) -> bool:
        await self.initialize()
        state = claim.state
        async with self.store._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    DELETE FROM syndcrawler_recrawl_claims
                    WHERE crawl_id = %s AND request_url = %s AND token = %s
                    RETURNING 1
                    """,
                    (state.crawl_id, state.request_url, claim.token),
                )
                return await cursor.fetchone() is not None


def _resource_state_from_row(row: dict[str, Any]) -> ResourceState:
    return ResourceState(
        crawl_id=str(row["crawl_id"]),
        request_url=str(row["request_url"]),
        fingerprint=ContentFingerprint(
            content_sha256=str(row["content_sha256"]),
            semantic_sha256=str(row["semantic_sha256"]),
            validators=HttpValidators(
                etag=str(row["etag"]) if row["etag"] is not None else None,
                last_modified=(
                    str(row["last_modified"])
                    if row["last_modified"] is not None
                    else None
                ),
            ),
        ),
        first_seen_at=float(row["first_seen_at"]),
        last_seen_at=float(row["last_seen_at"]),
        last_changed_at=float(row["last_changed_at"]),
        change_count=int(row["change_count"]),
        last_change_kind=ChangeKind(str(row["last_change_kind"])),
        recrawl_interval_seconds=(
            float(row["recrawl_interval_seconds"])
            if row["recrawl_interval_seconds"] is not None
            else None
        ),
        next_fetch_at=(
            float(row["next_fetch_at"]) if row["next_fetch_at"] is not None else None
        ),
    )
