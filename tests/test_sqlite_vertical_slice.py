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
    assert report.schema_version == 7
    assert report.state == "READY"

    def forbid_full_audit(_admin: VNextDatabaseAdminFacade) -> None:
        pytest.fail("reader startup must not run a full database audit")

    monkeypatch.setattr(VNextDatabaseAdminFacade, "check", forbid_full_audit)

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
