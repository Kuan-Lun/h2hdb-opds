from pathlib import Path

import pytest

from h2hdb_opds import OPDSConfig

from .fakes import CatalogFixture, build_catalog_fixture


@pytest.fixture
def catalog_fixture(tmp_path: Path) -> CatalogFixture:
    return build_catalog_fixture(tmp_path)


@pytest.fixture
def opds_config(tmp_path: Path) -> OPDSConfig:
    return OPDSConfig(
        artifact_root=tmp_path,
        public_base_url="http://catalog.example",
    )
