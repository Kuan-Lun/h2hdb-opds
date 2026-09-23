import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest
from h2hdb import (
    CoreConfig,
    DatabaseConfig,
    VNextDatabaseAdminFacade,
)

from h2hdb_opds import OPDSConfig, create_app

from .http_client import app_client


async def test_sqlite_epoch_three_is_opened_read_only_without_legacy_writer_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    writable_config = CoreConfig(
        database=DatabaseConfig(
            sql_type="sqlite",
            database=str(database_path),
        )
    )
    with closing(VNextDatabaseAdminFacade(writable_config)) as admin:
        report = admin.initialize()
    assert report.epoch == 3
    assert report.schema_version == 8
    assert report.state == "READY"

    def forbid_writer_or_full_audit(_admin: VNextDatabaseAdminFacade) -> None:
        pytest.fail("reader startup must only use read-only readiness admission")

    monkeypatch.setattr(VNextDatabaseAdminFacade, "check", forbid_writer_or_full_audit)
    monkeypatch.setattr(
        VNextDatabaseAdminFacade, "initialize", forbid_writer_or_full_audit
    )

    library_root = tmp_path / "current"
    library_root.mkdir()
    coordination_root = tmp_path / "coordination"
    coordination_root.mkdir()
    (coordination_root / "publication.lock").touch()

    database_sha256 = sha256(database_path.read_bytes()).digest()
    app = create_app(
        OPDSConfig(
            library_root=library_root,
            coordination_root=coordination_root,
            public_base_url="http://catalog.example",
            core=writable_config,
        )
    )
    async with app_client(app) as client:
        health = await client.get("/health")
        current_feed = await client.get("/opds/v2/publications")
        atom_feed = await client.get("/opds/v1.2/publications")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert current_feed.status_code == 404
    assert current_feed.json() == {"detail": "Catalog revision current not found"}
    assert atom_feed.status_code == 404
    assert atom_feed.json() == {"detail": "Catalog revision current not found"}
    assert sha256(database_path.read_bytes()).digest() == database_sha256


@pytest.mark.parametrize(("schema_version", "state"), ((7, "READY"), (8, "BUILDING")))
async def test_sqlite_startup_rejects_previous_or_unfinished_schema_without_writes(
    tmp_path: Path,
    schema_version: int,
    state: str,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    config = CoreConfig(
        database=DatabaseConfig(sql_type="sqlite", database=str(database))
    )
    with closing(VNextDatabaseAdminFacade(config)) as admin:
        admin.initialize()
    # A previous marker and an unfinished conversion are both rejected before
    # opening a catalog reader. This deliberately mutates only a local fixture.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE h2hdb_schema_epoch SET schema_version = ?, state = ?, "
            "ready_at = CASE WHEN ? = 'BUILDING' THEN NULL ELSE ready_at END "
            "WHERE singleton_id = 1",
            (schema_version, state, state),
        )
    library = tmp_path / "current"
    library.mkdir()
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    (coordination / "publication.lock").touch()
    before = sha256(database.read_bytes()).digest()
    app = create_app(
        OPDSConfig(
            library_root=library,
            coordination_root=coordination,
            public_base_url="http://catalog.example",
            core=config,
        )
    )
    with pytest.raises(RuntimeError, match=r"(?i)(schema|building)"):
        async with app_client(app):
            pytest.fail("an unsupported marker must not start the reader")
    assert sha256(database.read_bytes()).digest() == before
