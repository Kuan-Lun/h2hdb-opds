import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

from h2hdb_opds import OPDSConfig, create_app

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client


async def test_full_get_and_head_use_published_artifact_metadata(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v2/acquisitions/artifact-alpha"

    async with app_client(app) as client:
        response = await client.get(path)
        head = await client.head(path)

    expected_etag = f'"{catalog_fixture.artifact.sha256}"'
    assert response.status_code == 200
    assert response.content == catalog_fixture.payload
    assert response.headers["content-type"] == catalog_fixture.artifact.media_type
    assert response.headers["content-length"] == str(len(catalog_fixture.payload))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["etag"] == expected_etag
    assert response.headers["last-modified"] == "Wed, 05 Aug 2026 12:30:45 GMT"
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == response.headers["content-length"]
    assert head.headers["etag"] == expected_etag
    assert catalog_fixture.catalog.artifact_revisions == [
        catalog_fixture.catalog.revision,
        catalog_fixture.catalog.revision,
    ]


async def test_download_name_uses_published_name_not_storage_path_and_is_safe(
    catalog_fixture: CatalogFixture,
    tmp_path: Path,
    opds_config: OPDSConfig,
) -> None:
    payload = catalog_fixture.payload
    storage_name = f"{catalog_fixture.artifact.sha256}.cbz"
    storage_path = tmp_path / storage_name
    storage_path.write_bytes(payload)
    published_name = '../../ignored\r\nX-Evil: yes\\友善 Gallery "Title".cbz'
    artifact = replace(
        catalog_fixture.artifact,
        name=published_name,
        location=storage_path,
    )
    publication = replace(catalog_fixture.publications[0], artifacts=(artifact,))
    catalog = FakeCatalog((publication,))
    app = create_app(opds_config, catalog)

    async with app_client(app) as client:
        response = await client.get("/opds/v2/acquisitions/artifact-alpha")

    disposition = response.headers["content-disposition"]
    encoded_published_name = quote('友善 Gallery "Title".cbz', safe="")
    assert response.status_code == 200
    assert disposition == (
        'attachment; filename="Gallery Title.cbz"; '
        f"filename*=UTF-8''{encoded_published_name}"
    )
    assert storage_name not in disposition
    assert "ignored" not in disposition
    assert "X-Evil" not in disposition
    assert "\r" not in disposition
    assert "\n" not in disposition


async def test_single_closed_open_suffix_ranges_and_head_ignores_range(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v2/acquisitions/artifact-alpha"

    async with app_client(app) as client:
        closed = await client.get(path, headers={"Range": "bytes=2-6"})
        open_range = await client.get(path, headers={"Range": "bytes=10-"})
        suffix = await client.get(path, headers={"Range": "bytes=-5"})
        optional_whitespace = await client.get(
            path,
            headers={"Range": "bytes= 2-6"},
        )
        head = await client.head(path, headers={"Range": "bytes=3-8"})

    assert closed.status_code == 206
    assert closed.content == catalog_fixture.payload[2:7]
    assert closed.headers["content-range"] == (
        f"bytes 2-6/{len(catalog_fixture.payload)}"
    )
    assert closed.headers["content-length"] == "5"
    assert open_range.status_code == 206
    assert open_range.content == catalog_fixture.payload[10:]
    assert suffix.status_code == 206
    assert suffix.content == catalog_fixture.payload[-5:]
    assert optional_whitespace.status_code == 206
    assert optional_whitespace.content == catalog_fixture.payload[2:7]
    assert head.status_code == 200
    assert head.content == b""
    assert "content-range" not in head.headers
    assert head.headers["content-length"] == str(len(catalog_fixture.payload))


async def test_unsatisfiable_and_multiple_ranges_return_416(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v2/acquisitions/artifact-alpha"
    size = len(catalog_fixture.payload)

    async with app_client(app) as client:
        beyond_end = await client.get(
            path,
            headers={"Range": f"bytes={size}-"},
        )
        backwards = await client.get(path, headers={"Range": "bytes=9-3"})
        multiple = await client.get(path, headers={"Range": "bytes=0-1,4-5"})
        malformed = await client.get(path, headers={"Range": "not-a-range"})
        unsupported_unit = await client.get(path, headers={"Range": "items=0-1"})

    for response in (beyond_end, backwards, multiple, malformed):
        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{size}"
        assert response.headers["accept-ranges"] == "bytes"
    assert unsupported_unit.status_code == 200
    assert unsupported_unit.content == catalog_fixture.payload
    assert "content-range" not in unsupported_unit.headers


async def test_if_range_controls_whether_range_is_applied(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v2/acquisitions/artifact-alpha"
    etag = f'"{catalog_fixture.artifact.sha256}"'

    async with app_client(app) as client:
        matching = await client.get(
            path,
            headers={"Range": "bytes=0-3", "If-Range": etag},
        )
        mismatching = await client.get(
            path,
            headers={"Range": "bytes=0-3", "If-Range": '"different"'},
        )
        weak = await client.get(
            path,
            headers={"Range": "bytes=0-3", "If-Range": f"W/{etag}"},
        )

    assert matching.status_code == 206
    assert matching.content == catalog_fixture.payload[:4]
    assert mismatching.status_code == 200
    assert mismatching.content == catalog_fixture.payload
    assert "content-range" not in mismatching.headers
    assert weak.status_code == 200


async def test_conditional_requests_prioritize_if_none_match(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v2/acquisitions/artifact-alpha"
    etag = f'"{catalog_fixture.artifact.sha256}"'
    future = "Thu, 06 Aug 2026 12:30:45 GMT"

    async with app_client(app) as client:
        exact_etag = await client.get(path, headers={"If-None-Match": etag})
        weak_etag = await client.get(path, headers={"If-None-Match": f"W/{etag}"})
        modified_since = await client.get(
            path,
            headers={"If-Modified-Since": future},
        )
        precedence = await client.get(
            path,
            headers={
                "If-None-Match": '"different"',
                "If-Modified-Since": future,
            },
        )

    assert exact_etag.status_code == 304
    assert exact_etag.content == b""
    assert exact_etag.headers["etag"] == etag
    assert weak_etag.status_code == 304
    assert modified_since.status_code == 304
    assert precedence.status_code == 200
    assert precedence.content == catalog_fixture.payload


async def test_if_match_and_if_unmodified_since_follow_precondition_order(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v2/acquisitions/artifact-alpha"
    etag = f'"{catalog_fixture.artifact.sha256}"'
    before = "Tue, 04 Aug 2026 12:30:45 GMT"

    async with app_client(app) as client:
        matching = await client.get(path, headers={"If-Match": etag})
        wildcard = await client.get(path, headers={"If-Match": "*"})
        mismatching = await client.get(path, headers={"If-Match": '"other"'})
        weak = await client.get(path, headers={"If-Match": f"W/{etag}"})
        unmodified_failure = await client.get(
            path,
            headers={"If-Unmodified-Since": before},
        )
        if_match_precedence = await client.get(
            path,
            headers={"If-Match": etag, "If-Unmodified-Since": before},
        )

    assert matching.status_code == 200
    assert wildcard.status_code == 200
    assert mismatching.status_code == 412
    assert weak.status_code == 412
    assert unmodified_failure.status_code == 412
    assert if_match_precedence.status_code == 200


async def test_if_range_http_date_requires_exact_last_modified_match(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v2/acquisitions/artifact-alpha"
    exact = "Wed, 05 Aug 2026 12:30:45 GMT"
    later = "Thu, 06 Aug 2026 12:30:45 GMT"

    async with app_client(app) as client:
        exact_response = await client.get(
            path,
            headers={"Range": "bytes=0-3", "If-Range": exact},
        )
        later_response = await client.get(
            path,
            headers={"Range": "bytes=0-3", "If-Range": later},
        )

    assert exact_response.status_code == 206
    assert exact_response.content == catalog_fixture.payload[:4]
    assert later_response.status_code == 200
    assert later_response.content == catalog_fixture.payload


async def test_artifact_must_be_beneath_root_and_must_not_be_a_symlink(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    secure_config = opds_config.model_copy(update={"artifact_root": trusted_root})
    outside_root = tmp_path / "outside.cbz"
    outside_root.write_bytes(catalog_fixture.payload)
    symlink = trusted_root / "linked.cbz"
    symlink.symlink_to(outside_root)
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    nested_payload = outside_directory / "nested.cbz"
    nested_payload.write_bytes(catalog_fixture.payload)
    linked_directory = trusted_root / "linked-directory"
    linked_directory.symlink_to(outside_directory, target_is_directory=True)
    fifo = trusted_root / "artifact.fifo"
    os.mkfifo(fifo)
    outside_artifact = replace(catalog_fixture.artifact, location=outside_root)
    linked_artifact = replace(catalog_fixture.artifact, location=symlink)
    nested_link_artifact = replace(
        catalog_fixture.artifact,
        location=linked_directory / "nested.cbz",
    )
    fifo_artifact = replace(catalog_fixture.artifact, location=fifo)

    outside_catalog = FakeCatalog(
        (replace(catalog_fixture.publications[0], artifacts=(outside_artifact,)),)
    )
    linked_catalog = FakeCatalog(
        (replace(catalog_fixture.publications[0], artifacts=(linked_artifact,)),)
    )
    nested_link_catalog = FakeCatalog(
        (replace(catalog_fixture.publications[0], artifacts=(nested_link_artifact,)),)
    )
    fifo_catalog = FakeCatalog(
        (replace(catalog_fixture.publications[0], artifacts=(fifo_artifact,)),)
    )

    async with app_client(create_app(secure_config, outside_catalog)) as client:
        outside_response = await client.get("/opds/v2/acquisitions/artifact-alpha")
    async with app_client(create_app(secure_config, linked_catalog)) as client:
        linked_response = await client.get("/opds/v2/acquisitions/artifact-alpha")
    async with app_client(create_app(secure_config, nested_link_catalog)) as client:
        nested_link_response = await client.get("/opds/v2/acquisitions/artifact-alpha")
    async with app_client(create_app(secure_config, fifo_catalog)) as client:
        fifo_response = await client.get("/opds/v2/acquisitions/artifact-alpha")

    assert outside_response.status_code == 404
    assert linked_response.status_code == 404
    assert nested_link_response.status_code == 404
    assert fifo_response.status_code == 404


async def test_artifact_sha256_is_verified_even_when_size_is_unchanged(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    replacement = b"x" * len(catalog_fixture.payload)
    assert sha256(replacement).hexdigest() != catalog_fixture.artifact.sha256
    catalog_fixture.artifact.location.write_bytes(replacement)
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get("/opds/v2/acquisitions/artifact-alpha")
        head = await client.head("/opds/v2/acquisitions/artifact-alpha")

    assert response.status_code == 409
    assert head.status_code == 409
