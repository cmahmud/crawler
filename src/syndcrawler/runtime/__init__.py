from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from syndcrawler.runtime.local import LocalCrawler
    from syndcrawler.runtime.recrawl import RecrawlItemResult, RecrawlRunner, RecrawlRunResult
    from syndcrawler.runtime.resumable import CrawlRunResult, ResumableCrawler
    from syndcrawler.runtime.server import ServerCrawlStatus, ServerRuntime, WorkerSweepResult
    from syndcrawler.runtime.server_recrawl import (
        ServerRecrawlScheduler,
        ServerRecrawlSweepResult,
    )
    from syndcrawler.runtime.worker import CrawlWorker, WorkerBatchResult

__all__ = [
    "CrawlRunResult",
    "CrawlWorker",
    "LocalCrawler",
    "RecrawlItemResult",
    "RecrawlRunResult",
    "RecrawlRunner",
    "ResumableCrawler",
    "ServerCrawlStatus",
    "ServerRecrawlScheduler",
    "ServerRecrawlSweepResult",
    "ServerRuntime",
    "WorkerBatchResult",
    "WorkerSweepResult",
]


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
    if name in {"RecrawlItemResult", "RecrawlRunResult", "RecrawlRunner"}:
        from syndcrawler.runtime.recrawl import (
            RecrawlItemResult,
            RecrawlRunner,
            RecrawlRunResult,
        )

        return {
            "RecrawlItemResult": RecrawlItemResult,
            "RecrawlRunResult": RecrawlRunResult,
            "RecrawlRunner": RecrawlRunner,
        }[name]
    if name in {"ServerCrawlStatus", "ServerRuntime", "WorkerSweepResult"}:
        from syndcrawler.runtime.server import (
            ServerCrawlStatus,
            ServerRuntime,
            WorkerSweepResult,
        )

        return {
            "ServerCrawlStatus": ServerCrawlStatus,
            "ServerRuntime": ServerRuntime,
            "WorkerSweepResult": WorkerSweepResult,
        }[name]
    if name in {"ServerRecrawlScheduler", "ServerRecrawlSweepResult"}:
        from syndcrawler.runtime.server_recrawl import (
            ServerRecrawlScheduler,
            ServerRecrawlSweepResult,
        )

        return {
            "ServerRecrawlScheduler": ServerRecrawlScheduler,
            "ServerRecrawlSweepResult": ServerRecrawlSweepResult,
        }[name]
    if name in {"CrawlWorker", "WorkerBatchResult"}:
        from syndcrawler.runtime.worker import CrawlWorker, WorkerBatchResult

        return {
            "CrawlWorker": CrawlWorker,
            "WorkerBatchResult": WorkerBatchResult,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
