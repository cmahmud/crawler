# SyndCrawler

An adaptive, self-hostable crawler designed to run the same crawl logic on a laptop, a single VPS, or a distributed worker fleet.

> Status: early alpha. The current vertical slice provides safe HTTP crawling, fast HTML and structured-data parsing, optional proxy routing, deterministic HTTP-to-browser escalation, durable/resumable local crawl state, bounded sitemap discovery, and adaptive conditional recrawls.

## Design goals

- **Direct traffic is first-class.** Proxies are optional routes, never a requirement.
- **Cheap fetch first.** HTTP is preferred until evidence says a page class needs browser rendering.
- **Learn from outcomes.** Engine and network-route outcomes feed inspectable policy telemetry.
- **One kernel, multiple deployments.** The same crawl semantics are intended to work embedded, locally, on one VPS, or across workers.
- **Durable local crawling without external infrastructure.** SQLite provides crawl IDs, leases, dedupe, results, change state, and resume support without Redis/Postgres.
- **Safe egress by default.** Public HTTP(S) targets and proxy endpoints only unless private-network crawling is explicitly enabled.
- **Polite crawling.** `robots.txt` compliance is on by default and frontier queues are host-affine.
- **Efficient recrawls.** HTTP validators and content fingerprints avoid unnecessary downloads and distinguish representation changes from semantic changes.
- **No access-control bypass.** Browser rendering is an extraction mechanism, not a CAPTCHA/challenge bypass mechanism.

## Current stack

- Python 3.12+
- Crawlee Python 1.9+ lifecycle and autoscaling
- Impit-backed HTTP by default through Crawlee
- Selectolax Lexbor parser
- optional Crawlee + Playwright browser rendering
- SQLite durable local frontier/result/change-state store
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

Refresh only resources whose adaptive recrawl deadline is due:

```bash
syndcrawler recrawl example-crawl
```

Force a bounded refresh regardless of `next_fetch_at`:

```bash
syndcrawler recrawl example-crawl --force --limit 100
```

The recrawl summary distinguishes `not_modified`, `unchanged`, `representation_changed`, and `semantic_changed` resources. Conditional HTTP requests use persisted `ETag`/`Last-Modified` validators when available. A `304 Not Modified` updates observation state without replacing the stored page record.

Use a different state directory:

```bash
syndcrawler crawl https://example.com --state-dir ./crawler-state
syndcrawler resume example-crawl --state-dir ./crawler-state
syndcrawler recrawl example-crawl --state-dir ./crawler-state
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

Chromium sandboxing is enabled by default. Some CI/container hosts cannot provide a usable Linux browser sandbox; for a trusted environment where that limitation is understood, it can be explicitly disabled:

```bash
syndcrawler crawl https://example.com --browser --browser-no-sandbox
```

Do not make `--browser-no-sandbox` the default for general-purpose or untrusted workloads.

Robots compliance is enabled by default. Localhost/private-network destinations are blocked by default. For an intentional local development crawl:

```bash
syndcrawler scrape http://127.0.0.1:8000 --allow-private-networks --ignore-robots
```

## Parsed evidence and provenance

Every parsed HTML record can carry deterministic evidence without invoking AI:

- canonical URL
- JSON-LD payloads
- OpenGraph values, including repeated properties such as multiple images
- malformed JSON-LD count
- title and discovered links
- requested/final URL provenance
- rendering assessment and selected fetch/network route
- raw-content SHA-256
- normalized semantic SHA-256
- `ETag` and `Last-Modified` validators when supplied by the origin

Malformed embedded JSON-LD does not make the page fail.

## Discovery

Durable crawl starts can automatically seed their frontier from `robots.txt` and bounded sitemap graphs. Sitemap-discovered pages are deliberately lower priority than explicit seeds and normal HTML link discovery.

The discovery layer supports:

- XML sitemap URL sets
- nested sitemap indexes
- gzip-compressed sitemaps
- plain-text sitemaps
- RSS feeds
- Atom feeds
- `Sitemap:` directives in `robots.txt`

Untrusted discovery documents are bounded in size and count. XML documents containing DTD/entity declarations are rejected, gzip output is bounded while decompressing, and every fetched discovery document, redirect target, and discovered page URL crosses the central egress policy before entering the frontier.

Disable automatic sitemap seeding when needed:

```bash
syndcrawler crawl https://example.com --no-sitemap-discovery
```

## Change detection and recrawls

For durable records, SyndCrawler stores resource state separately from the latest extracted record:

- first/last observation time
- last semantic/representation change time
- change count
- last change classification
- HTTP validators
- raw and semantic fingerprints
- current recrawl interval
- `next_fetch_at`

The default adaptive interval policy starts new resources at 24 hours, backs off stable/304 resources up to 30 days, and pulls semantically changing resources down to a 15-minute minimum. The policy is deterministic and replaceable from Python.

Conditional validators are origin-scoped. If a conditional request redirects to a different origin, SyndCrawler strips `If-None-Match` and `If-Modified-Since` before making the cross-origin request.

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

Conditional recrawl usage:

```python
import asyncio

from syndcrawler.runtime import RecrawlRunner

async def main() -> None:
    runner = RecrawlRunner()
    result = await runner.run("example-crawl")
    print(result.semantic_changed, result.not_modified)

asyncio.run(main())
```

## Runtime model

```text
Embedded / local
    CLI or Python
         |
 durable SQLite frontier + result/change state
         |
    crawler kernel
         |
 Impit HTTP -----> bounded Playwright escalation
    |                    |
 conditional         rendered recrawl
 HTTP recrawl             |
         \_______________/
                 |
           public internet

Single VPS (next)
    API/control plane
         |
    Redis frontier
         |
 HTTP + browser workers
         |
 shared result/artifact store

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

The frontier contract models queue keys, priorities, leases, stale-work reclamation, per-queue concurrency, delays and crawl limits. `MemoryFrontier` provides a lightweight implementation; `SQLiteFrontier` persists the same semantics across process restarts. This gives the upcoming Redis/frontier-service backends a concrete compatibility target.

The adaptive fetch policy currently uses transparent EMA scoring. Recrawl scheduling is similarly deterministic. Both are intentionally inspectable so benchmark evidence can drive later contextual-bandit or learned-policy upgrades.

## Near-term roadmap

- Redis frontier backend for VPS/multi-worker crawling
- shared result/artifact store and richer provenance artifacts
- REST API + Docker deployment profile
- extraction validation and adaptive selector recovery
- page-class policy learning
- benchmark corpus for engine/parser/frontier/recrawl decisions
- distributed frontier service compatible with richer URL Frontier semantics

## Security

See [SECURITY.md](SECURITY.md). Network-facing subsystems are expected to route outbound destinations through the same egress policy. Safe raw/discovery fetches revalidate every redirect before connecting. Browser subresource-level egress enforcement remains an area to harden further before treating the browser worker as suitable for hostile multi-tenant input.

## License

Apache-2.0.
