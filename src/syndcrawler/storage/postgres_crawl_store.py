from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from syndcrawler.core.change import (
    ChangeAssessment,
    ChangeKind,
    ContentFingerprint,
    HttpValidators,
    assess_change,
)
from syndcrawler.models import PageRecord
from syndcrawler.storage.sqlite_crawl_store import CrawlManifest, ResourceState

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

_LIFECYCLES = frozenset({"active", "paused", "cancelled", "completed"})

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS syndcrawler_manifests (
        crawl_id TEXT PRIMARY KEY,
        seeds_json TEXT NOT NULL,
        follow_links BOOLEAN NOT NULL,
        same_domain BOOLEAN NOT NULL,
        max_pages BIGINT NOT NULL CHECK (max_pages > 0),
        lifecycle TEXT NOT NULL DEFAULT 'active',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    ALTER TABLE syndcrawler_manifests
    ADD COLUMN IF NOT EXISTS lifecycle TEXT NOT NULL DEFAULT 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_syndcrawler_manifests_lifecycle
    ON syndcrawler_manifests(lifecycle, updated_at, crawl_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS syndcrawler_results (
        crawl_id TEXT NOT NULL REFERENCES syndcrawler_manifests(crawl_id) ON DELETE CASCADE,
        request_url TEXT NOT NULL,
        record_json TEXT NOT NULL,
        recorded_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY(crawl_id, request_url)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_syndcrawler_results_crawl
    ON syndcrawler_results(crawl_id, recorded_at, request_url)
    """,
    """
    CREATE TABLE IF NOT EXISTS syndcrawler_resource_state (
        crawl_id TEXT NOT NULL REFERENCES syndcrawler_manifests(crawl_id) ON DELETE CASCADE,
        request_url TEXT NOT NULL,
        etag TEXT,
        last_modified TEXT,
        content_sha256 TEXT NOT NULL,
        semantic_sha256 TEXT NOT NULL,
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        last_changed_at DOUBLE PRECISION NOT NULL,
        change_count BIGINT NOT NULL DEFAULT 0,
        last_change_kind TEXT NOT NULL,
        recrawl_interval_seconds DOUBLE PRECISION,
        next_fetch_at DOUBLE PRECISION,
        PRIMARY KEY(crawl_id, request_url)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_syndcrawler_resource_state_seen
    ON syndcrawler_resource_state(crawl_id, last_seen_at, request_url)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_syndcrawler_resource_state_due
    ON syndcrawler_resource_state(crawl_id, next_fetch_at, request_url)
    """,
)


class PostgresCrawlStore:
    """Shared crawl metadata/result store for VPS and multi-worker deployments."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owns_pool: bool = False,
    ) -> None:
        self._pool = pool
        self._owns_pool = owns_pool

    @classmethod
    async def from_dsn(
        cls,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
    ) -> PostgresCrawlStore:
        if min_size < 0:
            raise ValueError("min_size cannot be negative")
        if max_size <= 0 or max_size < min_size:
            raise ValueError("max_size must be positive and >= min_size")
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL storage requires `pip install 'syndcrawler[postgres]'`."
            ) from exc

        pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            timeout=timeout,
            kwargs={"row_factory": dict_row},
        )
        await pool.open()
        store = cls(pool, owns_pool=True)
        try:
            await store.initialize()
        except Exception:
            await pool.close()
            raise
        return store

    async def initialize(self) -> None:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                for statement in _SCHEMA_STATEMENTS:
                    await connection.execute(statement)

    async def ping(self) -> bool:
        async with self._pool.connection() as connection:
            cursor = await connection.execute("SELECT 1 AS ok")
            row = await cursor.fetchone()
        return row is not None and int(row["ok"]) == 1

    async def create_manifest(
        self,
        crawl_id: str,
        seeds: tuple[str, ...],
        *,
        follow_links: bool,
        same_domain: bool,
        max_pages: int,
        now: float | None = None,
    ) -> CrawlManifest:
        if not crawl_id:
            raise ValueError("crawl_id cannot be empty")
        if not seeds:
            raise ValueError("at least one seed is required")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        current = time.time() if now is None else now

        try:
            from psycopg.errors import UniqueViolation
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL storage requires `pip install 'syndcrawler[postgres]'`."
            ) from exc

        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO syndcrawler_manifests (
                            crawl_id, seeds_json, follow_links, same_domain,
                            max_pages, lifecycle, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)
                        """,
                        (
                            crawl_id,
                            json.dumps(seeds, ensure_ascii=False),
                            follow_links,
                            same_domain,
                            max_pages,
                            current,
                            current,
                        ),
                    )
        except UniqueViolation as exc:
            raise ValueError(f"crawl already exists: {crawl_id}") from exc

        return CrawlManifest(
            crawl_id=crawl_id,
            seeds=seeds,
            follow_links=follow_links,
            same_domain=same_domain,
            max_pages=max_pages,
            created_at=current,
            updated_at=current,
        )

    async def get_manifest(self, crawl_id: str) -> CrawlManifest | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM syndcrawler_manifests WHERE crawl_id = %s",
                (crawl_id,),
            )
            row = await cursor.fetchone()
        return _manifest_from_row(row) if row is not None else None

    async def get_lifecycle(self, crawl_id: str) -> str | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT lifecycle FROM syndcrawler_manifests WHERE crawl_id = %s",
                (crawl_id,),
            )
            row = await cursor.fetchone()
        return str(row["lifecycle"]) if row is not None else None

    async def set_lifecycle(
        self,
        crawl_id: str,
        lifecycle: str,
        *,
        now: float | None = None,
    ) -> str:
        if lifecycle not in _LIFECYCLES:
            raise ValueError(f"unsupported crawl lifecycle: {lifecycle}")
        current = time.time() if now is None else now
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    UPDATE syndcrawler_manifests
                    SET lifecycle = %s, updated_at = %s
                    WHERE crawl_id = %s
                    RETURNING lifecycle
                    """,
                    (lifecycle, current, crawl_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError(f"crawl manifest does not exist: {crawl_id}")
        return str(row["lifecycle"])

    async def runnable_crawl_ids(self, *, limit: int = 100) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT crawl_id FROM syndcrawler_manifests
                WHERE lifecycle = 'active'
                ORDER BY updated_at ASC, crawl_id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [str(row["crawl_id"]) for row in rows]

    async def recent_crawl_ids(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[str]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT crawl_id FROM syndcrawler_manifests
                ORDER BY updated_at DESC, crawl_id DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = await cursor.fetchall()
        return [str(row["crawl_id"]) for row in rows]

    async def manifest_count(self) -> int:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM syndcrawler_manifests"
            )
            row = await cursor.fetchone()
        assert row is not None
        return int(row["count"])

    async def put_result(
        self,
        crawl_id: str,
        request_url: str,
        record: PageRecord,
        *,
        now: float | None = None,
    ) -> ChangeAssessment | None:
        current = time.time() if now is None else now
        payload = json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprint = _fingerprint_from_record(record)

        async with self._pool.connection() as connection:
            async with connection.transaction():
                assessment = None
                if fingerprint is not None:
                    cursor = await connection.execute(
                        """
                        SELECT * FROM syndcrawler_resource_state
                        WHERE crawl_id = %s AND request_url = %s
                        FOR UPDATE
                        """,
                        (crawl_id, request_url),
                    )
                    previous_row = await cursor.fetchone()
                    previous = (
                        _resource_state_from_row(previous_row)
                        if previous_row is not None
                        else None
                    )
                    assessment = assess_change(
                        previous.fingerprint if previous is not None else None,
                        fingerprint,
                    )
                    await self._upsert_resource_state(
                        connection,
                        crawl_id,
                        request_url,
                        fingerprint,
                        assessment,
                        previous,
                        current,
                    )

                await connection.execute(
                    """
                    INSERT INTO syndcrawler_results (
                        crawl_id, request_url, record_json, recorded_at
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT(crawl_id, request_url) DO UPDATE SET
                        record_json = EXCLUDED.record_json,
                        recorded_at = EXCLUDED.recorded_at
                    """,
                    (crawl_id, request_url, payload, current),
                )
                await connection.execute(
                    """
                    UPDATE syndcrawler_manifests SET updated_at = %s
                    WHERE crawl_id = %s
                    """,
                    (current, crawl_id),
                )
                return assessment

    async def mark_not_modified(
        self,
        crawl_id: str,
        request_url: str,
        *,
        now: float | None = None,
    ) -> ResourceState:
        current = time.time() if now is None else now
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    UPDATE syndcrawler_resource_state
                    SET last_seen_at = %s, last_change_kind = %s
                    WHERE crawl_id = %s AND request_url = %s
                    RETURNING *
                    """,
                    (
                        current,
                        ChangeKind.NOT_MODIFIED.value,
                        crawl_id,
                        request_url,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError(f"resource state does not exist: {request_url}")
                await connection.execute(
                    """
                    UPDATE syndcrawler_manifests SET updated_at = %s
                    WHERE crawl_id = %s
                    """,
                    (current, crawl_id),
                )
        return _resource_state_from_row(row)

    async def schedule_resource(
        self,
        crawl_id: str,
        request_url: str,
        *,
        interval_seconds: float,
        next_fetch_at: float,
    ) -> ResourceState:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    UPDATE syndcrawler_resource_state
                    SET recrawl_interval_seconds = %s, next_fetch_at = %s
                    WHERE crawl_id = %s AND request_url = %s
                    RETURNING *
                    """,
                    (interval_seconds, next_fetch_at, crawl_id, request_url),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError(f"resource state does not exist: {request_url}")
        return _resource_state_from_row(row)

    async def get_resource_state(
        self,
        crawl_id: str,
        request_url: str,
    ) -> ResourceState | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM syndcrawler_resource_state
                WHERE crawl_id = %s AND request_url = %s
                """,
                (crawl_id, request_url),
            )
            row = await cursor.fetchone()
        return _resource_state_from_row(row) if row is not None else None

    async def resource_states(self, crawl_id: str) -> list[ResourceState]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM syndcrawler_resource_state
                WHERE crawl_id = %s
                ORDER BY last_seen_at ASC, request_url ASC
                """,
                (crawl_id,),
            )
            rows = await cursor.fetchall()
        return [_resource_state_from_row(row) for row in rows]

    async def due_resources(
        self,
        crawl_id: str,
        *,
        now: float | None = None,
        limit: int = 100,
    ) -> list[ResourceState]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        current = time.time() if now is None else now
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM syndcrawler_resource_state
                WHERE crawl_id = %s
                  AND next_fetch_at IS NOT NULL
                  AND next_fetch_at <= %s
                ORDER BY next_fetch_at ASC, request_url ASC
                LIMIT %s
                """,
                (crawl_id, current, limit),
            )
            rows = await cursor.fetchall()
        return [_resource_state_from_row(row) for row in rows]

    async def results(self, crawl_id: str) -> list[PageRecord]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT record_json FROM syndcrawler_results
                WHERE crawl_id = %s
                ORDER BY recorded_at ASC, request_url ASC
                """,
                (crawl_id,),
            )
            rows = await cursor.fetchall()
        return [_record_from_json(str(row["record_json"])) for row in rows]

    async def results_page(
        self,
        crawl_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PageRecord]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT record_json FROM syndcrawler_results
                WHERE crawl_id = %s
                ORDER BY recorded_at ASC, request_url ASC
                LIMIT %s OFFSET %s
                """,
                (crawl_id, limit, offset),
            )
            rows = await cursor.fetchall()
        return [_record_from_json(str(row["record_json"])) for row in rows]

    async def result_count(self, crawl_id: str) -> int:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS count FROM syndcrawler_results
                WHERE crawl_id = %s
                """,
                (crawl_id,),
            )
            row = await cursor.fetchone()
        assert row is not None
        return int(row["count"])

    async def close(self) -> None:
        if self._owns_pool:
            await self._pool.close()

    async def _upsert_resource_state(
        self,
        connection: AsyncConnection,
        crawl_id: str,
        request_url: str,
        fingerprint: ContentFingerprint,
        assessment: ChangeAssessment,
        previous: ResourceState | None,
        current: float,
    ) -> None:
        first_seen = previous.first_seen_at if previous is not None else current
        changed = assessment.changed
        last_changed = current if previous is None or changed else previous.last_changed_at
        change_count = (previous.change_count if previous is not None else 0) + int(changed)
        interval = previous.recrawl_interval_seconds if previous is not None else None
        next_fetch = previous.next_fetch_at if previous is not None else None
        await connection.execute(
            """
            INSERT INTO syndcrawler_resource_state (
                crawl_id, request_url, etag, last_modified, content_sha256,
                semantic_sha256, first_seen_at, last_seen_at, last_changed_at,
                change_count, last_change_kind, recrawl_interval_seconds,
                next_fetch_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(crawl_id, request_url) DO UPDATE SET
                etag = EXCLUDED.etag,
                last_modified = EXCLUDED.last_modified,
                content_sha256 = EXCLUDED.content_sha256,
                semantic_sha256 = EXCLUDED.semantic_sha256,
                last_seen_at = EXCLUDED.last_seen_at,
                last_changed_at = EXCLUDED.last_changed_at,
                change_count = EXCLUDED.change_count,
                last_change_kind = EXCLUDED.last_change_kind,
                recrawl_interval_seconds = EXCLUDED.recrawl_interval_seconds,
                next_fetch_at = EXCLUDED.next_fetch_at
            """,
            (
                crawl_id,
                request_url,
                fingerprint.validators.etag,
                fingerprint.validators.last_modified,
                fingerprint.content_sha256,
                fingerprint.semantic_sha256,
                first_seen,
                current,
                last_changed,
                change_count,
                assessment.kind.value,
                interval,
                next_fetch,
            ),
        )


