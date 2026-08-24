from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from syndcrawler.runtime.local import LocalCrawler
    from syndcrawler.runtime.resumable import CrawlRunResult, ResumableCrawler

__all__ = ["CrawlRunResult", "LocalCrawler", "ResumableCrawler"]


def __getattr__(name: str) -> Any:
    """Load public runtime classes lazily to keep submodules import-independent."""

    if name == "LocalCrawler":
        from syndcrawler.runtime.local import LocalCrawler

        return LocalCrawler
    if name in {"CrawlRunResult", "ResumableCrawler"}:
        from syndcrawler.runtime.resumable import CrawlRunResult, ResumableCrawler

        return {
            "CrawlRunResult": CrawlRunResult,
            "ResumableCrawler": ResumableCrawler,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
