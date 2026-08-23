# SyndCrawler

An adaptive, self-hostable crawler designed to run the same crawl logic on a laptop, a single VPS, or a distributed worker fleet.

> Status: early alpha. The first vertical slice is intentionally small: safe HTTP crawling, fast HTML parsing, local frontier semantics, and the policy primitives that later choose between HTTP, browser and network routes.

## Design goals

- **Direct traffic is first-class.** Proxies are optional routes, never a requirement.
- **Cheap fetch first.** HTTP is preferred until evidence says a page class needs browser rendering.
- **Learn from outcomes.** Fetch engine, route, extraction strategy and recrawl policy are intended to adapt by domain/page class.
- **Local and server deployment use the same contracts.** Local mode needs no Redis or database; durable backends can replace the frontier later.
- **Safe egress by default.** Public HTTP(S) targets only unless private-network crawling is explicitly enabled.
- **Polite crawling.** `robots.txt` compliance is on by default and frontier queues are host-affine.
- **No access-control bypass.** Browser rendering is an extraction mechanism, not a CAPTCHA/challenge bypass mechanism.

## Current stack

- Python 3.12+
- Crawlee Python 1.9+ lifecycle and autoscaling
- Impit-backed HTTP by default through Crawlee
- Selectolax Lexbor parser
- Apache-2.0

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install -e .
```

## CLI

Fetch and parse a single page:

```bash
syndcrawler scrape https://example.com
```

Crawl up to 100 same-host pages:

```bash
syndcrawler crawl https://example.com --max-pages 100
```

Write records to JSONL:

```bash
syndcrawler crawl https://example.com --max-pages 1000 --output results.jsonl
```

Robots compliance is enabled by default. Localhost/private-network destinations are blocked by default. For an intentional local development crawl:

```bash
syndcrawler scrape http://127.0.0.1:8000 --allow-private-networks --ignore-robots
```

## Python

```python
import asyncio

from syndcrawler.runtime import LocalCrawler

async def main() -> None:
    crawler = LocalCrawler()
    page = await crawler.scrape("https://example.com")
    print(page)

asyncio.run(main())
```

## Runtime model

```text
Embedded / local
    CLI or Python
         |
    crawler kernel
         |
  Crawlee + Impit
         |
   public internet

Single VPS (planned)
    API/control plane
         |
    Redis frontier
         |
 HTTP + browser workers
         |
  artifact/result store

Cluster (planned)
    control plane
         |
 host-affine frontier
         |
   worker fleet
```

## Kernel boundaries

```text
frontier -> policy -> fetch engine -> artifact -> parser/extractor -> validation -> output
              |             |
          page class     network route
```

The in-memory frontier already models queue keys, priorities, leases, stale-work reclamation, per-queue concurrency, delays and crawl limits. This keeps local mode lightweight while giving Redis/SQL/distributed implementations a concrete compatibility target.

The adaptive policy currently uses transparent EMA scoring. That is deliberate: we can benchmark and inspect it before replacing it with a more sophisticated contextual bandit or learned model.

## Near-term roadmap

- persistent local frontier and resumable crawl IDs
- Redis frontier backend for VPS workers
- browser worker and HTTP-to-browser escalation
- direct/proxy route broker with health scoring
- structured-data extraction and provenance artifacts
- sitemap/feed/runtime-network discovery
- page-class learning and recrawl scheduling
- benchmark corpus for engine/parser/frontier decisions
- REST API + Docker deployment profile

## Security

See [SECURITY.md](SECURITY.md). All outbound destinations should eventually cross the same egress boundary, including redirects, sitemaps, browser navigation, nested resources and proxy endpoints.

## License

Apache-2.0.
