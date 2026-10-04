from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sys
from contextlib import closing
from pathlib import Path

import h2hdb
import pytest
from h2hdb import VNextDatabaseAdminFacade

from benchmarks.opds_sqlite_scalability import load_core_fixture
from h2hdb_opds import OPDSConfig, create_app

from .catalog_fixture import _prepare_core_smoke_fixture
from .catalog_http_oracle import EXPECTED_OPERATION_ORDER, SMOKE_EXPECTED_BODY_SHA256
from .database_support import DatabaseCase
from .http_client import app_client


def _seed_public_catalog(database_case: DatabaseCase) -> None:
    configured = os.environ.get("H2HDB_CORE_REPOSITORY")
    if configured is not None:
        if not configured.strip():
            pytest.fail("H2HDB_CORE_REPOSITORY must name a core checkout")
        source = Path(configured).expanduser().resolve(strict=True)
    elif h2hdb.__file__ is not None:
        source = Path(h2hdb.__file__).resolve(strict=True).parents[2]
    else:
        pytest.fail("H2HDB_CORE_REPOSITORY must name the portable fixture source")
    fixture_path = source / "benchmarks" / "sqlite_catalog_scalability.py"
    if not fixture_path.is_file():
        pytest.fail(
            "Required native database HTTP integration cannot be skipped; provide "
            "H2HDB_CORE_REPOSITORY containing seed_catalog_fixture"
        )
    spec = importlib.util.spec_from_file_location("_core_catalog_fixture", fixture_path)
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fixture
    try:
        spec.loader.exec_module(fixture)
        expected, _tables = fixture.seed_catalog_fixture(
            database_case.config,
            publication_count=165,
            seed=fixture.DEFAULT_SEED,
        )
    finally:
        del sys.modules[spec.name]
    assert expected["publication_count"] == 165
    assert expected["search"]["publication_count"] == 33


def _prepare_public_catalog(database_case: DatabaseCase, root: Path) -> None:
    if database_case.config.database.sql_type == "sqlite":
        fixture_root = root / "verified-core-fixture"
        fixture_root.mkdir()
        database, receipt = _prepare_core_smoke_fixture(fixture_root)
        authority = load_core_fixture(database, receipt)
        assert authority.publication_count == 165
        shutil.copyfile(database, database_case.config.database.database)
    else:
        with closing(VNextDatabaseAdminFacade(database_case.config)) as admin:
            assert admin.initialize().state == "READY"
        _seed_public_catalog(database_case)
    with closing(VNextDatabaseAdminFacade(database_case.config)) as admin:
        assert admin.check().state == "READY"


async def test_published_catalog_native_database_exact_http_oracle(
    database_case: DatabaseCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_public_catalog(database_case, tmp_path)

    def forbid_full_audit(_admin: VNextDatabaseAdminFacade) -> None:
        pytest.fail("published reader startup must use readiness admission")

    monkeypatch.setattr(VNextDatabaseAdminFacade, "check", forbid_full_audit)
    monkeypatch.setattr(VNextDatabaseAdminFacade, "initialize", forbid_full_audit)
    library = tmp_path / "empty-library"
    library.mkdir()
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    (coordination / "publication.lock").touch()
    application = create_app(
        OPDSConfig(
            library_root=library,
            coordination_root=coordination,
            public_base_url="http://benchmark.invalid",
            core=database_case.config,
            title="H2HDB OPDS SQL scalability benchmark",
            default_page_size=32,
            maximum_page_size=128,
        )
    )
    before = database_case.snapshot()
    hashes: dict[str, str] = {}
    async with app_client(application, base_url="http://benchmark.invalid") as client:

        async def record(name: str, url: str) -> str | None:
            response = await client.get(url)
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/opds+json"
            assert response.headers["content-length"] == str(len(response.content))
            hashes[name] = hashlib.sha256(response.content).hexdigest()
            links = response.json().get("links", [])
            next_links = [link["href"] for link in links if link.get("rel") == "next"]
            assert len(next_links) <= 1
            return str(next_links[0]) if next_links else None

        for family, url in (
            ("discovery", "/opds/v2/publications?limit=32"),
            ("nonempty_search", "/opds/v2/search?query=needle&limit=32"),
        ):
            next_url = await record(f"{family}_first_page", url)
            assert next_url is not None
            await record(f"{family}_cursor_page", next_url)
        for facet in ("language", "subject", "contributor"):
            await record(
                f"facet_{facet}_first_page",
                f"/opds/v2/facets/{facet}?query=needle&limit=128",
            )
    assert tuple(hashes) == EXPECTED_OPERATION_ORDER
    assert hashes == SMOKE_EXPECTED_BODY_SHA256
    assert database_case.snapshot() == before
