from pathlib import Path

from h2hdb import (
    CoreConfig,
    DatabaseConfig,
    VNextDatabaseAdminFacade,
)

from h2hdb_opds import OPDSConfig, create_app

from .http_client import app_client


async def test_sqlite_epoch_is_opened_read_only_without_legacy_writer_api(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    writable_config = CoreConfig(
        database=DatabaseConfig(
            sql_type="sqlite",
            database=str(database_path),
        )
    )
    report = VNextDatabaseAdminFacade(writable_config).initialize()
    assert report.epoch == 2
    assert report.state == "READY"

    app = create_app(
        OPDSConfig(
            artifact_root=tmp_path,
            public_base_url="http://catalog.example",
            core=writable_config,
        )
    )
    async with app_client(app) as client:
        health = await client.get("/health")
        current_feed = await client.get("/opds/v2/publications")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert current_feed.status_code == 404
    assert current_feed.json() == {"detail": "Catalog revision current not found"}
