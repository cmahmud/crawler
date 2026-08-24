from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from syndcrawler.core.change import (
    ChangeAssessment,
    ChangeKind,
    ContentFingerprint,
    HttpValidators,
    assess_change,
)
from syndcrawler.models import PageRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_manifests (
    crawl_id TEXT PRIMARY KEY,
    seeds_json TEXT NOT NULL,
    follow_links INTEGER NOT NULL,
    same_domain INTEGER NOT NULL DEFAULT 1,
    max_pages INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_results (
    crawl_id TEXT NOT NULL,
    request_url TEXT NOT NULL,
    record_json TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY(crawl_id, request_url)
);

CREATE INDEX IF NOT EXISTS idx_crawl_results_crawl
ON crawl_results(crawl_id, recorded_at, request_url);

CREATE TABLE IF NOT EXISTS crawl_resource_state (
    crawl_id TEXT NOT NULL,
    request_url TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    content_sha256 TEXT NOT NULL,
    semantic_sha256 TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    last_changed_at REAL NOT NULL,
    change_count INTEGER NOT NULL DEFAULT 0,
    last_change_kind TEXT NOT NULL,
    recrawl_interval_seconds REAL,
    next_fetch_at REAL,
    PRIMARY KEY(crawl_id, request_url)
);

CREATE INDEX IF NOT EXISTS idx_crawl_resource_state_seen
ON crawl_resource_state(crawl_id, last_seen_at, request_url);

CREATE INDEX IF NOT EXISTS idx_crawl_resource_state_due
ON crawl_resource_state(crawl_id, next_fetch_at, request_url);
"""


@dataclass(frozen=True, slots=True)
class CrawlManifest:
    crawl_id: str
    seeds: tuple[str, ...]
    follow_links: bool
    same_domain: bool
    max_pages: int
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class ResourceState:
    crawl_id: str
    request_url: str
    fingerprint: ContentFingerprint
    first_seen_at: float
    last_seen_at: float
    last_changed_at: float
    change_count: int
    last_change_kind: ChangeKind
    recrawl_interval_seconds: float | None
    next_fetch_at: float | None


class SQLiteCrawlStore:
    """Durable crawl metadata, latest results, and resource change state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(_SCHEMA)
        self._migrate_manifest_schema()
        self._migrate_resource_schema()
        self._connection.commit()
        self._lock = asyncio.Lock()
        self._closed = False

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
        async with self._lock:
            self._ensure_open()
            try:
                self._connection.execute(
                    """
                    INSERT INTO crawl_manifests (
                        crawl_id, seeds_json, follow_links, same_domain, max_pages,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        crawl_id,
                        json.dumps(seeds, ensure_ascii=False),
                        int(follow_links),
                        int(same_domain),
                        max_pages,
                        current,
                        current,
                    ),
                )
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
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
        async with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM crawl_manifests WHERE crawl_id = ?",
                (crawl_id,),
            ).fetchone()
            return _manifest_from_row(row) if row is not None else None

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

        async with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                assessment = None
                if fingerprint is not None:
                    previous_row = self._connection.execute(
                        """
                        SELECT * FROM crawl_resource_state
                        WHERE crawl_id = ? AND request_url = ?
                        """,
                        (crawl_id, request_url),
                    ).fetchone()
                    previous = (
                        _resource_state_from_row(previous_row)
                        if previous_row is not None
                        else None
                    )
                    assessment = assess_change(
                        previous.fingerprint if previous is not None else None,
                        fingerprint,
                    )
                    self._upsert_resource_state(
                        crawl_id,
                        request_url,
                        fingerprint,
                        assessment,
                        previous,
                        current,
                    )

                self._connection.execute(
                    """
                    INSERT INTO crawl_results (
                        crawl_id, request_url, record_json, recorded_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(crawl_id, request_url) DO UPDATE SET
                        record_json = excluded.record_json,
                        recorded_at = excluded.recorded_at
                    """,
                    (crawl_id, request_url, payload, current),
                )
                self._connection.execute(
                    """
                    UPDATE crawl_manifests SET updated_at = ?
                    WHERE crawl_id = ?
                    """,
                    (current, crawl_id),
                )
                self._connection.commit()
                return assessment
            except Exception:
                self._connection.rollback()
                raise

    async def mark_not_modified(
        self,
        crawl_id: str,
        request_url: str,
        *,
        now: float | None = None,
    ) -> ResourceState:
        current = time.time() if now is None else now
        async with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT * FROM crawl_resource_state
                    WHERE crawl_id = ? AND request_url = ?
                    """,
                    (crawl_id, request_url),
                ).fetchone()
                if row is None:
                    raise ValueError(f"resource state does not exist: {request_url}")
                self._connection.execute(
                    """
                    UPDATE crawl_resource_state
                    SET last_seen_at = ?, last_change_kind = ?
                    WHERE crawl_id = ? AND request_url = ?
                    """,
                    (
                        current,
                        ChangeKind.NOT_MODIFIED.value,
                        crawl_id,
                        request_url,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE crawl_manifests SET updated_at = ?
                    WHERE crawl_id = ?
                    """,
                    (current, crawl_id),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        state = await self.get_resource_state(crawl_id, request_url)
        assert state is not None
        return state

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
        async with self._lock:
            self._ensure_open()
            cursor = self._connection.execute(
                """
                UPDATE crawl_resource_state
                SET recrawl_interval_seconds = ?, next_fetch_at = ?
                WHERE crawl_id = ? AND request_url = ?
                """,
                (interval_seconds, next_fetch_at, crawl_id, request_url),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"resource state does not exist: {request_url}")
            self._connection.commit()
        state = await self.get_resource_state(crawl_id, request_url)
        assert state is not None
        return state

    async def get_resource_state(
        self,
        crawl_id: str,
        request_url: str,
    ) -> ResourceState | None:
        async with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT * FROM crawl_resource_state
                WHERE crawl_id = ? AND request_url = ?
                """,
                (crawl_id, request_url),
            ).fetchone()
            return _resource_state_from_row(row) if row is not None else None

    async def resource_states(self, crawl_id: str) -> list[ResourceState]:
        async with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT * FROM crawl_resource_state
                WHERE crawl_id = ?
                ORDER BY last_seen_at ASC, request_url ASC
                """,
                (crawl_id,),
            ).fetchall()
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
        async with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT * FROM crawl_resource_state
                WHERE crawl_id = ?
                  AND next_fetch_at IS NOT NULL
                  AND next_fetch_at <= ?
                ORDER BY next_fetch_at ASC, request_url ASC
                LIMIT ?
                """,
                (crawl_id, current, limit),
            ).fetchall()
        return [_resource_state_from_row(row) for row in rows]

    async def results(self, crawl_id: str) -> list[PageRecord]:
        async with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT record_json FROM crawl_results
                WHERE crawl_id = ?
                ORDER BY recorded_at ASC, request_url ASC
                """,
                (crawl_id,),
            ).fetchall()
        return [_record_from_json(str(row["record_json"])) for row in rows]

    async def result_count(self, crawl_id: str) -> int:
        async with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM crawl_results WHERE crawl_id = ?",
                (crawl_id,),
            ).fetchone()
            assert row is not None
            return int(row["count"])

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def _upsert_resource_state(
        self,
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
        self._connection.execute(
            """
            INSERT INTO crawl_resource_state (
                crawl_id, request_url, etag, last_modified, content_sha256,
                semantic_sha256, first_seen_at, last_seen_at, last_changed_at,
                change_count, last_change_kind, recrawl_interval_seconds,
                next_fetch_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(crawl_id, request_url) DO UPDATE SET
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                content_sha256 = excluded.content_sha256,
                semantic_sha256 = excluded.semantic_sha256,
                last_seen_at = excluded.last_seen_at,
                last_changed_at = excluded.last_changed_at,
                change_count = excluded.change_count,
                last_change_kind = excluded.last_change_kind,
                recrawl_interval_seconds = excluded.recrawl_interval_seconds,
                next_fetch_at = excluded.next_fetch_at
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

    def _migrate_manifest_schema(self) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(crawl_manifests)")
        }
        if "same_domain" not in columns:
            self._connection.execute(
                "ALTER TABLE crawl_manifests ADD COLUMN same_domain INTEGER NOT NULL DEFAULT 1"
            )

    def _migrate_resource_schema(self) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(crawl_resource_state)")
        }
        if "recrawl_interval_seconds" not in columns:
            self._connection.execute(
                "ALTER TABLE crawl_resource_state ADD COLUMN recrawl_interval_seconds REAL"
            )
        if "next_fetch_at" not in columns:
            self._connection.execute(
                "ALTER TABLE crawl_resource_state ADD COLUMN next_fetch_at REAL"
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteCrawlStore is closed")


def _manifest_from_row(row: sqlite3.Row) -> CrawlManifest:
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


def _resource_state_from_row(row: sqlite3.Row) -> ResourceState:
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
            float(row["next_fetch_at"])
            if row["next_fetch_at"] is not None
            else None
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
