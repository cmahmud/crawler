# SyndCrawler

An adaptive, self-hostable web crawler designed to use the same crawling kernel on a laptop, a single VPS, or a multi-worker deployment.

> **Status:** alpha. The current `kernel-v1` branch includes the complete v0.1 data path: HTTP crawling, evidence-driven browser rendering, durable local state, safe discovery, conditional recrawls, a Redis/PostgreSQL distributed runtime, authenticated REST control plane, worker daemon, and Docker Compose deployment profile.

## Design goals

- **Direct traffic is first-class.** Proxies are optional routes, never a requirement.
- **Cheap fetch first.** HTTP is preferred until page evidence says rendering is needed.
- **Inspectable adaptation.** Fetch-engine and network-route outcomes feed transparent policy telemetry.
- **One kernel, multiple deployments.** Local and distributed execution share the same frontier/result/worker contracts.
- **Crash-resumable local crawling.** SQLite provides crawl IDs, leases, dedupe, results, change state, and resume support without external infrastructure.
- **Distributed correctness.** Redis provides atomic work leasing and crawl-wide budgets; PostgreSQL provides shared manifests, results, lifecycle, change history, and recrawl claims.
- **Safe egress by default.** Public HTTP(S) targets are allowed; private/non-global destinations require explicit opt-in.
- **Polite crawling.** `robots.txt` compliance is enabled by default and frontier queues are host-affine.
- **Efficient monitoring.** `ETag`/`Last-Modified`, raw fingerprints, semantic fingerprints, and adaptive schedules reduce unnecessary recrawl work.
- **No access-control bypass.** Browser rendering is an extraction mechanism, not a CAPTCHA/challenge bypass mechanism.

## Current stack

- Python 3.12+
- Crawlee Python 1.9+ lifecycle/autoscaling
- Impit-backed HTTP transport
- Selectolax/Lexbor HTML parsing
- optional Crawlee + Playwright browser rendering
- SQLite for durable local crawls
- Redis for distributed frontier/leasing
- PostgreSQL for shared result/change/control state
- FastAPI + Uvicorn for the optional control plane
- Docker Compose single-VPS profile
- Apache-2.0

## Install

Local HTTP crawler:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e .
```

Browser support:

```bash
python -m pip install -e '.[browser]'
python -m playwright install chromium
```

Server/control-plane support:

```bash
python -m pip install -e '.[server]'
```

The production container installs both `server` and `browser` extras.

## Local CLI

Fetch and parse one page without durable state:

```bash
syndcrawler scrape https://example.com
```

Start a durable same-host crawl:

```bash
syndcrawler crawl https://example.com --max-pages 100
```

The command prints a crawl ID and stores state at `.syndcrawler/<crawl-id>.sqlite3`. A stable ID can be supplied explicitly:

```bash
syndcrawler crawl https://example.com --crawl-id example-crawl
```

Resume or inspect it later:

```bash
syndcrawler resume example-crawl
syndcrawler status example-crawl
```

Run due conditional recrawls:

```bash
syndcrawler recrawl example-crawl
```

Force a bounded refresh regardless of `next_fetch_at`:

```bash
syndcrawler recrawl example-crawl --force --limit 100
```

Write durable results to JSONL:

```bash
syndcrawler crawl https://example.com --max-pages 1000 --output results.jsonl
```

Use a different state directory:

```bash
syndcrawler crawl https://example.com --state-dir ./crawler-state
syndcrawler resume example-crawl --state-dir ./crawler-state
```

### Optional proxy routing

Make proxies available without requiring them:

```bash
syndcrawler crawl https://example.com \
  --proxy http://user:pass@proxy-a.example:8000 \
  --proxy http://user:pass@proxy-b.example:8000 \
  --proxy-mode auto
