from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable

from syndcrawler.runtime.server import ServerRuntime, WorkerSweepResult
from syndcrawler.runtime.server_recrawl import ServerRecrawlScheduler, ServerRecrawlSweepResult

logger = logging.getLogger(__name__)


async def run_worker_loop(
    runtime: ServerRuntime,
    *,
    poll_interval_seconds: float = 1.0,
    error_backoff_seconds: float = 5.0,
    max_crawls: int = 100,
    per_crawl_limit: int | None = None,
    recrawl_limit: int = 100,
    recrawl_lease_seconds: float = 120.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Continuously process initial crawl work and due distributed recrawls.

    When no stop event is supplied, the daemon owns one and connects it to
    SIGTERM/SIGINT where the event loop supports signal handlers. This lets
    container shutdown finish the current sweep and return through the caller's
    normal resource-cleanup path.
    """

    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    if error_backoff_seconds <= 0:
        raise ValueError("error_backoff_seconds must be positive")
    if max_crawls <= 0:
        raise ValueError("max_crawls must be positive")
    if per_crawl_limit is not None and per_crawl_limit <= 0:
        raise ValueError("per_crawl_limit must be positive")
    if recrawl_limit <= 0:
        raise ValueError("recrawl_limit must be positive")
    if recrawl_lease_seconds <= 0:
        raise ValueError("recrawl_lease_seconds must be positive")

    owned_stop_event = stop_event is None
    event = stop_event or asyncio.Event()
    cleanup_signals: Callable[[], None] = _noop
    if owned_stop_event:
        cleanup_signals = _install_signal_handlers(event)

    recrawls = ServerRecrawlScheduler(
        runtime.store,
        runtime.config,
        policy=runtime.recrawl_policy,
    )
    try:
        while not event.is_set():
            try:
                sweep = await runtime.run_runnable_once(
                    max_crawls=max_crawls,
                    per_crawl_limit=per_crawl_limit,
                )
                recrawl = await recrawls.run_once(
                    limit=recrawl_limit,
                    lease_seconds=recrawl_lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker sweep failed")
                await _wait(error_backoff_seconds, event)
                continue

            _log_sweep(sweep, recrawl)
            if sweep.leased == 0 and recrawl.claimed == 0:
                await _wait(poll_interval_seconds, event)
    finally:
        cleanup_signals()


async def _wait(seconds: float, stop_event: asyncio.Event) -> None:
    if stop_event.is_set():
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


def _noop() -> None:
    return


def _install_signal_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(sig)

    def cleanup() -> None:
        for sig in installed:
            loop.remove_signal_handler(sig)

    return cleanup


def _log_sweep(sweep: WorkerSweepResult, recrawl: ServerRecrawlSweepResult) -> None:
    if sweep.leased == 0 and sweep.failed == 0 and recrawl.claimed == 0:
        logger.debug("worker sweep idle across %s active crawls", sweep.crawls_seen)
        return
    logger.info(
        "worker sweep crawls=%s leased=%s persisted=%s failed=%s discovered=%s "
        "recrawl_claimed=%s recrawl_checked=%s recrawl_failed=%s",
        sweep.crawls_seen,
        sweep.leased,
        sweep.persisted,
        sweep.failed,
        sweep.discovered,
        recrawl.claimed,
        recrawl.checked,
        recrawl.failures,
    )
