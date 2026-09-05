#!/usr/bin/env bash

set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

scripts/check-fast.sh
.venv/bin/python scripts/check-opds-schema-snapshots.py
.venv/bin/python scripts/validate-opds12.py --check-schema
node scripts/validate-opds2.mjs --check-schemas
.venv/bin/python -m pytest -p no:cacheprovider -m "not deep"

artifact_root="$(mktemp -d "${TMPDIR:-/tmp}/h2hdb-opds-check.XXXXXX")"
cleanup() {
    rm -rf -- "$artifact_root"
}
trap cleanup EXIT

.venv/bin/python -m build --no-isolation --outdir "$artifact_root/dist"
wheel="$(find "$artifact_root/dist" -maxdepth 1 -type f -name '*.whl' -print -quit)"
[[ -n "$wheel" ]] || {
    printf 'Wheel build did not produce an artifact\n' >&2
    exit 1
}

UV_CACHE_DIR="$artifact_root/uv-cache" \
    uv venv --python "$repository_root/.venv/bin/python" \
    "$artifact_root/smoke-venv"
smoke_python="$artifact_root/smoke-venv/bin/python"
UV_CACHE_DIR="$artifact_root/uv-cache" \
    uv pip install --python "$smoke_python" --no-deps "$wheel"
development_site="$("$repository_root/.venv/bin/python" -c \
    'import sysconfig; print(sysconfig.get_path("purelib"))')"
smoke_site="$("$smoke_python" -c \
    'import sysconfig; print(sysconfig.get_path("purelib"))')"
"$repository_root/.venv/bin/python" -c \
    'import sys; print(f"import site; site.addsitedir({sys.argv[1]!r})")' \
    "$development_site" > "$smoke_site/development-dependencies.pth"
(
    cd "$artifact_root"
    "$smoke_python" -I - "$repository_root/pyproject.toml" <<'PY'
import asyncio
import sys
import tempfile
import tomllib
from importlib.metadata import version
from pathlib import Path

import httpx

import h2hdb_opds

assert Path(h2hdb_opds.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
expected_version = tomllib.loads(Path(sys.argv[1]).read_text())["project"]["version"]
assert version("h2hdb-opds") == expected_version


async def check_version_api() -> None:
    with tempfile.TemporaryDirectory() as temporary_root:
        root = Path(temporary_root)
        app = h2hdb_opds.create_app(
            h2hdb_opds.OPDSConfig(
                library_root=root / "current",
                coordination_root=root / "coordination",
                public_base_url="http://wheel.example",
            )
        )
        # ASGI transport exercises package metadata without opening a database.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://wheel.example"
        ) as client:
            response = await client.get("/version")
            assert response.status_code == 200
            assert response.json() == {
                "service": "h2hdb-opds",
                "version": expected_version,
            }
            assert response.headers["Cache-Control"] == "no-store"
            schema = await client.get("/openapi.json")
            assert schema.status_code == 200
            assert schema.json()["info"]["version"] == expected_version
            operation_ids = [
                operation["operationId"]
                for path_item in schema.json()["paths"].values()
                for method, operation in path_item.items()
                if method in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
            ]
            assert operation_ids
            assert all(isinstance(value, str) and value.strip() for value in operation_ids)
            assert len(set(operation_ids)) == len(operation_ids)


asyncio.run(check_version_api())
PY
    "$smoke_python" -I -m h2hdb_opds --help >/dev/null
)
