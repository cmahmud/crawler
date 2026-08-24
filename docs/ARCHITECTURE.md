# Architecture

SyndCrawler separates crawl intent from execution infrastructure. A crawl definition should behave the same in embedded, local, VPS, and clustered deployments.

## Data flow

```text
seed/discovery
    |
    v
frontier -- queue policy / lease / dedupe / recrawl
    |
    v
policy -- page class + historical outcomes
    |
    +--> engine: HTTP / browser
    +--> route: direct / proxy pool
    |
    v
fetch artifact
    |
    +--> network/embedded structured data
    +--> DOM parser
    +--> adaptive extraction
    +--> adapter
    +--> AI repair (last resort)
    |
    v
validation -> normalized record + provenance
```

## Runtime profiles

### Embedded

Imported as a Python library. No external services are required.

### Local

Single process with local persistence. Intended for development, one-off crawls, and resumable desktop jobs.

### Server

API/control plane plus Redis-backed frontier and independently scalable HTTP/browser workers. Intended for a single VPS.

### Cluster

The frontier becomes a separate service with host-affine queues and distributed leases. Workers remain stateless aside from bounded sessions/caches.

## Frontier contract

The frontier owns URL uniqueness within a crawl, priority, availability time, queue key, lease ownership, retry/refetch scheduling, and per-queue politeness. The initial `MemoryFrontier` models these semantics without external infrastructure.

Long-term implementations should remain compatible with crawler-commons URL Frontier concepts: crawl namespaces, queue keys, in-transit leases, batched discovery, per-queue delays/blocks/limits, and scheduled refetch.

## Policy contract

The policy learner receives a context such as `domain + page class` and chooses among available fetch actions. An action is currently an engine/network-route pair. Outcome observations include success, extraction quality, latency, and cost.

The initial implementation uses exponential moving averages and deterministic exploration. This is intentionally inspectable and provides a benchmark baseline before more complex contextual-bandit approaches are introduced.

## Egress boundary

All outbound destinations must eventually pass through one egress policy, including initial targets, discovered URLs, redirects, sitemap entries, browser navigations/subresources where enforceable, and proxy endpoints. Local/private network access is opt-in.

Pre-queue DNS validation alone is not sufficient protection against DNS rebinding, so transport implementations must also enforce destination policy at connection/redirect time. Crawlee is pinned above its 2026 URL-validation security fixes as defense in depth.

## Site-specific code

Retailer, marketplace, CMS, and other target-specific extraction belongs outside the generic kernel. Site adapters may declare discovery/extraction hints, but they must not own frontier, transport, proxy, or deployment policy.
