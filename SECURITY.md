# Security

## Reporting

Please report security vulnerabilities privately through GitHub's security-advisory flow rather than opening a public issue.

## Egress model

SyndCrawler treats outbound URL handling as a security boundary. Production defaults reject non-HTTP(S) schemes, localhost, loopback, link-local, private, reserved and otherwise non-global IP destinations. Hostnames are resolved before they enter the crawl queue and every returned address is checked.

Private-network crawling is available only through an explicit opt-in intended for local development and controlled intranet use.

The crawler also requires a Crawlee release at or above the security-fixed 1.9 line. Redirect and transport-level destination validation remain the responsibility of the HTTP engine as well as SyndCrawler's own pre-queue validation; defense in depth is intentional.

## Project scope

This project is for normal web crawling, indexing, archival, research and structured-data extraction. It does not include CAPTCHA bypass, credential abuse, access-control bypass, persistence on third-party systems, or automatic expansion into targets outside configured crawl scope.
