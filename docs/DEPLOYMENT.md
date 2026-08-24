# Server deployment

SyndCrawler can run without external services in local SQLite mode. The server profile is for persistent API-driven crawls, one VPS, or multiple workers sharing the same crawl state.

## Topology

```text
client / reverse proxy
        |
        v
  FastAPI control plane
        |
   +----+--------------------+
   |                         |
PostgreSQL                 Redis
manifests/results/         frontier/leasing/
change state/claims        crawl budget
   |                         |
   +-----------+-------------+
               |
        worker processes
        HTTP / browser
```

PostgreSQL is the durable authority for manifests, lifecycle, latest results, fingerprints, validators, adaptive recrawl schedules, and distributed recrawl claims. Redis owns the initial crawl frontier, host-affine leasing, retry timing, politeness state, and atomic crawl-wide page budget.

Workers are stateless apart from their in-flight leases. Multiple workers can consume the same crawl safely.

## Quick start with Docker Compose

Copy the example environment file and replace both required secrets:

```bash
cp .env.example .env
```

At minimum, set strong unique values for:

```text
SYNCRAWLER_POSTGRES_PASSWORD
SYNCRAWLER_API_TOKEN
```

Then build and start the stack:

```bash
docker compose up -d --build
```

The default stack starts:

- PostgreSQL 16 with a persistent volume
- Redis 7 with AOF persistence
- one API container
- one worker container

The API is bound to `127.0.0.1:8080` by default. This is deliberate: terminate TLS at a reverse proxy such as Caddy, nginx, or another trusted ingress instead of exposing the control plane directly over plain HTTP.

Check container state:

```bash
docker compose ps
```

Scale workers without changing crawl semantics:

```bash
docker compose up -d --scale worker=3
```

The Redis leasing protocol and PostgreSQL recrawl claims prevent independent workers from taking the same unit of work, and the crawl-wide initial page budget is enforced atomically in Redis.

## API authentication

All `/v1/...` routes require a bearer token by default. `/healthz` is intentionally unauthenticated for container and reverse-proxy health checks.

Example request:

```bash
curl -X POST http://127.0.0.1:8080/v1/crawls \
  -H "Authorization: Bearer $SYNCRAWLER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://example.com"],"max_pages":100}'
```

Do not place Redis or PostgreSQL ports on a public interface. The supplied Compose profile does not publish them.

## Control-plane endpoints

### Submit a crawl

```http
POST /v1/crawls
```

Example body:

```json
{
  "urls": ["https://example.com"],
  "follow_links": true,
  "max_pages": 100
}
```

A caller may also supply a stable `crawl_id`. Replaying an identical submission is repair-safe; trying to reuse the ID with a different crawl definition returns a conflict.

### Status

```http
GET /v1/crawls/{crawl_id}
```

Lifecycle values:

- `active`: initial crawl work is eligible for workers
- `completed`: the initial crawl is done and scheduled monitoring/recrawls remain enabled
- `paused`: neither initial work nor scheduled monitoring is processed
- `cancelled`: the crawl is disabled and its Redis frontier state is removed

### Results

```http
GET /v1/crawls/{crawl_id}/results?limit=100&offset=0
```

Results are paginated and represent the latest stored record for each request URL.

### Pause / resume / cancel

```http
POST   /v1/crawls/{crawl_id}/pause
POST   /v1/crawls/{crawl_id}/resume
DELETE /v1/crawls/{crawl_id}
```

Pausing a completed crawl pauses monitoring. Resuming it restores the `completed` monitoring state. A cancelled crawl cannot be resumed.

## Distributed recrawls

Each successfully fingerprinted resource stores an adaptive `next_fetch_at` deadline. Once an initial crawl reaches `completed`, workers also process due recrawls.

Due-resource ownership is coordinated through PostgreSQL leases:

- workers claim due rows using row locking with `SKIP LOCKED`
- every claim has a unique token and expiration time
- a worker crash leaves no permanent lock; another worker can reclaim the resource after lease expiry
- successful processing atomically removes the claim and advances `next_fetch_at`
- `ETag` and `Last-Modified` validators are reused for conditional requests
- `304 Not Modified` avoids replacing the stored PageRecord
- semantic and representation changes update the resource history and next interval
- browser escalation remains available for resources requiring client rendering

Tune the worker recrawl batch and lease duration with:

```text
SYNCRAWLER_RECRAWL_BATCH_SIZE
SYNCRAWLER_RECRAWL_LEASE_SECONDS
```

## Browser workers

The production image includes Playwright Chromium. Browser escalation remains disabled unless:

```text
SYNCRAWLER_BROWSER_ENABLED=true
```

Chromium sandboxing is enabled by default. Do not disable it merely for convenience. If a specific trusted container host cannot provide the required sandbox facilities, the setting can be changed explicitly with:

```text
SYNCRAWLER_BROWSER_CHROMIUM_SANDBOX=false
```

Browser rendering is for extraction, not challenge/CAPTCHA or access-control bypass.

## Networking and SSRF policy

Public HTTP(S) destinations are the default allowed crawl targets. Localhost, private, link-local, reserved, and other non-global destinations are rejected unless private-network crawling is explicitly enabled.

Keep this default for an internet-facing crawler service:

```text
SYNCRAWLER_ALLOW_PRIVATE_NETWORKS=false
```

The safe raw HTTP path manually validates redirect destinations before making the next connection. Browser final URLs are also validated. Browser subresource-level egress enforcement is still a hardening item for hostile multi-tenant deployments, so do not treat the current browser worker as a fully isolated untrusted-tenant browser sandbox.

## Persistence and backups

The supplied Compose profile creates two named volumes:

```text
postgres-data
redis-data
```

PostgreSQL contains the durable crawl/result/change record and should be included in normal database backups. Redis AOF makes frontier state resilient to ordinary service restarts, but PostgreSQL remains the durable business-data authority.

A typical backup policy should include regular PostgreSQL dumps or physical backups and tested restore procedures. Do not rely on container lifetime as persistence.

## Running without Compose

The API and worker can also be run directly if Redis and PostgreSQL are already available.

Required environment variables:

```text
SYNCRAWLER_REDIS_URL
SYNCRAWLER_POSTGRES_DSN
SYNCRAWLER_API_TOKEN    # API process only
```

Start the API:

```bash
syndcrawler serve --host 127.0.0.1 --port 8080
```

Start a worker:

```bash
syndcrawler worker
```

Run exactly one worker sweep for orchestration/debugging:

```bash
syndcrawler worker --once
```

## Updating

For a source checkout deployment:

```bash
git pull
docker compose build
docker compose up -d
```

Database schema initialization is idempotent. The distributed recrawl claim-table migration serializes its one-time DDL initialization so simultaneous worker startups do not race in PostgreSQL's system catalog.