def _manifest_from_row(row: dict[str, Any]) -> CrawlManifest:
    seeds = json.loads(str(row["seeds_json"]))
    if not isinstance(seeds, list) or not all(isinstance(seed, str) for seed in seeds):
        raise ValueError("stored crawl seeds are invalid")
    return CrawlManifest(
        crawl_id=str(row["crawl_id"]),
        seeds=tuple(seeds),
        follow_links=bool(row["follow_links"]),
        same_domain=bool(row["same_domain"]),
        max_pages=int(row["max_pages"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


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


def _fingerprint_from_record(record: PageRecord) -> ContentFingerprint | None:
    value = record.metadata.get("fingerprint")
    if not isinstance(value, dict):
        return None
    content_sha256 = value.get("content_sha256")
    semantic_sha256 = value.get("semantic_sha256")
    if not isinstance(content_sha256, str) or not isinstance(semantic_sha256, str):
        return None
    validators = value.get("validators")
    if not isinstance(validators, dict):
        validators = {}
    etag = validators.get("etag")
    last_modified = validators.get("last_modified")
    return ContentFingerprint(
        content_sha256=content_sha256,
        semantic_sha256=semantic_sha256,
        validators=HttpValidators(
            etag=etag if isinstance(etag, str) else None,
            last_modified=last_modified if isinstance(last_modified, str) else None,
        ),
    )


def _record_from_json(value: str) -> PageRecord:
    data = json.loads(value)
    return PageRecord(
        url=str(data["url"]),
        status_code=data.get("status_code"),
        content_type=data.get("content_type"),
        title=data.get("title"),
        links=tuple(data.get("links", [])),
        engine=str(data.get("engine", "http")),
        route=str(data.get("route", "direct")),
        metadata=dict(data.get("metadata", {})),
    )
