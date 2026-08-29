import fcntl
import os
from pathlib import Path

import pytest

from h2hdb_opds import OPDSConfig, create_app

from .fakes import CatalogFixture
from .http_client import app_client


async def test_activation_marker_fail_closes_every_catalog_reader(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    marker = opds_config.coordination_root / "ACTIVATING"
    marker.write_text("malformed interrupted marker\n", encoding="utf-8")
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        health = await client.get("/health")
        navigation = await client.get("/opds/v2")
        feed = await client.get("/opds/v2/publications")
        publication = await client.get("/opds/v2/publications/publication-alpha")
        acquisition = await client.get("/opds/v2/acquisitions/artifact-alpha")

    assert health.status_code == 200
    for response in (navigation, feed, publication, acquisition):
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.headers["cache-control"] == "no-store"


async def test_nonregular_activation_markers_also_fail_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside-marker"
    target.write_text("not trusted\n", encoding="utf-8")
    (opds_config.coordination_root / "ACTIVATING").symlink_to(target)
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        linked_response = await client.get("/opds/v2/publications")
        (opds_config.coordination_root / "ACTIVATING").unlink()
        (opds_config.coordination_root / "ACTIVATING").mkdir()
        directory_response = await client.get("/opds/v2/publications")

    assert linked_response.status_code == 503
    assert directory_response.status_code == 503


async def test_exclusive_activation_lock_returns_503_then_recovers(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    lock_descriptor = os.open(
        opds_config.coordination_root / "publication.lock",
        os.O_RDWR | os.O_CLOEXEC,
    )
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    app = create_app(opds_config, catalog_fixture.catalog)
    try:
        async with app_client(app) as client:
            unavailable = await client.get("/opds/v2/publications")
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            available = await client.get("/opds/v2/publications")
    finally:
        os.close(lock_descriptor)

    assert unavailable.status_code == 503
    assert available.status_code == 200


async def test_fifo_publication_lock_fails_startup_without_blocking(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    lock_path = opds_config.coordination_root / "publication.lock"
    lock_path.unlink()
    os.mkfifo(lock_path)
    app = create_app(opds_config, catalog_fixture.catalog)

    with pytest.raises(RuntimeError, match=r"regular publication\.lock"):
        async with app_client(app):
            pass
