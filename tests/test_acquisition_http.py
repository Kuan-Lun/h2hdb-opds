import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from h2hdb import CatalogRevision

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
    opds_config: OPDSConfig,
) -> None:
    published_name = '../../ignored\r\nX-Evil: yes\\友善 Gallery "Title".cbz'
    artifact = replace(
        catalog_fixture.artifact,
        name=published_name,
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
    assert "hash-v1" not in disposition
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


async def test_artifact_path_rejects_symlinks_and_special_files(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "trusted-current"
    trusted_root.mkdir()
    secure_config = opds_config.model_copy(update={"library_root": trusted_root})
    outside_root = tmp_path / "outside.cbz"
    outside_root.write_bytes(catalog_fixture.payload)
    relative = Path(*catalog_fixture.artifact.storage_key.segments)
    expected = trusted_root / relative
    expected.parent.mkdir(parents=True)
    expected.symlink_to(outside_root)
    catalog = FakeCatalog((catalog_fixture.publications[0],))

    async with app_client(create_app(secure_config, catalog)) as client:
        linked_response = await client.get("/opds/v2/acquisitions/artifact-alpha")

    expected.unlink()
    shard_root = trusted_root / relative.parts[0]
    directory = expected.parent
    while directory != trusted_root:
        parent = directory.parent
        directory.rmdir()
        directory = parent
    outside_directory = tmp_path / "outside-directory"
    nested_payload = outside_directory.joinpath(*relative.parts[1:])
    nested_payload.parent.mkdir(parents=True)
    nested_payload.write_bytes(catalog_fixture.payload)
    shard_root.symlink_to(outside_directory, target_is_directory=True)

    async with app_client(create_app(secure_config, catalog)) as client:
        nested_link_response = await client.get("/opds/v2/acquisitions/artifact-alpha")

    shard_root.unlink()
    expected.parent.mkdir(parents=True)
    os.mkfifo(expected)
    async with app_client(create_app(secure_config, catalog)) as client:
        fifo_response = await client.get("/opds/v2/acquisitions/artifact-alpha")

    assert linked_response.status_code == 404
    assert nested_link_response.status_code == 404
    assert fifo_response.status_code == 404


async def test_artifact_size_contract_is_checked_without_hashing(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    catalog_fixture.artifact_path.write_bytes(catalog_fixture.payload[:-1])
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get("/opds/v2/acquisitions/artifact-alpha")
        head = await client.head("/opds/v2/acquisitions/artifact-alpha")

    assert response.status_code == 409
    assert head.status_code == 409


async def test_head_and_not_modified_do_not_read_artifact_payload(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v2/acquisitions/artifact-alpha"
    etag = f'"{catalog_fixture.artifact.sha256}"'

    with patch(
        "h2hdb_opds.acquisition._read_file",
        side_effect=AssertionError("payload must not be read"),
    ):
        async with app_client(app) as client:
            head = await client.head(path)
            not_modified = await client.get(path, headers={"If-None-Match": etag})

    assert head.status_code == 200
    assert not_modified.status_code == 304


async def test_open_descriptor_stream_survives_atomic_leaf_replacement(
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

    with patch.object(
        catalog_fixture.catalog,
        "get_catalog_revision",
        side_effect=replace_before_head_revalidation,
    ):
        async with app_client(app) as client:
            response = await client.get("/opds/v2/acquisitions/artifact-alpha")

    assert response.status_code == 200
    assert response.content == catalog_fixture.payload
    assert catalog_fixture.artifact_path.read_bytes() == replacement
