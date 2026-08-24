from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from syndcrawler.config import CrawlConfig
from syndcrawler.core.routes import ProxyMode
from syndcrawler.runtime import LocalCrawler, RecrawlRunner, ResumableCrawler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syndcrawler",
        description="Adaptive self-hostable crawler",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scrape = subparsers.add_parser("scrape", help="fetch and parse one page")
    scrape.add_argument("url")
    _add_fetch_flags(scrape)

    crawl = subparsers.add_parser(
        "crawl",
        help="start a durable crawl from one or more seeds",
    )
    crawl.add_argument("urls", nargs="+")
    crawl.add_argument("--max-pages", type=int, default=100)
    crawl.add_argument(
        "--cross-domain",
        action="store_true",
        help="follow links across hostnames",
    )
    crawl.add_argument(
        "--crawl-id",
        help="optional durable crawl ID; generated automatically when omitted",
    )
    _add_state_dir_flag(crawl)
    _add_discovery_flags(crawl)
    _add_fetch_flags(crawl)

    resume = subparsers.add_parser(
        "resume",
        help="resume a durable crawl from its SQLite state",
    )
    resume.add_argument("crawl_id")
    _add_state_dir_flag(resume)
    _add_fetch_flags(resume)

    recrawl = subparsers.add_parser(
        "recrawl",
        help="conditionally refresh due resources in a durable crawl",
    )
    recrawl.add_argument("crawl_id")
    recrawl.add_argument("--limit", type=int, default=100)
    recrawl.add_argument(
        "--force",
        action="store_true",
        help="refresh resources even when their next_fetch_at is in the future",
    )
    _add_state_dir_flag(recrawl)
    _add_fetch_flags(recrawl, include_output=False)

    status_parser = subparsers.add_parser(
        "status",
        help="show durable crawl state without fetching",
    )
    status_parser.add_argument("crawl_id")
    _add_state_dir_flag(status_parser)

    tui = subparsers.add_parser(
        "tui",
        help="launch the interactive OpenTUI server operator console",
    )
    tui.add_argument(
        "--api-url",
        help="override the control-plane base URL passed to the TUI",
    )
    tui.add_argument(
        "--env-file",
        default=".env",
        help="load API token/bind/port from this env file before launch",
    )
    tui.add_argument(
        "--binary",
        help="explicit syndcrawler-tui executable path",
    )

    serve = subparsers.add_parser(
        "serve",
        help="run the Redis/PostgreSQL HTTP control plane",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        default="info",
    )

    worker = subparsers.add_parser(
        "worker",
        help="process active crawls and scheduled recrawls",
    )
    worker.add_argument(
        "--once",
        action="store_true",
        help="run one initial-work and recrawl sweep, then exit",
    )
    worker.add_argument("--poll-interval", type=float)
    worker.add_argument("--error-backoff", type=float)
    worker.add_argument("--max-crawls", type=int, default=100)
    worker.add_argument("--per-crawl-limit", type=int)
    worker.add_argument("--recrawl-limit", type=int)
    worker.add_argument("--recrawl-lease-seconds", type=float)
    worker.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default="info",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = asyncio.run(_run(args))
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    raise SystemExit(code)


