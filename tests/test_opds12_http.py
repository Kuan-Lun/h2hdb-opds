import fcntl
import os
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from h2hdb import CatalogRevision
from httpx import Response
from pydantic import SecretStr

from h2hdb_opds import BasicAuthConfig, OPDSConfig, ServerConfig, create_app
from h2hdb_opds.atom import (
    ATOM_NAMESPACE,
    OPDS12_ACQUISITION_MEDIA_TYPE,
    OPDS_ACQUISITION_REL,
)

from .fakes import CatalogFixture
from .http_client import app_client

_NAMESPACES = {"atom": ATOM_NAMESPACE}
_PANELS_ACCEPT = (
    "application/atom+xml;profile=opds-catalog, application/atom+xml;q=0.9, */*;q=0.1"
)


def _feed(response: Response) -> ElementTree.Element:
    assert response.status_code == 200
    assert response.headers["content-type"] == OPDS12_ACQUISITION_MEDIA_TYPE
    return ElementTree.fromstring(response.content)


def _link(root: ElementTree.Element, relation: str) -> str:
    return next(
        link.attrib["href"]
        for link in root.findall("atom:link", _NAMESPACES)
        if link.attrib["rel"] == relation
    )


def _first_acquisition(root: ElementTree.Element) -> str:
    return next(
        link.attrib["href"]
        for link in root.findall("atom:entry/atom:link", _NAMESPACES)
        if link.attrib["rel"] == OPDS_ACQUISITION_REL
    )


async def test_panels_accept_header_returns_an_opds12_acquisition_feed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get(
            "/opds/v1.2/catalog",
            headers={"Accept": _PANELS_ACCEPT},
        )

    root = _feed(response)
    assert root.tag == f"{{{ATOM_NAMESPACE}}}feed"
    assert root.findtext("atom:title", namespaces=_NAMESPACES) == "All Publications"
    assert [
        entry.findtext("atom:title", namespaces=_NAMESPACES)
        for entry in root.findall("atom:entry", _NAMESPACES)
    ] == ["Alpha Gallery", "Beta Gallery", "Gamma Gallery"]
    assert _first_acquisition(root).endswith(
        "/opds/v1.2/acquisitions/artifact-alpha?revision=7"
    )


