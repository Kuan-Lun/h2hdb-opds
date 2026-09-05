#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "$0")/.." && pwd)"
exec "$repository_root/.venv/bin/python" "$repository_root/scripts/run-checks.py" full
