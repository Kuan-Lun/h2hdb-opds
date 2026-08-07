#!/usr/bin/env bash
# Recreate the repository-local environment without a lockfile.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

uv venv --clear --python 3.14

EDITABLES=(-e ".[dev]")
if [[ -f ../h2hdb.clone/pyproject.toml ]]; then
    EDITABLES+=(-e ../h2hdb.clone)
fi
uv pip install --python .venv/bin/python "${EDITABLES[@]}"
