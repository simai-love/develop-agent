#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ ! -d .venv ]]; then
  uv sync --frozen
fi
source .venv/bin/activate
uv sync --frozen --active
if [[ ! -x app/cli/node_modules/.bin/codex ]]; then
  npm ci --prefix app/cli
fi
python -m app.server
