#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TUI_DIR="$ROOT_DIR/tui"
ENV_FILE="${SYNCRAWLER_TUI_ENV_FILE:-$ROOT_DIR/.env}"

if ! command -v bun >/dev/null 2>&1; then
  echo "ERROR: Bun is required to run the SyndCrawler TUI from source." >&2
  echo "Install Bun, then rerun: $TUI_DIR/run.sh" >&2
  exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

cd "$TUI_DIR"
if [[ ! -d node_modules ]]; then
  bun install
fi
exec bun run src/index.tsx
