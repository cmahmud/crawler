# SyndCrawler

An adaptive, self-hostable crawler designed to run the same crawl logic on a laptop, a single VPS, or a distributed worker fleet.

> Status: early alpha. The current vertical slice provides safe HTTP crawling, fast HTML and structured-data parsing, optional proxy routing, deterministic HTTP-to-browser escalation, durable local crawl state, and transparent policy-learning primitives.

## Design goals

- **Direct traffic is first-class.** Proxies are optional routes, never a requirement.
- **Cheap fetch first.** HTTP is preferred until evidence says a page class needs browser rendering.
- **Learn from outcomes.** Engine and network-route outcomes feed inspectable policy telemetry.
- **One kernel, multiple deployments.** The same crawl semantics are intended to work embedded, locally, on one VPS, or across workers.
- **Durable local crawling without external infrastructure.** SQLite provides crawl IDs, leases, dedupe, results, and resume support without Redis/Postgres.
- **Safe egress by default.** Public HTTP(S) targets and proxy endpoints only unless private-network crawling is explicitly enabled.
- **Polite crawling.** `robots.txt` compliance is on by default and frontier queues are host-affine.
- **No access-control bypass.** Browser rendering is an extraction mechanism, not a CAPTCHA/challenge bypass mechanism.

## Current stack

- Python 3.12+
- Crawlee Python 1.9+ lifecycle and autoscaling
- Impit-backed HTTP by default through Crawlee
- Selectolax Lexbor parser
- optional Crawlee + Playwright browser rendering
- SQLite durable local frontier/state store
- Apache-2.0

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install -e .
```

Browser rendering is optional:

```bash
python -m pip install -e '.[browser]'
python -m playwright install chromium
```

A system Chrome/Chromium executable can also be selected with `--browser-executable`.

## CLI

Fetch and parse a single page without creating durable crawl state:

```bash
syndcrawler scrape https://example.com
```

Start a durable same-host crawl:

```bash
syndcrawler crawl https://example.com --max-pages 100
```

The command prints a crawl ID and stores state under `.syndcrawler/<crawl-id>.sqlite3`. Supply your own stable ID when useful:

```bash
syndcrawler crawl https://example.com --crawl-id example-crawl
```

Resume or inspect it later:

```bash
syndcrawler resume example-crawl
syndcrawler status example-crawl
```

Use a different state directory:

```bash
syndcrawler crawl https://example.com --state-dir ./crawler-state
syndcrawler resume example-crawl --state-dir ./crawler-state
```

Write the complete durable result set to JSONL:

```bash
syndcrawler crawl https://example.com --max-pages 1000 --output results.jsonl
```

Make a proxy pool available without requiring it:

```bash
syndcrawler crawl https://example.com \
  --proxy http://user:pass@proxy-a.example:8000 \
  --proxy http://user:pass@proxy-b.example:8000 \
  --proxy-mode auto
```

`--proxy-mode direct` forbids proxy use. `--proxy-mode required` requires a configured proxy. In `auto`, direct and proxy routes are candidates and outcomes are learned per context.

Allow evidence-driven browser escalation:

```bash
syndcrawler crawl https://example.com --browser
```

A framework marker alone does not trigger a browser. The deterministic rendering assessment looks for evidence such as an empty app root, very little server-rendered content, script-heavy shells, and explicit JavaScript-required messages. SSR pages with meaningful content stay on HTTP.

Robots compliance is enabled by default. Localhost/private-network destinations are blocked by default. For an intentional local development crawl:

```bash
syndcrawler scrape http://127.0.0.1:8000 --allow-private-networks --ignore-robots
```

## Parsed evidence

Every parsed HTML record can carry deterministic structured evidence without invoking AI:

- canonical URL
- JSON-LD payloads
- OpenGraph values, including repeated properties such as multiple images
- malformed JSON-LD count
- title and discovered links
- requested/final URL provenance
- rendering assessment and selected fetch/network route

Malformed embedded JSON-LD does not make the page fail.

## Python

Ephemeral single-page usage:

```python
import asyncio

from syndcrawler import CrawlConfig
from syndcrawler.runtime import LocalCrawler

async def main() -> None:
    crawler = LocalCrawler(CrawlConfig(browser_enabled=True))
    page = await crawler.scrape("https://example.com")
    print(page)

asyncio.run(main())
```

Durable local usage:

```python
import asyncio

from syndcrawler.runtime import ResumableCrawler

async def main() -> None:
    crawler = ResumableCrawler()
    run = await crawler.start(["https://example.com"], max_pages=1000)
    print(run.crawl_id, run.stats)

    resumed = await crawler.resume(run.crawl_id)
    print(resumed.completed)

asyncio.run(main())
```

## Runtime model

```text
Embedded / local
    CLI or Python
         |
 durable SQLite frontier (crawl) / ephemeral request (scrape)
         |
    crawler kernel
         |
 Impit HTTP -----> bounded Playwright escalation
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

The frontier contract models queue keys, priorities, leases, stale-work reclamation, per-queue concurrency, delays and crawl limits. `MemoryFrontier` provides a lightweight implementation; `SQLiteFrontier` persists the same semantics across process restarts. This gives later Redis/frontier-service backends a concrete compatibility target.

The adaptive policy currently uses transparent EMA scoring. That is deliberate: we can benchmark and inspect it before replacing it with a more sophisticated contextual bandit or learned model.

## Near-term roadmap

- sitemap/feed/runtime-network discovery
- conditional fetch + content/change detection for recrawls
- Redis frontier backend for VPS workers
- provenance artifact store and extraction validation
- page-class learning and recrawl scheduling
- benchmark corpus for engine/parser/frontier decisions
- REST API + Docker deployment profile

## Security

See [SECURITY.md](SECURITY.md). All outbound destinations should eventually cross the same egress boundary, including redirects, sitemaps, browser navigation, nested resources and proxy endpoints. Pre-queue validation is defense in depth; transport-level redirect/connection validation remains mandatory.

## License

Apache-2.0.