```

- `direct`: proxy use is forbidden.
- `required`: a configured proxy is mandatory.
- `auto`: direct and proxy routes are candidates and outcomes are learned per context.

### Browser escalation

Allow evidence-driven rendering:

```bash
syndcrawler crawl https://example.com --browser
```

A framework marker alone does not trigger a browser. The rendering assessment considers signals such as empty app roots, little useful SSR content, script-heavy shells, and explicit JavaScript-required messages. Meaningful SSR pages stay on HTTP.

Chromium sandboxing is enabled by default. On a trusted host that genuinely cannot provide it, sandboxing can be disabled explicitly:

```bash
syndcrawler crawl https://example.com --browser --browser-no-sandbox
```

Do not make no-sandbox operation a general default.

### Robots and private networks

Robots compliance is enabled by default. Localhost/private-network targets are blocked by default. Intentional local development can opt in:

```bash
syndcrawler scrape http://127.0.0.1:8000 --allow-private-networks --ignore-robots
```

## Parsed evidence and provenance

HTML records can carry deterministic evidence without invoking AI:

- title and filtered discovered links
- canonical URL
- JSON-LD payloads
- OpenGraph values, including repeated properties
- malformed JSON-LD count
- requested/final URL provenance
- rendering assessment
- selected fetch engine and network route
- raw-content SHA-256
- normalized semantic SHA-256
- `ETag` and `Last-Modified` validators

Malformed embedded JSON-LD is non-fatal.

## Discovery

Durable crawl starts can automatically seed the frontier from `robots.txt` and bounded sitemap graphs. Sitemap-discovered pages have lower priority than explicit seeds and normal HTML link discovery.

The discovery parser supports:

- XML sitemap URL sets
- nested sitemap indexes
- gzip-compressed sitemaps
- plain-text sitemaps
- RSS
- Atom
- `Sitemap:` directives in `robots.txt`

Untrusted discovery input is bounded. DTD/entity declarations are rejected, gzip output is capped while decompressing, and every fetched discovery document, redirect target, and discovered page URL crosses the central egress policy.

Disable automatic sitemap seeding when needed:

```bash
syndcrawler crawl https://example.com --no-sitemap-discovery
```

## Change detection and adaptive recrawls

For durable records, SyndCrawler stores resource state separately from the latest PageRecord:

- first/last observation time
- last semantic/representation change time
- change count
- last change classification
- HTTP validators
- raw and semantic fingerprints
- current recrawl interval
- `next_fetch_at`

The default deterministic recrawl policy starts new resources at a 24-hour interval, backs stable/304 resources off toward 30 days, and can pull semantically changing resources toward a 15-minute minimum.

Conditional validators are origin-scoped. If a conditional request redirects to another origin, `If-None-Match` and `If-Modified-Since` are stripped before the cross-origin request.

Local recrawl summaries distinguish:

- `not_modified`
- `unchanged`
- `representation_changed`
- `semantic_changed`

## Server / VPS mode

The server profile uses Redis for the frontier and PostgreSQL for durable shared crawl state. A bearer-authenticated FastAPI control plane submits crawls while one or more worker processes execute them.

### Docker Compose

Copy the environment template and replace the required secrets:

```bash
cp .env.example .env
docker compose up -d --build
```

By default the API binds only to `127.0.0.1:8080`, so a reverse proxy can terminate TLS without directly publishing the control plane.

Scale workers independently:

```bash
docker compose up -d --scale worker=3
```

The supplied topology includes:

```text
client / TLS reverse proxy
          |
          v
  FastAPI control plane
          |
    +-----+------------------+
    |                        |
PostgreSQL                 Redis
manifests/results/         frontier/leases/
change state/claims        crawl-wide budget
    |                        |
    +-----------+------------+
                |
          worker fleet
       HTTP + Playwright
```

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for deployment, security, scaling, persistence, API, and update details.

### Control-plane API

All `/v1/...` endpoints require a bearer token by default. `/healthz` is unauthenticated for infrastructure health checks.

Submit:

```bash
curl -X POST http://127.0.0.1:8080/v1/crawls \
  -H "Authorization: Bearer $SYNCRAWLER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://example.com"],"max_pages":100}'