async def test_opds12_catalog_enforces_basic_authentication(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = OPDSConfig(
        library_root=opds_config.library_root,
        coordination_root=opds_config.coordination_root,
        public_base_url="https://books.example",
        auth=BasicAuthConfig(
            username="reader",
            password=SecretStr("secret"),
            realm="Panels Library",
        ),
        server=ServerConfig(trusted_proxy_ips=("127.0.0.1",)),
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app, base_url="https://testserver") as client:
        unauthorized = await client.get(
            "/opds/v1.2/catalog",
            headers={"Accept": _PANELS_ACCEPT},
        )
        wrong_password = await client.get(
            "/opds/v1.2/catalog",
            headers={"Accept": _PANELS_ACCEPT},
            auth=("reader", "wrong"),
        )
        authorized = await client.get(
            "/opds/v1.2/catalog",
            headers={"Accept": _PANELS_ACCEPT},
            auth=("reader", "secret"),
        )

    for response in (unauthorized, wrong_password):
        assert response.status_code == 401
        assert response.content == b""
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["www-authenticate"] == (
            'Basic realm="Panels Library", charset="UTF-8"'
        )
    assert _feed(authorized).find("atom:entry", _NAMESPACES) is not None


async def test_opds12_cursor_links_page_within_the_selected_revision(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        first_response = await client.get("/opds/v1.2/catalog")
        first = _feed(first_response)
        next_url = _link(first, "next")
        second_response = await client.get(next_url)

    second = _feed(second_response)
    next_query = parse_qs(urlsplit(next_url).query)
    assert next_query["revision"] == ["7"]
    assert len(next_query["cursor"]) == 1
    assert [
        entry.findtext("atom:title", namespaces=_NAMESPACES)
        for entry in first.findall("atom:entry", _NAMESPACES)
    ] == ["Alpha Gallery", "Beta Gallery"]
    assert [
        entry.findtext("atom:title", namespaces=_NAMESPACES)
        for entry in second.findall("atom:entry", _NAMESPACES)
    ] == ["Gamma Gallery"]
    assert all(
        revision == catalog_fixture.catalog.revision
        for revision in catalog_fixture.catalog.list_revisions
    )
    assert catalog_fixture.catalog.artifact_list_calls[0] == (None, 2)
    assert catalog_fixture.catalog.artifact_list_calls[1][0] is not None
    assert catalog_fixture.catalog.artifact_list_calls[1][1] == 2


async def test_opds12_revision_bound_links_fail_closed_after_head_advances(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        first = _feed(await client.get("/opds/v1.2/catalog"))
        stale_next = _link(first, "next")
        stale_acquisition = _first_acquisition(first)
        catalog_fixture.catalog.add_revision(
            CatalogRevision(
                revision=8,
                published_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
                publication_count=1,
                artifact_count=1,
            ),
            (catalog_fixture.publications[1],),
        )

        old_next_response = await client.get(stale_next)
        old_acquisition_response = await client.get(stale_acquisition)
        current_response = await client.get("/opds/v1.2/catalog")

    for response in (old_next_response, old_acquisition_response):
        assert response.status_code == 404
        assert response.json() == {"detail": "Catalog revision 7 not found"}
    current = _feed(current_response)
    assert parse_qs(urlsplit(_link(current, "self")).query)["revision"] == ["8"]
    assert [
        entry.findtext("atom:title", namespaces=_NAMESPACES)
        for entry in current.findall("atom:entry", _NAMESPACES)
    ] == ["Beta Gallery"]


async def test_opds12_catalog_head_has_get_metadata_without_a_body(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    path = "/opds/v1.2/catalog?limit=2&revision=7"

    async with app_client(app) as client:
        get_response = await client.get(path, headers={"Accept": _PANELS_ACCEPT})
        head_response = await client.head(path, headers={"Accept": _PANELS_ACCEPT})

    assert get_response.status_code == 200
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert head_response.headers["content-type"] == OPDS12_ACQUISITION_MEDIA_TYPE
    assert (
        head_response.headers["content-length"]
        == get_response.headers["content-length"]
    )


async def test_opds12_acquisition_link_supports_get_head_and_a_single_range(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        feed = _feed(await client.get("/opds/v1.2/catalog"))
        acquisition_url = _first_acquisition(feed)
        full = await client.get(acquisition_url)
        head = await client.head(acquisition_url)
        selected_range = await client.get(
            acquisition_url,
            headers={"Range": "bytes=2-6"},
        )

    expected_etag = f'"{catalog_fixture.artifact.sha256}"'
    assert full.status_code == 200
    assert full.content == catalog_fixture.payload
    assert full.headers["content-type"] == catalog_fixture.artifact.media_type
    assert full.headers["etag"] == expected_etag
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(catalog_fixture.payload))
    assert head.headers["etag"] == expected_etag
    assert selected_range.status_code == 206
    assert selected_range.content == catalog_fixture.payload[2:7]
    assert selected_range.headers["content-range"] == (
        f"bytes 2-6/{len(catalog_fixture.payload)}"
    )


async def test_opds12_catalog_and_acquisition_use_activation_coordination(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    marker = opds_config.coordination_root / "ACTIVATING"
    marker.write_text("activation in progress\n", encoding="utf-8")
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        blocked_catalog = await client.get("/opds/v1.2/catalog")
        blocked_acquisition = await client.get("/opds/v1.2/acquisitions/artifact-alpha")
        marker.unlink()

        lock_descriptor = os.open(
            opds_config.coordination_root / "publication.lock",
            os.O_RDWR | os.O_CLOEXEC,
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked_by_lock = await client.get("/opds/v1.2/catalog")
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            available = await client.get("/opds/v1.2/catalog")
        finally:
            os.close(lock_descriptor)

    for response in (blocked_catalog, blocked_acquisition, blocked_by_lock):
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.headers["cache-control"] == "no-store"
    assert available.status_code == 200


async def test_opds12_rejects_offset_invalid_cursor_and_excessive_limit(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        offset = await client.get(
            "/opds/v1.2/catalog",
            params={"offset": 2},
        )
        cursor = await client.get(
            "/opds/v1.2/catalog",
            params={"cursor": "not-a-valid-cursor"},
        )
        limit = await client.get(
            "/opds/v1.2/catalog",
            params={"limit": 3},
        )

    assert offset.status_code == 422
    assert offset.json() == {
        "detail": "offset pagination was removed; follow the cursor links"
    }
    assert cursor.status_code == 422
    assert cursor.json() == {"detail": "cursor is invalid"}
    assert limit.status_code == 422
    assert limit.json() == {"detail": "limit must not exceed 2"}
