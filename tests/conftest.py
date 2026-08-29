from pathlib import Path

import pytest

from h2hdb_opds import OPDSConfig

from .fakes import CatalogFixture, build_catalog_fixture


@pytest.fixture
def catalog_fixture(tmp_path: Path) -> CatalogFixture:
    return build_catalog_fixture(tmp_path)


@pytest.fixture
def opds_config(tmp_path: Path) -> OPDSConfig:
    library_root = tmp_path / "current"
    library_root.mkdir(exist_ok=True)
    coordination_root = tmp_path / "coordination"
    coordination_root.mkdir(exist_ok=True)
    (coordination_root / "publication.lock").touch()
    return OPDSConfig(
        library_root=library_root,
        coordination_root=coordination_root,
        public_base_url="http://catalog.example",
    )
