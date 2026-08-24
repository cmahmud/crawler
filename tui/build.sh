#!/usr/bin/env bash
set -Eeuo pipefail

TUI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$TUI_DIR"

if ! command -v bun >/dev/null 2>&1; then
  echo "ERROR: Bun is required to build syndcrawler-tui." >&2
  exit 1
fi

bun install
bun run check
bun run build

echo "Built: $TUI_DIR/dist/syndcrawler-tui"
