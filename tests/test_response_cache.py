from dataclasses import replace

import pytest
from pydantic import SecretStr

from h2hdb_opds import BasicAuthConfig, OPDSConfig, create_app

from .fakes import ALPHA_ARTIFACT_ID, CatalogFixture, FakeCatalog
from .http_client import app_client


@pytest.mark.parametrize(
    "path",
    [
        "/opds/v1.2/publications/urn:h2h:gallery:1001",
        "/opds/v2/publications/urn:h2h:gallery:1001",
        f"/opds/v1.2/acquisitions/{ALPHA_ARTIFACT_ID}",
        f"/opds/v2/acquisitions/{ALPHA_ARTIFACT_ID}",
        "/media/publications/urn:h2h:gallery:1001/pages/0",
        "/media/publications/urn:h2h:gallery:1001/thumbnail",
    ],
)
async def test_missing_resource_is_not_cached_across_publication(
    path: str,
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    catalog = FakeCatalog(())
    async with app_client(create_app(opds_config, catalog)) as client:
        missing = await client.get(path)
        catalog.add_revision(
            replace(catalog_fixture.catalog.revision, revision=8),
            catalog_fixture.publications,
        )
        published = await client.get(path)

    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    assert "location" not in missing.headers
    assert published.status_code == 200


async def test_missing_file_response_preserves_fresh_range_and_etag_behavior(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    path = f"/opds/v2/acquisitions/{ALPHA_ARTIFACT_ID}?revision=7"
    catalog_fixture.artifact_path.unlink()
    async with app_client(create_app(opds_config, catalog_fixture.catalog)) as client:
        missing = await client.get(path)
        catalog_fixture.artifact_path.write_bytes(catalog_fixture.payload)
        restored = await client.get(path)
        partial = await client.get(path, headers={"Range": "bytes=0-3"})
        unchanged = await client.get(
            path, headers={"If-None-Match": restored.headers["etag"]}
        )

    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    assert restored.status_code == 200
    assert restored.content == catalog_fixture.payload
    assert "cache-control" not in restored.headers
    assert partial.status_code == 206
    assert partial.content == catalog_fixture.payload[:4]
    assert partial.headers["etag"] == restored.headers["etag"]
    assert unchanged.status_code == 304
    assert unchanged.content == b""


async def test_router_errors_keep_status_and_allow_header_without_caching(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    async with app_client(create_app(opds_config, catalog_fixture.catalog)) as client:
        missing = await client.get("/missing-route")
        unsupported = await client.post("/opds/v2")

    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}
    assert missing.headers["cache-control"] == "no-store"
    assert unsupported.status_code == 405
    assert unsupported.headers["allow"] == "GET"
    assert unsupported.headers["cache-control"] == "no-store"


async def test_authentication_metadata_and_challenge_are_not_cached(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={
            "public_base_url": "https://books.example",
            "auth": BasicAuthConfig(username="reader", password=SecretStr("secret")),
        }
    )
    async with app_client(
        create_app(config, catalog_fixture.catalog), base_url="https://testserver"
    ) as client:
        metadata = await client.get("/opds/v2/authentication")
        challenge = await client.get("/opds/v2")

    assert metadata.status_code == 200
    assert metadata.headers["cache-control"] == "no-store"
    assert challenge.status_code == 401
    assert challenge.headers["cache-control"] == "no-store"
    assert challenge.headers["www-authenticate"].startswith("Basic ")
    assert "https://books.example/opds/v2/authentication" in challenge.headers["link"]
