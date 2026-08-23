from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from syndcrawler.config import CrawlConfig
from syndcrawler.runtime import LocalCrawler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="syndcrawler", description="Adaptive self-hostable crawler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scrape = subparsers.add_parser("scrape", help="fetch and parse one page")
    scrape.add_argument("url")
    _add_common_flags(scrape, default_pages=1)

    crawl = subparsers.add_parser("crawl", help="crawl a site starting from one or more seeds")
    crawl.add_argument("urls", nargs="+")
    _add_common_flags(crawl, default_pages=100)
    crawl.add_argument("--cross-domain", action="store_true", help="follow links across hostnames")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    raise SystemExit(asyncio.run(_run(args)))


async def _run(args: argparse.Namespace) -> int:
    config = CrawlConfig(max_pages=args.max_pages, max_concurrency=args.concurrency, max_retries=args.retries, same_domain=not getattr(args, "cross_domain", False), respect_robots_txt=not args.ignore_robots, allow_private_networks=args.allow_private_networks, output=Path(args.output) if args.output else None)
    crawler = LocalCrawler(config)
    if args.command == "scrape":
        record = await crawler.scrape(args.url)
        if record is not None:
            print(json.dumps(record.to_dict(), indent=2, ensure_ascii=False))
        return 0
    records = await crawler.crawl(args.urls)
    print(json.dumps({"pages": len(records), "output": args.output}, indent=2))
    return 0


def _add_common_flags(parser: argparse.ArgumentParser, *, default_pages: int) -> None:
    parser.add_argument("--max-pages", type=int, default=default_pages)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--output", help="write JSON or JSONL records to this path")
    parser.add_argument("--ignore-robots", action="store_true", help="disable robots.txt compliance")
    parser.add_argument("--allow-private-networks", action="store_true", help="allow localhost/private IPs for explicit intranet or development crawls")
