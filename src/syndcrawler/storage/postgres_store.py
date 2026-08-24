from __future__ import annotations

from syndcrawler.storage.postgres_crawl_store import (
    PostgresCrawlStore as _BasePostgresCrawlStore,
)
from syndcrawler.storage.postgres_crawl_store import _SCHEMA_STATEMENTS

_SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(187003, 0)"


class PostgresCrawlStore(_BasePostgresCrawlStore):
    """PostgreSQL store with cross-process-safe schema initialization.

    PostgreSQL's `CREATE TABLE IF NOT EXISTS` can still race in the system
    catalog when two fresh processes initialize an empty database at exactly the
    same time. A transaction-scoped advisory lock serializes only the migration
    transaction; normal result and crawl operations remain fully concurrent.
    """

    async def initialize(self) -> None:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(_SCHEMA_LOCK_SQL)
                for statement in _SCHEMA_STATEMENTS:
                    await connection.execute(statement)
