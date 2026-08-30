import fcntl
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import pytest
from h2hdb import (
    CatalogRecentArtifactWindow,
    CatalogRecentOrder,
    CatalogRevision,
    artifact_storage_key,
)
from httpx import Response
from pydantic import SecretStr

from h2hdb_opds import BasicAuthConfig, OPDSConfig, ServerConfig, create_app
from h2hdb_opds.atom import (
    ATOM_NAMESPACE,
    OPDS12_ACQUISITION_MEDIA_TYPE,
    OPDS12_NAVIGATION_MEDIA_TYPE,
    OPDS_ACQUISITION_REL,
    OPDS_SORT_NEW_REL,
)

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client

_NAMESPACES = {"atom": ATOM_NAMESPACE}
_PANELS_ACCEPT = (
    "application/atom+xml;profile=opds-catalog, application/atom+xml;q=0.9, */*;q=0.1"
)


def _feed(response: Response, media_type: str) -> ElementTree.Element:
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    return ElementTree.fromstring(response.content)


def _feed_link(root: ElementTree.Element, relation: str) -> str:
    return next(
        link.attrib["href"]
        for link in root.findall("atom:link", _NAMESPACES)
        if link.attrib["rel"] == relation
    )


def _navigation_link(root: ElementTree.Element, title: str) -> ElementTree.Element:
    return next(
        link
        for entry in root.findall("atom:entry", _NAMESPACES)
        if entry.findtext("atom:title", namespaces=_NAMESPACES) == title
        for link in entry.findall("atom:link", _NAMESPACES)
    )


def _first_acquisition(root: ElementTree.Element) -> str:
    return next(
        link.attrib["href"]
        for link in root.findall("atom:entry/atom:link", _NAMESPACES)
        if link.attrib["rel"] == OPDS_ACQUISITION_REL
    )


def _entry_titles(root: ElementTree.Element) -> list[str | None]:
    return [
        entry.findtext("atom:title", namespaces=_NAMESPACES)
        for entry in root.findall("atom:entry", _NAMESPACES)
    ]


