from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from syndcrawler.core.frontier import (
    FrontierLease,
    FrontierRequest,
    QueuePolicy,
    RequestState,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier_requests (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_id TEXT NOT NULL,
    url TEXT NOT NULL,
    queue_key TEXT NOT NULL,
    priority INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    available_at REAL NOT NULL,
    metadata_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at REAL NOT NULL DEFAULT 0,
    error TEXT,
    UNIQUE(crawl_id, url)
);

CREATE INDEX IF NOT EXISTS idx_frontier_ready
ON frontier_requests(crawl_id, state, available_at, priority DESC, sequence);

CREATE INDEX IF NOT EXISTS idx_frontier_lease_expiry
ON frontier_requests(crawl_id, state, lease_expires_at);

CREATE TABLE IF NOT EXISTS frontier_queues (
    crawl_id TEXT NOT NULL,
    queue_key TEXT NOT NULL,
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    delay_seconds REAL NOT NULL DEFAULT 0,
    blocked_until REAL NOT NULL DEFAULT 0,
    crawl_limit INTEGER,
    active INTEGER NOT NULL DEFAULT 0,
    issued INTEGER NOT NULL DEFAULT 0,
    next_allowed_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(crawl_id, queue_key)
);

CREATE TABLE IF NOT EXISTS frontier_crawls (
    crawl_id TEXT PRIMARY KEY,
    crawl_limit INTEGER,
    issued INTEGER NOT NULL DEFAULT 0
);
"""


class SQLiteFrontier:
    """Durable local frontier implementing the same contract as MemoryFrontier.

    Every request/queue/crawl-budget transition is committed atomically. A crawl's
    global budget counts unique URLs when they receive their first lease; retries
    and reclaimed stale leases do not consume additional budget.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._lock = asyncio.Lock()
        self._closed = False

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    async def add(self, *requests: FrontierRequest) -> int:
        async with self._lock:
            self._ensure_open()
            added = 0
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for raw in requests:
                    request = raw.normalized()
                    assert request.queue_key is not None
                    self._ensure_crawl(request.crawl_id)
                    cursor = self._connection.execute(
                        """
                        INSERT OR IGNORE INTO frontier_requests (
                            crawl_id, url, queue_key, priority, depth,
                            available_at, metadata_json, state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request.crawl_id,
                            request.url,
                            request.queue_key,
                            request.priority,
                            request.depth,
                            request.available_at,
                            _encode_metadata(request.metadata),
                            RequestState.QUEUED.value,
                        ),
                    )
                    added += cursor.rowcount
                    self._ensure_queue(request.crawl_id, request.queue_key)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            return added

    async def set_crawl_limit(self, crawl_id: str, limit: int | None) -> None:
        if limit is not None and limit <= 0:
            raise ValueError("crawl limit must be positive")
        async with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_crawl(crawl_id)
                self._connection.execute(
                    """
                    UPDATE frontier_crawls SET crawl_limit = ?
                    WHERE crawl_id = ?
                    """,
                    (limit, crawl_id),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    async def set_queue_policy(
        self,
        crawl_id: str,
        queue_key: str,
        policy: QueuePolicy,
    ) -> None:
        _validate_queue_policy(policy)
        async with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_crawl(crawl_id)
                self._ensure_queue(crawl_id, queue_key)
                self._connection.execute(
                    """
                    UPDATE frontier_queues
                    SET max_concurrency = ?, delay_seconds = ?, blocked_until = ?,
                        crawl_limit = ?
                    WHERE crawl_id = ? AND queue_key = ?
                    """,
                    (
                        policy.max_concurrency,
                        policy.delay_seconds,
                        policy.blocked_until,
                        policy.crawl_limit,
                        crawl_id,
                        queue_key,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

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
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_crawl(crawl_id)
                self._reclaim_expired(crawl_id, current)
                rows = self._connection.execute(
                    """
                    SELECT * FROM frontier_requests
                    WHERE crawl_id = ? AND state = ? AND available_at <= ?
                    ORDER BY priority DESC, available_at ASC, sequence ASC
                    """,
                    (crawl_id, RequestState.QUEUED.value, current),
                ).fetchall()

                crawl_runtime = self._crawl_row(crawl_id)
                global_limit = crawl_runtime["crawl_limit"]
                global_issued = int(crawl_runtime["issued"])
                leases: list[FrontierLease] = []
                for row in rows:
                    if len(leases) >= limit:
                        break
                    queue = self._queue_row(crawl_id, row["queue_key"])
                    if queue["blocked_until"] > current:
                        continue
                    if queue["next_allowed_at"] > current:
                        continue
                    if queue["active"] >= queue["max_concurrency"]:
                        continue
                    crawl_limit = queue["crawl_limit"]
                    if crawl_limit is not None and queue["issued"] >= crawl_limit:
                        continue

                    first_lease = int(row["attempt"]) == 0
                    if (
                        first_lease
                        and global_limit is not None
                        and global_issued >= int(global_limit)
                    ):
                        continue

                    token = uuid.uuid4().hex
                    attempt = int(row["attempt"]) + 1
                    expires_at = current + lease_seconds
                    cursor = self._connection.execute(
                        """
                        UPDATE frontier_requests
                        SET state = ?, attempt = ?, lease_token = ?,
                            lease_expires_at = ?
                        WHERE sequence = ? AND state = ?
                        """,
                        (
                            RequestState.LEASED.value,
                            attempt,
                            token,
                            expires_at,
                            row["sequence"],
                            RequestState.QUEUED.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue

                    next_allowed_at = float(queue["next_allowed_at"])
                    if queue["delay_seconds"]:
                        next_allowed_at = current + float(queue["delay_seconds"])
                    self._connection.execute(
                        """
                        UPDATE frontier_queues
                        SET active = active + 1, issued = issued + 1,
                            next_allowed_at = ?
                        WHERE crawl_id = ? AND queue_key = ?
                        """,
                        (next_allowed_at, crawl_id, row["queue_key"]),
                    )
                    if first_lease:
                        global_issued += 1
                        self._connection.execute(
                            """
                            UPDATE frontier_crawls SET issued = ?
                            WHERE crawl_id = ?
                            """,
                            (global_issued, crawl_id),
                        )

                    request = _request_from_row(row)
                    leases.append(
                        FrontierLease(
                            request=request,
                            token=token,
                            attempt=attempt,
                            leased_at=current,
                            expires_at=expires_at,
                        )
                    )

                self._connection.commit()
                return leases
            except Exception:
                self._connection.rollback()
                raise

    async def ack(self, lease: FrontierLease) -> None:
        await self._finish(
            lease,
            state=RequestState.DONE,
            available_at=None,
            error=None,
        )

    async def retry(
        self,
        lease: FrontierLease,
        *,
        available_at: float,
        error: str | None = None,
    ) -> None:
        await self._finish(
            lease,
            state=RequestState.QUEUED,
            available_at=available_at,
            error=error,
        )

    async def fail(self, lease: FrontierLease, *, error: str) -> None:
        await self._finish(
            lease,
            state=RequestState.FAILED,
            available_at=None,
            error=error,
        )

    async def stats(self, crawl_id: str) -> dict[str, int]:
        async with self._lock:
            self._ensure_open()
            counts = {state.value: 0 for state in RequestState}
            rows = self._connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM frontier_requests
                WHERE crawl_id = ?
                GROUP BY state
                """,
                (crawl_id,),
            ).fetchall()
            for row in rows:
                counts[str(row["state"])] = int(row["count"])
            return counts

    async def _finish(
        self,
        lease: FrontierLease,
        *,
        state: RequestState,
        available_at: float | None,
        error: str | None,
    ) -> None:
        async with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._active_row(lease)
                self._release_queue(lease.request.crawl_id, row["queue_key"])
                if available_at is None:
                    self._connection.execute(
                        """
                        UPDATE frontier_requests
                        SET state = ?, lease_token = NULL, lease_expires_at = 0,
                            error = ?
                        WHERE sequence = ?
                        """,
                        (state.value, error, row["sequence"]),
                    )
                else:
                    self._connection.execute(
                        """
                        UPDATE frontier_requests
                        SET state = ?, available_at = ?, lease_token = NULL,
                            lease_expires_at = 0, error = ?
                        WHERE sequence = ?
                        """,
                        (state.value, available_at, error, row["sequence"]),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _active_row(self, lease: FrontierLease) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT * FROM frontier_requests
            WHERE crawl_id = ? AND url = ?
            """,
            (lease.request.crawl_id, lease.request.url),
        ).fetchone()
        if (
            row is None
            or row["state"] != RequestState.LEASED.value
            or row["lease_token"] != lease.token
        ):
            raise RuntimeError(f"lease is no longer active for {lease.request.url}")
        return row

    def _reclaim_expired(self, crawl_id: str, now: float) -> None:
        rows = self._connection.execute(
            """
            SELECT sequence, queue_key FROM frontier_requests
            WHERE crawl_id = ? AND state = ? AND lease_expires_at <= ?
            """,
            (crawl_id, RequestState.LEASED.value, now),
        ).fetchall()
        for row in rows:
            self._release_queue(crawl_id, row["queue_key"])
            self._connection.execute(
                """
                UPDATE frontier_requests
                SET state = ?, lease_token = NULL, lease_expires_at = 0
                WHERE sequence = ?
                """,
                (RequestState.QUEUED.value, row["sequence"]),
            )

    def _release_queue(self, crawl_id: str, queue_key: str) -> None:
        self._ensure_queue(crawl_id, queue_key)
        self._connection.execute(
            """
            UPDATE frontier_queues
            SET active = CASE WHEN active > 0 THEN active - 1 ELSE 0 END
            WHERE crawl_id = ? AND queue_key = ?
            """,
            (crawl_id, queue_key),
        )

    def _ensure_crawl(self, crawl_id: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO frontier_crawls (crawl_id, issued)
            SELECT ?, COUNT(*)
            FROM frontier_requests
            WHERE crawl_id = ? AND attempt > 0
            """,
            (crawl_id, crawl_id),
        )

    def _crawl_row(self, crawl_id: str) -> sqlite3.Row:
        self._ensure_crawl(crawl_id)
        row = self._connection.execute(
            "SELECT * FROM frontier_crawls WHERE crawl_id = ?",
            (crawl_id,),
        ).fetchone()
        assert row is not None
        return row

    def _ensure_queue(self, crawl_id: str, queue_key: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO frontier_queues (crawl_id, queue_key)
            VALUES (?, ?)
            """,
            (crawl_id, queue_key),
        )

    def _queue_row(self, crawl_id: str, queue_key: str) -> sqlite3.Row:
        self._ensure_queue(crawl_id, queue_key)
        row = self._connection.execute(
            """
            SELECT * FROM frontier_queues
            WHERE crawl_id = ? AND queue_key = ?
            """,
            (crawl_id, queue_key),
        ).fetchone()
        assert row is not None
        return row

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteFrontier is closed")


def _request_from_row(row: sqlite3.Row) -> FrontierRequest:
    return FrontierRequest(
        url=str(row["url"]),
        crawl_id=str(row["crawl_id"]),
        queue_key=str(row["queue_key"]),
        priority=int(row["priority"]),
        depth=int(row["depth"]),
        available_at=float(row["available_at"]),
        metadata=_decode_metadata(str(row["metadata_json"])),
    )


def _encode_metadata(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_metadata(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("frontier metadata must decode to an object")
    return decoded


def _validate_queue_policy(policy: QueuePolicy) -> None:
    if policy.max_concurrency <= 0:
        raise ValueError("max_concurrency must be positive")
    if policy.delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")
    if policy.crawl_limit is not None and policy.crawl_limit <= 0:
        raise ValueError("crawl_limit must be positive")
