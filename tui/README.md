# SyndCrawler TUI

Interactive OpenTUI operator console for the SyndCrawler Redis/PostgreSQL control plane.

The TUI is deliberately API-only: it never talks directly to Redis or PostgreSQL and never needs the Docker socket.

## Current functional surface

- control-plane / PostgreSQL / Redis health
- recent crawl list
- keyboard selection and detail inspection
- first 100 stored results with HTTP/browser engine visibility
- create a bounded crawl from a seed URL
- optional explicit crawl ID
- configurable max-pages budget
- pause / resume / cancel
- automatic dashboard/detail refresh
- responsive narrow terminal layout
- clear authentication/API errors

## Motion

Motion is presentation-only and was added after the functional operator path passed CI.

- active crawls use a subtle rotating activity glyph
- the header shows a small fetch → parse → store flow animation
- the product name has a short startup reveal
- no API request, selection, form, or lifecycle action depends on an animation frame

Disable motion completely with:

```bash
SYNCRAWLER_TUI_REDUCED_MOTION=true ./tui/run.sh
```

`TERM=dumb` also selects the static fallback automatically.

## Run from source

The source runner loads the repository `.env` by default, so the existing API token and port are reused:

```bash
./tui/run.sh
```

Override the env file with `SYNCRAWLER_TUI_ENV_FILE`.

## Configuration

- `SYNCRAWLER_API_TOKEN` — required for `/v1/*` operations.
- `SYNCRAWLER_TUI_API_URL` — optional full base URL, highest priority.
- `SYNCRAWLER_API_URL` — optional fallback full base URL.
- `SYNCRAWLER_API_BIND` + `SYNCRAWLER_API_PORT` — used to derive the local URL when no full URL is supplied. Wildcard binds are normalized to `127.0.0.1`.
- `SYNCRAWLER_TUI_REDUCED_MOTION` — use static motion fallbacks when true.

## Build a standalone binary

```bash
./tui/build.sh
```

Output:

```text
tui/dist/syndcrawler-tui
```

The Python `syndcrawler tui` launcher looks for `syndcrawler-tui` on `PATH` and also for this source-tree build.

## Keyboard

Dashboard:

- `↑` / `k` — previous crawl
- `↓` / `j` — next crawl
- `Enter` — inspect
- `n` — new crawl
- `r` — refresh
- `?` — help
- `q` / `Esc` — quit

Detail:

- `p` — pause/resume
- `x` — cancel
- `r` — refresh
- `Esc` / `Backspace` — dashboard

New crawl:

- `Tab` — next field
- `Enter` — advance/submit
- `Esc` — cancel

## Design lineage

The implementation uses the React OpenTUI runtime/scaffold patterns from `cmahmud/opentui` and `cmahmud/create-tui`. Responsive terminal breakpoints, motion sequencing, and reduced-motion behavior are informed by `cmahmud/syndrid-tui-studio`.