```

Current endpoints:

```text
GET    /healthz
POST   /v1/crawls
GET    /v1/crawls/{crawl_id}
GET    /v1/crawls/{crawl_id}/results?limit=100&offset=0
POST   /v1/crawls/{crawl_id}/pause
POST   /v1/crawls/{crawl_id}/resume
DELETE /v1/crawls/{crawl_id}
```

Lifecycle semantics:

- `active`: initial crawl work is eligible.
- `completed`: initial crawling is finished and scheduled recrawl monitoring remains enabled.
- `paused`: initial work/monitoring are suspended.
- `cancelled`: processing is disabled and Redis frontier state is removed.

### Worker daemon

Run a shared worker directly:

```bash
syndcrawler worker
```

Run one sweep and exit:

```bash
syndcrawler worker --once
```

Each worker handles both active initial crawls and due monitoring recrawls. Multiple workers can safely share the same Redis/PostgreSQL services.

### Distributed initial crawl semantics

The Redis frontier provides:

- exact URL dedupe per crawl
- priority/FIFO ready ordering
- separate delayed scheduling
- host-affine queue policy
- concurrency/delay/blocking limits
- lease expiration and stale-work recovery
- retry timing
- atomic ACK/fail/retry transitions
- crawl-wide first-lease `max_pages` enforcement

The global page budget is enforced inside the same Redis Lua transaction that issues a first lease, so separate workers cannot independently overshoot the crawl limit. Retries do not consume another first-visit budget slot.

### Distributed recrawl semantics

Completed crawls remain monitorable. Due resources are claimed through PostgreSQL rather than reopening their Redis initial-crawl entries.

The claim protocol provides:

- `FOR UPDATE ... SKIP LOCKED` worker distribution
- per-resource claim tokens
- expiring claim leases
- reclaim after worker failure
- atomic claim release + next-schedule update
- conditional HTTP using persisted validators
- browser escalation when rendering is required
- pause/resume/cancel integration

The one-time recrawl-claim schema migration is serialized with a PostgreSQL transaction-scoped advisory lock so simultaneous worker startup does not race in the database catalog.

## Python API

Ephemeral usage:

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

Distributed runtime usage:

```python
import asyncio

from syndcrawler.runtime import ServerRuntime

async def main() -> None:
    runtime = await ServerRuntime.from_urls(
        redis_url="redis://127.0.0.1:6379/0",
        postgres_dsn="postgresql://syndcrawler:password@127.0.0.1/syndcrawler",
    )
    try:
        status = await runtime.submit(["https://example.com"], max_pages=100)
        print(status.crawl_id)
    finally:
        await runtime.close()

asyncio.run(main())
```

## Runtime model

```text
Embedded / local
    CLI or Python
         |
 SQLite frontier + result/change state
         |
    crawler kernel
         |
 Impit HTTP -----> bounded Playwright escalation
         |
   conditional local recrawls

Server / single VPS / worker fleet
       authenticated API
              |
    PostgreSQL + Redis
              |
       shared workers
        /          \
   HTTP fetch     browser escalation
        \          /
      distributed recrawl scheduler
```

## Kernel boundaries

```text
frontier -> policy -> fetch engine -> artifact -> parser/extractor -> validation -> output
              |             |
          page class     network route
```

The `Frontier` and `ResultStore` contracts keep scheduling/storage implementations separate from worker execution. The same `CrawlWorker` persist → discover → ACK ordering is used by the local durable coordinator and server workers.

The adaptive fetch policy currently uses transparent EMA scoring. Recrawl scheduling is deterministic and inspectable. Both intentionally leave room for benchmark-driven contextual-bandit or other learned-policy upgrades later.

## CI coverage

The branch CI currently gates:

- Python 3.12 and 3.13
- Ruff
- full unit/integration suite
- local HTTP crawling
- real Chromium rendering
- real Redis 7 frontier tests
- real PostgreSQL store and recrawl-claim tests
- Redis + PostgreSQL multi-worker server tests
- authenticated FastAPI control-plane tests
- distributed conditional recrawl tests
- Docker Compose configuration
- production Docker image build and CLI smoke test

## Post-v0.1 hardening roadmap

The v0.1 foundation is intentionally not the end of the project. High-value follow-up work includes:

- browser subresource-level egress enforcement for hostile multi-tenant deployments
- persistent/shared adaptive route-policy telemetry
- artifact/blob storage for raw response and rendered evidence retention
- Prometheus/OpenTelemetry-compatible observability
- load/chaos benchmarks and worker-failure testing at much larger frontier sizes
- richer extraction validation and adaptive selector relocation
- pluggable site adapters and extraction schemas
- benchmark-driven engine/parser/policy tuning
- optional richer distributed frontier/service semantics

## Security

See [`SECURITY.md`](SECURITY.md).

Important defaults:

- API authentication is required in server mode unless explicitly disabled.
- the supplied Compose profile does not publish Redis or PostgreSQL ports.
- the API binds to loopback by default.
- public HTTP(S) egress is the default allowed target space.
- safe raw/discovery redirects are revalidated before the next connection.
- browser rendering does not attempt challenge or access-control bypass.

Browser final URLs are validated, but browser subresource-level egress enforcement remains a hardening item before treating a browser worker as a fully isolated hostile multi-tenant sandbox.

## License

Apache-2.0.