async def test_panels_catalog_is_two_entry_navigation_to_recent_feeds(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        root_response = await client.get(
            "/opds/v1.2/catalog",
            headers={"Accept": _PANELS_ACCEPT},
        )
        root = _feed(root_response, OPDS12_NAVIGATION_MEDIA_TYPE)
        uploaded_link = _navigation_link(root, "Recently Uploaded")
        downloaded_link = _navigation_link(root, "Recently Downloaded")
        uploaded_response = await client.get(uploaded_link.attrib["href"])
        downloaded_response = await client.get(downloaded_link.attrib["href"])

    assert root.tag == f"{{{ATOM_NAMESPACE}}}feed"
    assert _entry_titles(root) == ["Recently Uploaded", "Recently Downloaded"]
    assert uploaded_link.attrib["rel"] == OPDS_SORT_NEW_REL
    assert downloaded_link.attrib["rel"] == "subsection"
    for link in (uploaded_link, downloaded_link):
        assert link.attrib["type"] == OPDS12_ACQUISITION_MEDIA_TYPE
        assert parse_qs(urlsplit(link.attrib["href"]).query) == {"revision": ["7"]}

    uploaded = _feed(uploaded_response, OPDS12_ACQUISITION_MEDIA_TYPE)
    downloaded = _feed(downloaded_response, OPDS12_ACQUISITION_MEDIA_TYPE)
    assert _entry_titles(uploaded) == [
        "Beta Gallery",
        "Alpha Gallery",
        "Gamma Gallery",
    ]
    assert _entry_titles(downloaded) == [
        "Gamma Gallery",
        "Alpha Gallery",
        "Beta Gallery",
    ]
    assert [call[0] for call in catalog_fixture.catalog.recent_list_calls] == [
        CatalogRecentOrder.UPLOADED,
        CatalogRecentOrder.DOWNLOADED,
    ]
    assert all(
        revision == catalog_fixture.catalog.revision
        for _, revision in catalog_fixture.catalog.recent_list_calls
    )


async def test_opds12_navigation_and_recent_feed_enforce_basic_authentication(
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
        unauthorized_root = await client.get("/opds/v1.2/catalog")
        unauthorized_recent = await client.get("/opds/v1.2/recent/uploaded")
        wrong_password = await client.get(
            "/opds/v1.2/catalog",
            auth=("reader", "wrong"),
        )
        authorized = await client.get(
            "/opds/v1.2/catalog",
            auth=("reader", "secret"),
        )

    for response in (unauthorized_root, unauthorized_recent, wrong_password):
        assert response.status_code == 401
        assert response.content == b""
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["www-authenticate"] == (
            'Basic realm="Panels Library", charset="UTF-8"'
        )
    assert (
        _feed(authorized, OPDS12_NAVIGATION_MEDIA_TYPE).find("atom:entry", _NAMESPACES)
        is not None
    )


async def test_recent_feed_is_hard_capped_at_128_without_pagination(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    base = catalog_fixture.publications[0]
    publications = tuple(
        replace(
            base,
            publication_id=f"publication-{index}",
            gid=10_000 + index,
            title=f"Publication {index}",
            published_at=base.published_at + timedelta(seconds=index),
            downloaded_at=base.downloaded_at + timedelta(seconds=index),
            artifacts=(
                replace(
                    base.artifacts[0],
                    artifact_id=f"artifact-{index}",
                    name=f"publication-{index}.cbz",
                    storage_key=artifact_storage_key(10_000 + index),
                ),
            ),
        )
        for index in range(130)
    )
    app = create_app(opds_config, FakeCatalog(publications))

    async with app_client(app) as client:
        response = await client.get("/opds/v1.2/recent/uploaded")

    feed = _feed(response, OPDS12_ACQUISITION_MEDIA_TYPE)
    assert len(feed.findall("atom:entry", _NAMESPACES)) == 128
    assert _entry_titles(feed)[:2] == ["Publication 129", "Publication 128"]
    relations = {link.attrib["rel"] for link in feed.findall("atom:link", _NAMESPACES)}
    assert relations == {"self", "start", "up"}
    assert parse_qs(urlsplit(_feed_link(feed, "self")).query) == {"revision": ["7"]}


async def test_recent_feeds_allow_an_empty_artifact_catalog(
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, FakeCatalog(()))

    async with app_client(app) as client:
        uploaded = _feed(
            await client.get("/opds/v1.2/recent/uploaded"),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )
        downloaded = _feed(
            await client.get("/opds/v1.2/recent/downloaded"),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )

    assert _entry_titles(uploaded) == []
    assert _entry_titles(downloaded) == []


async def test_revision_bound_recent_and_acquisition_links_fail_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        root = _feed(
            await client.get("/opds/v1.2/catalog"),
            OPDS12_NAVIGATION_MEDIA_TYPE,
        )
        stale_recent = _navigation_link(root, "Recently Uploaded").attrib["href"]
        old_recent = _feed(
            await client.get(stale_recent),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )
        stale_acquisition = _first_acquisition(old_recent)
        catalog_fixture.catalog.add_revision(
            CatalogRevision(
                revision=8,
                published_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
                publication_count=1,
                artifact_count=1,
            ),
            (catalog_fixture.publications[1],),
        )

        old_recent_response = await client.get(stale_recent)
        old_acquisition_response = await client.get(stale_acquisition)
        current_root = _feed(
            await client.get("/opds/v1.2/catalog"),
            OPDS12_NAVIGATION_MEDIA_TYPE,
        )

    for response in (old_recent_response, old_acquisition_response):
        assert response.status_code == 404
        assert response.json() == {"detail": "Catalog revision 7 not found"}
    assert parse_qs(urlsplit(_feed_link(current_root, "self")).query) == {
        "revision": ["8"]
    }


async def test_recent_feed_rejects_a_reader_window_for_the_wrong_order(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = catalog_fixture.catalog.list_recent_artifact_publications

    def wrong_order(
        *,
        order: CatalogRecentOrder,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogRecentArtifactWindow:
        del order
        return original(order=CatalogRecentOrder.DOWNLOADED, revision=revision)

    monkeypatch.setattr(
        catalog_fixture.catalog,
        "list_recent_artifact_publications",
        wrong_order,
    )
    app = create_app(opds_config, catalog_fixture.catalog)

    with pytest.raises(
        ValueError,
        match="recent artifact window order differs from the request",
    ):
        async with app_client(app) as client:
            await client.get("/opds/v1.2/recent/uploaded")


async def test_opds12_navigation_and_recent_head_match_get_metadata(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        for path, media_type in (
            ("/opds/v1.2/catalog?revision=7", OPDS12_NAVIGATION_MEDIA_TYPE),
            (
                "/opds/v1.2/recent/uploaded?revision=7",
                OPDS12_ACQUISITION_MEDIA_TYPE,
            ),
        ):
            get_response = await client.get(path, headers={"Accept": _PANELS_ACCEPT})
            head_response = await client.head(path, headers={"Accept": _PANELS_ACCEPT})
            assert get_response.status_code == 200
            assert head_response.status_code == 200
            assert head_response.content == b""
            assert head_response.headers["content-type"] == media_type
            assert (
                head_response.headers["content-length"]
                == (get_response.headers["content-length"])
            )


async def test_opds12_recent_acquisition_supports_get_head_and_range(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        feed = _feed(
            await client.get("/opds/v1.2/recent/uploaded"),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )
        acquisition_url = next(
            link.attrib["href"]
            for entry in feed.findall("atom:entry", _NAMESPACES)
            if entry.findtext("atom:title", namespaces=_NAMESPACES) == "Alpha Gallery"
            for link in entry.findall("atom:link", _NAMESPACES)
            if link.attrib["rel"] == OPDS_ACQUISITION_REL
        )
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
    assert selected_range.status_code == 206
    assert selected_range.content == catalog_fixture.payload[2:7]
    assert selected_range.headers["content-range"] == (
        f"bytes 2-6/{len(catalog_fixture.payload)}"
    )


async def test_opds12_navigation_recent_and_acquisition_use_coordination(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    marker = opds_config.coordination_root / "ACTIVATING"
    marker.write_text("activation in progress\n", encoding="utf-8")
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        blocked_catalog = await client.get("/opds/v1.2/catalog")
        blocked_recent = await client.get("/opds/v1.2/recent/uploaded")
        blocked_acquisition = await client.get("/opds/v1.2/acquisitions/artifact-alpha")
        marker.unlink()

        lock_descriptor = os.open(
            opds_config.coordination_root / "publication.lock",
            os.O_RDWR | os.O_CLOEXEC,
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked_by_lock = await client.get("/opds/v1.2/recent/downloaded")
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            available = await client.get("/opds/v1.2/catalog")
        finally:
            os.close(lock_descriptor)

    for response in (
        blocked_catalog,
        blocked_recent,
        blocked_acquisition,
        blocked_by_lock,
    ):
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.headers["cache-control"] == "no-store"
    assert available.status_code == 200


async def test_opds12_has_no_all_publications_compatibility_path(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        root_response = await client.get("/opds/v1.2/catalog")
        removed = await client.get("/opds/v1.2/all")

    root = _feed(root_response, OPDS12_NAVIGATION_MEDIA_TYPE)
    assert removed.status_code == 404
    assert not root.findall(
        f"atom:entry/atom:link[@rel='{OPDS_ACQUISITION_REL}']",
        _NAMESPACES,
    )


async def test_opds12_rejects_removed_pagination_parameters(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        responses = [
            await client.get(path, params={parameter: value})
            for path, parameter, value in (
                ("/opds/v1.2/catalog", "cursor", "legacy"),
                ("/opds/v1.2/recent/uploaded", "limit", 20),
                ("/opds/v1.2/recent/downloaded", "offset", 20),
            )
        ]

    for response in responses:
        assert response.status_code == 422
        assert response.json() == {
            "detail": "OPDS 1.2 catalog does not support pagination"
        }
