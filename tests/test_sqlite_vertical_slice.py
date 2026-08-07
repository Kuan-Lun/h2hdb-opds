from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from h2hdb import (
    H2HDB,
    CatalogArtifact,
    CatalogPublicationSelection,
    CatalogSnapshot,
    CoreConfig,
    DatabaseConfig,
    GallerySourceFile,
    GallerySourceRecord,
    GalleryTag,
)

from h2hdb_opds import OPDSConfig, create_app

from .http_client import app_client


async def test_sqlite_catalog_is_opened_read_only_and_served(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    payload = b"sqlite vertical slice"
    artifact_path = tmp_path / "publication.cbz"
    artifact_path.write_bytes(payload)
    timestamp = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)
    writable_config = CoreConfig(
        database=DatabaseConfig(
            sql_type="sqlite",
            database=str(database_path),
        )
    )
    writer = H2HDB(writable_config)
    writer.migrate()
    ingest_turn = writer.claim_gallery_ingest(
        lease_seconds=30,
        periodic_scan=False,
    )
    assert ingest_turn is not None
    published_result = writer.publish_snapshot(
        CatalogSnapshot(
            galleries=(
                GallerySourceRecord(
                    gallery_name="sqlite-gallery",
                    gid=2001,
                    title="SQLite Publication",
                    comment="Published by the core projection",
                    upload_account="integration-test",
                    upload_time=timestamp,
                    download_time=timestamp,
                    modified_time=timestamp,
                    tags=(GalleryTag(name="language", value="english"),),
                    files=(
                        GallerySourceFile(
                            name="001.jpg",
                            size_bytes=len(payload),
                            sha256=sha256(payload).hexdigest(),
                        ),
                    ),
                    source_manifest_sha256=sha256(b"sqlite manifest").hexdigest(),
                    content_sha256=sha256(payload).hexdigest(),
                ),
            ),
            selections=(
                CatalogPublicationSelection(
                    source_gallery_name="sqlite-gallery",
                    artifacts=(
                        CatalogArtifact(
                            artifact_id="sqlite-artifact",
                            name="publication.cbz",
                            location=artifact_path,
                            media_type="application/vnd.comicbook+zip",
                            size_bytes=len(payload),
                            sha256=sha256(payload).hexdigest(),
                            modified_at=timestamp,
                        ),
                    ),
                ),
            ),
        ),
        ingest_turn=ingest_turn,
    )
    assert writer.complete_gallery_ingest(ingest_turn)
    next_ingest_turn = writer.claim_gallery_ingest(
        lease_seconds=30,
        periodic_scan=True,
    )
    assert next_ingest_turn is not None
    current_result = writer.publish_snapshot(
        CatalogSnapshot(galleries=(), selections=()),
        ingest_turn=next_ingest_turn,
    )
    assert writer.complete_gallery_ingest(next_ingest_turn)
    published_revision = published_result.revision
    current_revision = current_result.revision

    app = create_app(
        OPDSConfig(
            artifact_root=tmp_path,
            public_base_url="http://catalog.example",
            core=writable_config,
        )
    )
    async with app_client(app) as client:
        current_feed = await client.get("/opds/v2/publications")
        historical_feed = await client.get(
            f"/opds/v2/publications?revision={published_revision.revision}"
        )
        acquisition = await client.get(
            "/opds/v2/acquisitions/sqlite-artifact"
            f"?revision={published_revision.revision}"
        )

    assert current_feed.status_code == 200
    assert current_feed.json()["metadata"]["numberOfItems"] == 0
    assert f"revision={current_revision.revision}" in (
        current_feed.json()["links"][0]["href"]
    )
    assert historical_feed.status_code == 200
    assert historical_feed.json()["publications"][0]["metadata"]["identifier"] == (
        "urn:h2h:gallery:2001"
    )
    assert acquisition.status_code == 200
    assert acquisition.content == payload
