from __future__ import annotations

import re
import uuid

_CRAWL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def new_crawl_id() -> str:
    return f"cr_{uuid.uuid4().hex[:16]}"


def validate_crawl_id(crawl_id: str) -> str:
    if not _CRAWL_ID_RE.fullmatch(crawl_id):
        raise ValueError(
            "crawl_id must be 1-128 characters using letters, numbers, '.', '_' or '-'"
        )
    return crawl_id


def crawl_is_complete(stats: dict[str, int], max_pages: int) -> bool:
    terminal = stats.get("done", 0) + stats.get("failed", 0)
    return terminal >= max_pages or (
        stats.get("queued", 0) == 0 and stats.get("leased", 0) == 0
    )
