from contextlib import closing
from pathlib import Path

import pytest
from h2hdb import (
    VNextDatabaseAdminFacade,
)

from h2hdb_opds import OPDSConfig, create_app

from .database_support import DatabaseCase
from .http_client import app_client


async def test_epoch_three_is_opened_read_only_without_legacy_writer_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_case: DatabaseCase,
) -> None:
    writable_config = database_case.config
    with closing(VNextDatabaseAdminFacade(writable_config)) as admin:
        report = admin.initialize()
    assert report.epoch == 3
    assert report.schema_version == 9
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

    database_sha256 = database_case.snapshot()
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
    assert database_case.snapshot() == database_sha256


@pytest.mark.parametrize(("schema_version", "state"), ((8, "READY"), (9, "BUILDING")))
async def test_startup_rejects_previous_or_unfinished_schema_without_writes(
    tmp_path: Path,
    schema_version: int,
    state: str,
    database_case: DatabaseCase,
) -> None:
    config = database_case.config
    with closing(VNextDatabaseAdminFacade(config)) as admin:
        admin.initialize()
    # A previous marker and an unfinished conversion are both rejected before
    # opening a catalog reader. This deliberately mutates only a local fixture.
    database_case.execute(
        "UPDATE h2hdb_schema_epoch SET schema_version = %s, state = %s, "
        "ready_at = CASE WHEN %s = 'BUILDING' THEN NULL ELSE ready_at END "
        "WHERE singleton_id = 1",
        (schema_version, state, state),
    )
    library = tmp_path / "current"
    library.mkdir()
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    (coordination / "publication.lock").touch()
    before = database_case.snapshot()
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
    assert database_case.snapshot() == before
