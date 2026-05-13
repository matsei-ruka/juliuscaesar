#!/usr/bin/env bash
set -euo pipefail

ROOT="${JC_FRAMEWORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if [[ -n "${PYTHON:-}" ]]; then
    PYTHON_BIN="$PYTHON"
elif [[ -x "${XDG_DATA_HOME:-$HOME/.local/share}/juliuscaesar/venv/bin/python" ]]; then
    PYTHON_BIN="${XDG_DATA_HOME:-$HOME/.local/share}/juliuscaesar/venv/bin/python"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi
export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON_BIN" -m release_updates.release_2026_05_13_01 "$@"
