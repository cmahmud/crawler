from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from syndcrawler.models import PageRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_manifests (
    crawl_id TEXT PRIMARY KEY,
    seeds_json TEXT NOT NULL,
    follow_links INTEGER NOT NULL,
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
"""


@dataclass(frozen=True, slots=True)
class CrawlManifest:
    crawl_id: str
    seeds: tuple[str, ...]
    follow_links: bool
    max_pages: int
    created_at: float
    updated_at: float


class SQLiteCrawlStore:
    """Durable crawl metadata and idempotent result storage."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._lock = asyncio.Lock()
        self._closed = False

    async def create_manifest(
        self,
        crawl_id: str,
        seeds: tuple[str, ...],
        *,
        follow_links: bool,
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
                        crawl_id, seeds_json, follow_links, max_pages,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        crawl_id,
                        json.dumps(seeds, ensure_ascii=False),
                        int(follow_links),
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
    ) -> None:
        current = time.time() if now is None else now
        payload = json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        async with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
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
            except Exception:
                self._connection.rollback()
                raise

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
        max_pages=int(row["max_pages"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
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
