# Changelog

All notable project changes will be documented in this file.

The format is based on Keep a Changelog principles. SyndCrawler is currently pre-stable; interfaces may still change between minor releases.

## [0.1.0] - 2026-08-23

### Added

- Crawlee/Impit HTTP crawler with Selectolax/Lexbor parsing.
- Central HTTP(S) egress policy with public-network defaults and explicit private-network opt-in.
- Conservative URL canonicalization and loaded/final URL provenance.
- Deterministic HTTP-to-Playwright browser escalation with bounded DOM stabilization.
- Optional direct/proxy route selection with adaptive outcome telemetry.
- JSON-LD, OpenGraph, canonical URL, title, and link evidence extraction.
- In-memory and SQLite frontier implementations with priority, leases, retries, host-affine politeness, stale-lease recovery, and dedupe.
- Durable local `crawl`, `resume`, `status`, and conditional `recrawl` workflows.
- Bounded sitemap, sitemap-index, gzip/text sitemap, RSS/Atom, and robots `Sitemap:` parsing.
- Safe raw HTTP fetching with bounded bodies and redirect-by-redirect egress validation.
- Raw and semantic fingerprints plus persisted `ETag`/`Last-Modified` validators.
- Adaptive recrawl scheduling with `304`, unchanged, representation-change, and semantic-change classifications.
- Redis distributed frontier with atomic Lua transitions, ready/delayed ordering, multi-worker leases, and atomic crawl-wide initial page budgets.
- PostgreSQL shared manifests, results, lifecycle, resource state, and adaptive schedules.
- Storage-neutral `CrawlWorker` shared by local and distributed coordinators.
- Multi-worker Redis/PostgreSQL `ServerRuntime`.
- Lifecycle states for active, completed/monitored, paused, and cancelled crawls.
- Distributed PostgreSQL recrawl claims with expiring ownership and `SKIP LOCKED` scheduling.
- Bearer-authenticated FastAPI control plane with health, submit, status, results, pause, resume, and cancel endpoints.
- Long-running `syndcrawler worker` and `syndcrawler serve` commands.
- Production Docker image with Chromium and non-root application user.
- Docker Compose single-VPS topology with Redis/PostgreSQL persistence and scalable workers.
- Python 3.12/3.13, Redis, PostgreSQL, browser, server, distributed-recrawl, Compose, and Docker-image CI gates.

### Security

- No CAPTCHA solving, challenge bypass, credential abuse, access-control circumvention, or stealth/fingerprint-evasion functionality.
- API bearer authentication is required by default.
- Redis/PostgreSQL are not externally published by the supplied Compose profile.
- Chromium sandboxing is enabled by default.
- Cross-origin conditional redirects strip origin-specific HTTP validators.

### Known limitations

- Browser final destinations are validated, but full browser subresource-level egress enforcement is not yet implemented.
- Adaptive route-policy telemetry is not yet persisted/shared across distributed worker restarts.
- Raw/rendered artifacts are not yet stored in a dedicated blob/artifact backend.
