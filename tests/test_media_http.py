import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from h2hdb import CatalogRevision, StorageObjectKey

from h2hdb_opds import OPDSConfig, create_app

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client


def _page_path() -> str:
    return "/media/publications/urn:h2h:gallery:1001/pages/0"


async def test_page_and_thumbnail_stream_sealed_logical_extents(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    cover = catalog_fixture.publications[0].cover
    assert cover is not None
    expected_page = catalog_fixture.payload[
        cover.extent.offset : cover.extent.offset + cover.extent.length
    ]

    async with app_client(app) as client:
        page = await client.get(_page_path())
        page_head = await client.head(_page_path())
        partial = await client.get(_page_path(), headers={"Range": "bytes=2-5"})
        thumbnail = await client.get(
            "/media/publications/urn:h2h:gallery:1001/thumbnail"
        )

    assert page.status_code == 200
    assert page.content == expected_page
    assert page.headers["content-type"] == "image/jpeg"
    assert page.headers["etag"] == f'"{cover.sha256}"'
    assert "content-disposition" not in page.headers
    assert page_head.status_code == 200
    assert page_head.content == b""
    assert page_head.headers["content-length"] == str(len(expected_page))
    assert partial.status_code == 206
    assert partial.content == expected_page[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(expected_page)}"
    assert thumbnail.content == catalog_fixture.thumbnail_payload


async def test_page_number_revision_and_storage_codec_fail_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publication = catalog_fixture.publications[0]
    cover = publication.cover
    assert cover is not None
    unknown_codec_cover = replace(
        cover,
        storage_object=replace(
            cover.storage_object,
            key=StorageObjectKey(
                codec="unknown-adapter-v1",
                segments=cover.storage_object.key.segments,
            ),
        ),
    )
    catalog = FakeCatalog((replace(publication, cover=unknown_codec_cover),))
    app = create_app(opds_config, catalog)

    async with app_client(app) as client:
        unknown_codec = await client.get(_page_path())
        missing_page = await client.get(
            "/media/publications/urn:h2h:gallery:1001/pages/1"
        )
        stale = await client.get(f"{_page_path()}?revision=404")

    assert unknown_codec.status_code == 404
    assert unknown_codec.json()["detail"] == "Storage codec is unavailable"
    assert missing_page.status_code == 404
    assert stale.status_code == 404


async def test_page_storage_size_is_checked_before_streaming(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    catalog_fixture.artifact_path.write_bytes(catalog_fixture.payload[:-1])
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        page = await client.get(_page_path())
        page_head = await client.head(_page_path())

    assert page.status_code == 409
    assert page_head.status_code == 409


async def test_open_page_descriptor_survives_atomic_leaf_replacement(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    tmp_path: Path,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    original_lookup = catalog_fixture.catalog.get_catalog_revision
    replacement = b"x" * len(catalog_fixture.payload)
    lookup_count = 0

    def replace_before_head_revalidation(
        revision: int | None = None,
    ) -> CatalogRevision:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 2:
            replacement_path = tmp_path / "replacement.cbz"
            replacement_path.write_bytes(replacement)
            os.replace(replacement_path, catalog_fixture.artifact_path)
        return original_lookup(revision)

    cover = catalog_fixture.publications[0].cover
    assert cover is not None
    expected = catalog_fixture.payload[
        cover.extent.offset : cover.extent.offset + cover.extent.length
    ]
    with patch.object(
        catalog_fixture.catalog,
        "get_catalog_revision",
        side_effect=replace_before_head_revalidation,
    ):
        async with app_client(app) as client:
            response = await client.get(_page_path())

    assert response.status_code == 200
    assert response.content == expected
