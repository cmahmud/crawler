from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from syndcrawler.core.change import ChangeAssessment
from syndcrawler.models import PageRecord


class ResultStore(Protocol):
    """Minimal result persistence contract required by crawl workers."""

    async def put_result(
        self,
        crawl_id: str,
        request_url: str,
        record: PageRecord,
        *,
        now: float | None = None,
    ) -> ChangeAssessment | None: ...


@dataclass(frozen=True, slots=True)
class StoredResult:
    crawl_id: str
    request_url: str
    record: PageRecord
    recorded_at: float
