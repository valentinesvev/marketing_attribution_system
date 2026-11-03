#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$ROOT/.venv312/bin/python" || ! -f "$ROOT/src/.env" ]]; then
    echo "Сначала выполните: bash \"$ROOT/scripts/setup.sh\"" >&2
    exit 1
fi
cd "$ROOT/src"
exec "$ROOT/.venv312/bin/python" -m bot.main
