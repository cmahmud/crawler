# Security

## Reporting

Please report security vulnerabilities privately through GitHub's security-advisory flow rather than opening a public issue.

## Egress model

SyndCrawler treats outbound URL handling as a security boundary. Production defaults reject non-HTTP(S) schemes, localhost, loopback, link-local, private, reserved, and otherwise non-global IP destinations. Hostnames are resolved and returned addresses are checked before crawl work is accepted.

Private-network crawling is available only through an explicit opt-in intended for local development and controlled intranet use. Do not enable it on an internet-facing multi-tenant crawler unless access to those networks is intentional.

Safe raw/discovery HTTP fetching disables implicit redirect traversal and revalidates each redirect destination before connecting. Conditional validators are origin-scoped: `If-None-Match` and `If-Modified-Since` are removed when a redirect crosses origins.

Crawlee is pinned to the security-fixed 1.9+ line. Transport-level validation and SyndCrawler's own egress checks are intentionally defense in depth rather than substitutes for one another.

## Browser boundary

Browser rendering is an extraction tier, not an anti-bot or access-control bypass mechanism. Chromium sandboxing is enabled by default and disabling it requires explicit configuration.

Browser final navigation URLs are checked through the central egress policy. Full subresource-level browser egress enforcement is not yet implemented. Consequently, the current browser worker should not be treated as a fully isolated hostile multi-tenant browser sandbox. Operators accepting arbitrary untrusted crawl targets should isolate browser workers at the network/container layer and keep access to internal/cloud-metadata networks blocked.

## Server/control-plane boundary

The FastAPI control plane requires bearer authentication by default. The supplied Docker Compose profile:

- requires an API bearer token and PostgreSQL password,
- binds the API to loopback by default,
- does not publish Redis or PostgreSQL ports,
- runs the crawler application image as a non-root user,
- persists PostgreSQL and Redis data in named volumes.

Terminate TLS at a trusted reverse proxy before exposing the API beyond localhost. Use long random secrets and do not commit `.env` files containing credentials.

Redis owns transient distributed frontier/leasing state. PostgreSQL owns durable crawl metadata, results, change history, lifecycle, and distributed recrawl claims. Recrawl claims use expiring tokens so a dead worker cannot permanently strand scheduled work.

## Project scope

This project is for normal web crawling, indexing, archival, research, monitoring, and structured-data extraction. It does not include CAPTCHA solving, anti-bot challenge bypass, credential abuse, access-control circumvention, persistence on third-party systems, stealth/fingerprint evasion, or automatic expansion into targets outside configured crawl scope.
