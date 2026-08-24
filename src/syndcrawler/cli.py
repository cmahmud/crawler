from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from syndcrawler.config import CrawlConfig
from syndcrawler.core.routes import ProxyMode
from syndcrawler.runtime import LocalCrawler, ResumableCrawler


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
    _add_fetch_flags(crawl)

    resume = subparsers.add_parser(
        "resume",
        help="resume a durable crawl from its SQLite state",
    )
    resume.add_argument("crawl_id")
    _add_state_dir_flag(resume)
    _add_fetch_flags(resume)

    status = subparsers.add_parser(
        "status",
        help="show durable crawl state without fetching",
    )
    status.add_argument("crawl_id")
    _add_state_dir_flag(status)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = asyncio.run(_run(args))
    except ValueError as exc:
        parser.error(str(exc))
    raise SystemExit(code)


async def _run(args: argparse.Namespace) -> int:
    if args.command == "status":
        crawler = ResumableCrawler(state_dir=args.state_dir)
        result = await crawler.status(args.crawl_id)
        print(json.dumps(_run_summary(result), indent=2))
        return 0

    config = _config_from_args(args)

    if args.command == "scrape":
        crawler = LocalCrawler(config)
        record = await crawler.scrape(args.url)
        if record is not None:
            print(json.dumps(record.to_dict(), indent=2, ensure_ascii=False))
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

    print(json.dumps(_run_summary(result, output=args.output), indent=2))
    return 0


def _config_from_args(args: argparse.Namespace) -> CrawlConfig:
    return CrawlConfig(
        max_pages=getattr(args, "max_pages", 100),
        max_concurrency=args.concurrency,
        max_retries=args.retries,
        same_domain=not getattr(args, "cross_domain", False),
        respect_robots_txt=not args.ignore_robots,
        allow_private_networks=args.allow_private_networks,
        proxy_urls=tuple(args.proxy),
        proxy_mode=ProxyMode(args.proxy_mode),
        browser_enabled=args.browser,
        browser_type=args.browser_type,
        browser_executable_path=(
            Path(args.browser_executable) if args.browser_executable else None
        ),
        browser_max_concurrency=args.browser_concurrency,
        browser_navigation_timeout_seconds=args.browser_timeout,
        browser_settle_seconds=args.browser_settle_ms / 1000,
        browser_chromium_sandbox=not args.browser_no_sandbox,
        output=Path(args.output) if args.output else None,
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


def _add_state_dir_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        default=".syndcrawler",
        help="directory for durable crawl SQLite state",
    )


def _add_fetch_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--retries", type=int, default=2)
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
