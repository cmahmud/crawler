from __future__ import annotations

import asyncio
import logging

from syndcrawler.runtime.server import ServerRuntime, WorkerSweepResult

logger = logging.getLogger(__name__)


async def run_worker_loop(
    runtime: ServerRuntime,
    *,
    poll_interval_seconds: float = 1.0,
    error_backoff_seconds: float = 5.0,
    max_crawls: int = 100,
    per_crawl_limit: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Continuously process active shared crawls until cancelled or stopped."""

    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    if error_backoff_seconds <= 0:
        raise ValueError("error_backoff_seconds must be positive")
    if max_crawls <= 0:
        raise ValueError("max_crawls must be positive")
    if per_crawl_limit is not None and per_crawl_limit <= 0:
        raise ValueError("per_crawl_limit must be positive")

    while stop_event is None or not stop_event.is_set():
        try:
            sweep = await runtime.run_runnable_once(
                max_crawls=max_crawls,
                per_crawl_limit=per_crawl_limit,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker sweep failed")
            await _wait(error_backoff_seconds, stop_event)
            continue

        _log_sweep(sweep)
        if sweep.leased == 0:
            await _wait(poll_interval_seconds, stop_event)


async def _wait(seconds: float, stop_event: asyncio.Event | None) -> None:
    if stop_event is None:
        await asyncio.sleep(seconds)
        return
    if stop_event.is_set():
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


def _log_sweep(sweep: WorkerSweepResult) -> None:
    if sweep.leased == 0 and sweep.failed == 0:
        logger.debug("worker sweep idle across %s crawls", sweep.crawls_seen)
        return
    logger.info(
        "worker sweep crawls=%s leased=%s persisted=%s failed=%s discovered=%s",
        sweep.crawls_seen,
        sweep.leased,
        sweep.persisted,
        sweep.failed,
        sweep.discovered,
    )