async def _run(args: argparse.Namespace) -> int:
    if args.command == "status":
        crawler = ResumableCrawler(state_dir=args.state_dir)
        result = await crawler.status(args.crawl_id)
        print(json.dumps(_run_summary(result), indent=2))
        return 0

    if args.command == "tui":
        return await _run_tui(args)

    if args.command == "serve":
        return await _run_server(args)

    if args.command == "worker":
        return await _run_worker(args)

    config = _config_from_args(args)

    if args.command == "scrape":
        crawler = LocalCrawler(config)
        record = await crawler.scrape(args.url)
        if record is not None:
            print(json.dumps(record.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "recrawl":
        runner = RecrawlRunner(config, state_dir=args.state_dir)
        result = await runner.run(
            args.crawl_id,
            limit=args.limit,
            force=args.force,
        )
        print(json.dumps(_recrawl_summary(result), indent=2))
        return 0

    crawler = ResumableCrawler(config, state_dir=args.state_dir)
    if args.command == "crawl":
        result = await crawler.start(
            args.urls,
            crawl_id=args.crawl_id,
            follow_links=True,
            max_pages=args.max_pages,
        )
    else:
        result = await crawler.resume(args.crawl_id)

    print(json.dumps(_run_summary(result, output=getattr(args, "output", None)), indent=2))
    return 0


async def _run_tui(args: argparse.Namespace) -> int:
    env = os.environ.copy()
    env_file = Path(args.env_file)
    if env_file.is_file():
        env.update(_read_env_file(env_file))
    if args.api_url:
        env["SYNCRAWLER_TUI_API_URL"] = args.api_url

    candidates: list[Path] = []
    if args.binary:
        candidates.append(Path(args.binary).expanduser())
    discovered = shutil.which("syndcrawler-tui")
    if discovered:
        candidates.append(Path(discovered))
    source_binary = Path(__file__).resolve().parents[2] / "tui" / "dist" / "syndcrawler-tui"
    candidates.append(source_binary)

    binary = next((path for path in candidates if path.is_file()), None)
    if binary is None:
        raise RuntimeError(
            "SyndCrawler TUI binary is not installed. From a source checkout run "
            "`./tui/build.sh`, then retry `syndcrawler tui`, or pass --binary PATH."
        )

    process = await asyncio.create_subprocess_exec(str(binary), env=env)
    return await process.wait()


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


async def _run_server(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "server command requires `pip install 'syndcrawler[server]'`"
        ) from exc

    config = uvicorn.Config(
        "syndcrawler.control_plane.api:create_app_from_env",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    server = uvicorn.Server(config)
    await server.serve()
    return 0 if server.started else 1


async def _run_worker(args: argparse.Namespace) -> int:
    from syndcrawler.runtime.daemon import run_worker_loop
    from syndcrawler.runtime.server_env import (
        env_float,
        env_int,
        server_runtime_from_env,
    )
    from syndcrawler.runtime.server_recrawl import ServerRecrawlScheduler

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    poll_interval = (
        args.poll_interval
        if args.poll_interval is not None
        else env_float("SYNCRAWLER_WORKER_POLL_INTERVAL", 1.0, minimum=0.001)
    )
    error_backoff = (
        args.error_backoff
        if args.error_backoff is not None
        else env_float("SYNCRAWLER_WORKER_ERROR_BACKOFF", 5.0, minimum=0.001)
    )
    recrawl_limit = (
        args.recrawl_limit
        if args.recrawl_limit is not None
        else env_int("SYNCRAWLER_RECRAWL_BATCH_SIZE", 100, minimum=1)
    )
    recrawl_lease_seconds = (
        args.recrawl_lease_seconds
        if args.recrawl_lease_seconds is not None
        else env_float("SYNCRAWLER_RECRAWL_LEASE_SECONDS", 120.0, minimum=0.001)
    )
    runtime = await server_runtime_from_env()
    try:
        if args.once:
            sweep = await runtime.run_runnable_once(
                max_crawls=args.max_crawls,
                per_crawl_limit=args.per_crawl_limit,
            )
            recrawl = await ServerRecrawlScheduler(
                runtime.store,
                runtime.config,
                policy=runtime.recrawl_policy,
            ).run_once(
                limit=recrawl_limit,
                lease_seconds=recrawl_lease_seconds,
            )
            print(json.dumps(_sweep_summary(sweep, recrawl), indent=2))
            return 0
        await run_worker_loop(
            runtime,
            poll_interval_seconds=poll_interval,
            error_backoff_seconds=error_backoff,
            max_crawls=args.max_crawls,
            per_crawl_limit=args.per_crawl_limit,
            recrawl_limit=recrawl_limit,
            recrawl_lease_seconds=recrawl_lease_seconds,
        )
        return 0
    finally:
        await runtime.close()


def _config_from_args(args: argparse.Namespace) -> CrawlConfig:
    output = getattr(args, "output", None)
    return CrawlConfig(
        max_pages=getattr(args, "max_pages", 100),
        max_concurrency=args.concurrency,
        max_retries=args.retries,
        same_domain=not getattr(args, "cross_domain", False),
        respect_robots_txt=not args.ignore_robots,
        allow_private_networks=args.allow_private_networks,
        proxy_urls=tuple(args.proxy),
        proxy_mode=ProxyMode(args.proxy_mode),
        sitemap_discovery_enabled=not getattr(args, "no_sitemap_discovery", False),
        sitemap_max_documents=getattr(args, "sitemap_max_documents", 32),
        sitemap_max_depth=getattr(args, "sitemap_max_depth", 4),
        sitemap_max_urls=getattr(args, "sitemap_max_urls", 10_000),
        browser_enabled=args.browser,
        browser_type=args.browser_type,
        browser_executable_path=(
            Path(args.browser_executable) if args.browser_executable else None
        ),
        browser_max_concurrency=args.browser_concurrency,
        browser_navigation_timeout_seconds=args.browser_timeout,
        browser_settle_seconds=args.browser_settle_ms / 1000,
        browser_chromium_sandbox=not args.browser_no_sandbox,
        output=Path(output) if output else None,
    )


def _run_summary(result, *, output: str | None = None) -> dict[str, object]:
    return {
        "crawl_id": result.crawl_id,
        "completed": result.completed,
        "pages": len(result.records),
        "stats": result.stats,
        "state": str(result.state_path),
        "output": output,
    }


def _recrawl_summary(result) -> dict[str, object]:
    return {
        "crawl_id": result.crawl_id,
        "checked": result.checked,
        "not_modified": result.not_modified,
        "unchanged": result.unchanged,
        "representation_changed": result.representation_changed,
        "semantic_changed": result.semantic_changed,
        "failures": result.failures,
        "items": [
            {
                "url": item.url,
                "change_kind": (
                    item.change_kind.value if item.change_kind is not None else None
                ),
                "status_code": item.status_code,
                "next_fetch_at": item.next_fetch_at,
                "error": item.error,
            }
            for item in result.items
        ],
    }


def _sweep_summary(result, recrawl) -> dict[str, int]:
    return {
        "crawls_seen": result.crawls_seen,
        "leased": result.leased,
        "persisted": result.persisted,
        "failed": result.failed,
        "discovered": result.discovered,
        "recrawl_claimed": recrawl.claimed,
        "recrawl_checked": recrawl.checked,
        "recrawl_not_modified": recrawl.not_modified,
        "recrawl_unchanged": recrawl.unchanged,
        "recrawl_representation_changed": recrawl.representation_changed,
        "recrawl_semantic_changed": recrawl.semantic_changed,
        "recrawl_failures": recrawl.failures,
    }


def _add_state_dir_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        default=".syndcrawler",
        help="directory for durable crawl SQLite state",
    )


def _add_discovery_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-sitemap-discovery",
        action="store_true",
        help="do not seed the durable frontier from robots.txt/sitemaps",
    )
    parser.add_argument("--sitemap-max-documents", type=int, default=32)
    parser.add_argument("--sitemap-max-depth", type=int, default=4)
    parser.add_argument("--sitemap-max-urls", type=int, default=10_000)


def _add_fetch_flags(
    parser: argparse.ArgumentParser,
    *,
    include_output: bool = True,
) -> None:
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--retries", type=int, default=2)
    if include_output:
        parser.add_argument("--output", help="write JSON or JSONL records to this path")
    parser.add_argument(
        "--proxy",
        action="append",
        default=[],
        metavar="URL",
        help="make an HTTP(S) proxy route available; repeat for a pool",
    )
    parser.add_argument(
        "--proxy-mode",
        choices=[mode.value for mode in ProxyMode],
        default=ProxyMode.AUTO.value,
        help="auto learns direct/proxy preference; direct/required are hard overrides",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="allow evidence-driven Playwright rendering escalation",
    )
    parser.add_argument(
        "--browser-type",
        choices=["chromium", "chrome", "firefox", "webkit"],
        default="chromium",
    )
    parser.add_argument(
        "--browser-executable",
        metavar="PATH",
        help="optional browser executable path",
    )
    parser.add_argument("--browser-concurrency", type=int, default=2)
    parser.add_argument("--browser-timeout", type=float, default=45.0)
    parser.add_argument("--browser-settle-ms", type=float, default=250.0)
    parser.add_argument(
        "--browser-no-sandbox",
        action="store_true",
        help="disable the Chromium sandbox only when the host cannot provide one",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="disable robots.txt compliance",
    )
    parser.add_argument(
        "--allow-private-networks",
        action="store_true",
        help="allow localhost/private targets and proxies for controlled development/intranet use",
    )
