#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY=""
for candidate in python3.12 python3.13 python3.11 python3.10 python3.14 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] < (3, 15)))'; then
        PY="$candidate"
        break
    fi
done
if [[ -z "$PY" ]]; then
    echo "Установите Python 3.10–3.14 (рекомендуется 3.12) и повторите команду." >&2
    exit 1
fi
if [[ ! -x "$ROOT/.venv312/bin/python" ]]; then
    "$PY" -m venv "$ROOT/.venv312"
fi
"$ROOT/.venv312/bin/python" -m pip install -r "$ROOT/requirements.txt"
exec "$ROOT/.venv312/bin/python" "$ROOT/scripts/configure.py"
