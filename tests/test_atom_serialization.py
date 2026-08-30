from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from fastapi import FastAPI, Request
from h2hdb import CatalogRecentArtifactWindow, CatalogRecentOrder

from h2hdb_opds import OPDSConfig
from h2hdb_opds.atom import (
    ATOM_NAMESPACE,
    DC_TERMS_NAMESPACE,
    OPDS12_ACQUISITION_MEDIA_TYPE,
    OPDS12_NAVIGATION_MEDIA_TYPE,
    OPDS_ACQUISITION_REL,
    OPDS_NAMESPACE,
    OPDS_SORT_NEW_REL,
    navigation_feed_document,
    recent_acquisition_feed_document,
)

from .fakes import CatalogFixture

_NAMESPACES = {"atom": ATOM_NAMESPACE, "dc": DC_TERMS_NAMESPACE}


def _serialization_request() -> Request:
    app = FastAPI()

    @app.get("/opds/v1.2/catalog", name="opds12_catalog")
    def catalog() -> None:
        return None

    @app.get("/opds/v1.2/recent/uploaded", name="opds12_recent_uploaded")
    def recently_uploaded() -> None:
        return None

    @app.get("/opds/v1.2/recent/downloaded", name="opds12_recent_downloaded")
    def recently_downloaded() -> None:
        return None

    @app.get(
        "/opds/v1.2/acquisitions/{artifact_id}",
        name="opds12_acquire_artifact",
    )
    def acquire(artifact_id: str) -> None:
        del artifact_id

    return Request(
        {
            "type": "http",
            "app": app,
            "router": app.router,
            "method": "GET",
            "scheme": "http",
            "server": ("untrusted.example", 80),
            "path": "/ignored",
            "root_path": "",
            "query_string": b"",
            "headers": (),
        }
    )


def _links_by_relation(root: ElementTree.Element) -> dict[str, ElementTree.Element]:
    return {link.attrib["rel"]: link for link in root.findall("atom:link", _NAMESPACES)}


def _query(href: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(href).query)


def _window(
    catalog_fixture: CatalogFixture,
    *publication_indexes: int,
    order: CatalogRecentOrder = CatalogRecentOrder.UPLOADED,
) -> CatalogRecentArtifactWindow:
    publications = tuple(
        catalog_fixture.publications[index] for index in publication_indexes
    )
    return CatalogRecentArtifactWindow(
        revision=replace(
            catalog_fixture.catalog.revision,
            publication_count=len(publications),
            artifact_count=len(publications),
        ),
        order=order,
        publications=publications,
    )


def test_navigation_feed_serializes_two_revision_bound_recent_entries(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    document = navigation_feed_document(
        _serialization_request(),
        opds_config,
        catalog_fixture.catalog.revision,
    )

    assert document.startswith(b"<?xml")
    assert f'xmlns="{ATOM_NAMESPACE}"'.encode() in document
    root = ElementTree.fromstring(document)
    assert root.tag == f"{{{ATOM_NAMESPACE}}}feed"
    assert root.findtext("atom:id", namespaces=_NAMESPACES) == (
        "http://catalog.example/opds/v1.2/catalog"
    )
    assert root.findtext("atom:title", namespaces=_NAMESPACES) == opds_config.title
    assert root.findtext("atom:updated", namespaces=_NAMESPACES) == (
        "2026-08-05T12:00:00Z"
    )
    assert root.findtext("atom:author/atom:name", namespaces=_NAMESPACES) == (
        opds_config.title
    )

    feed_links = _links_by_relation(root)
    assert set(feed_links) == {"self", "start"}
    assert feed_links["self"].attrib == feed_links["start"].attrib | {"rel": "self"}
    for relation, link in feed_links.items():
        assert link.attrib["type"] == OPDS12_NAVIGATION_MEDIA_TYPE
        assert _query(link.attrib["href"]) == {"revision": ["7"]}, relation

    entries = root.findall("atom:entry", _NAMESPACES)
    assert [
        entry.findtext("atom:title", namespaces=_NAMESPACES) for entry in entries
    ] == [
        "Recently Uploaded",
        "Recently Downloaded",
    ]
    assert [entry.findtext("atom:id", namespaces=_NAMESPACES) for entry in entries] == [
        "urn:h2hdb:navigation:recently-uploaded",
        "urn:h2hdb:navigation:recently-downloaded",
    ]
    assert all(
        entry.findtext("atom:updated", namespaces=_NAMESPACES) == "2026-08-05T12:00:00Z"
        for entry in entries
    )
    uploaded_link = entries[0].find("atom:link", _NAMESPACES)
    downloaded_link = entries[1].find("atom:link", _NAMESPACES)
    assert uploaded_link is not None
    assert downloaded_link is not None
    assert uploaded_link.attrib == {
        "rel": OPDS_SORT_NEW_REL,
        "href": "http://catalog.example/opds/v1.2/recent/uploaded?revision=7",
        "type": OPDS12_ACQUISITION_MEDIA_TYPE,
        "title": "Recently Uploaded",
    }
    assert downloaded_link.attrib == {
        "rel": "subsection",
        "href": "http://catalog.example/opds/v1.2/recent/downloaded?revision=7",
        "type": OPDS12_ACQUISITION_MEDIA_TYPE,
        "title": "Recently Downloaded",
    }
    assert not root.findall(
        f"atom:entry/atom:link[@rel='{OPDS_ACQUISITION_REL}']",
        _NAMESPACES,
    )


def test_recent_acquisition_feed_is_single_page_with_root_navigation(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    window = catalog_fixture.catalog.list_recent_artifact_publications(
        order=CatalogRecentOrder.UPLOADED
    )
    document = recent_acquisition_feed_document(
        _serialization_request(),
        opds_config,
        window,
        endpoint="opds12_recent_uploaded",
        title="Recently Uploaded",
    )

    assert f'xmlns:dc="{DC_TERMS_NAMESPACE}"'.encode() in document
    assert f'xmlns:opds="{OPDS_NAMESPACE}"'.encode() in document
    root = ElementTree.fromstring(document)
    assert root.findtext("atom:id", namespaces=_NAMESPACES) == (
        "http://catalog.example/opds/v1.2/recent/uploaded"
    )
    assert root.findtext("atom:title", namespaces=_NAMESPACES) == "Recently Uploaded"
    assert root.findtext("atom:updated", namespaces=_NAMESPACES) == (
        "2026-08-05T12:00:00Z"
    )
    links = _links_by_relation(root)
    assert set(links) == {"self", "start", "up"}
    assert links["self"].attrib["type"] == OPDS12_ACQUISITION_MEDIA_TYPE
    assert links["self"].attrib["href"] == (
        "http://catalog.example/opds/v1.2/recent/uploaded?revision=7"
    )
    for relation in ("start", "up"):
        assert links[relation].attrib["type"] == OPDS12_NAVIGATION_MEDIA_TYPE
        assert links[relation].attrib["href"] == (
            "http://catalog.example/opds/v1.2/catalog?revision=7"
        )
    assert [
        entry.findtext("atom:title", namespaces=_NAMESPACES)
        for entry in root.findall("atom:entry", _NAMESPACES)
    ] == ["Beta Gallery", "Alpha Gallery", "Gamma Gallery"]


def test_recent_entry_serializes_metadata_and_revision_bound_acquisition(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    root = ElementTree.fromstring(
        recent_acquisition_feed_document(
            _serialization_request(),
            opds_config,
            _window(catalog_fixture, 0),
            endpoint="opds12_recent_uploaded",
            title="Recently Uploaded",
        )
    )

    alpha = root.find("atom:entry", _NAMESPACES)
    assert alpha is not None
    assert alpha.findtext("atom:id", namespaces=_NAMESPACES) == (
        "urn:h2hdb:publication:publication-alpha"
    )
    assert alpha.findtext("atom:title", namespaces=_NAMESPACES) == "Alpha Gallery"
    assert alpha.findtext("atom:updated", namespaces=_NAMESPACES) == (
        "2026-08-05T12:30:45Z"
    )
    assert alpha.findtext("atom:author/atom:name", namespaces=_NAMESPACES) == "Alice"
    assert alpha.findtext("atom:content", namespaces=_NAMESPACES) == (
        "A cobalt adventure"
    )
    assert alpha.findtext("dc:identifier", namespaces=_NAMESPACES) == (
        "publication-alpha"
    )
    assert alpha.findtext("dc:language", namespaces=_NAMESPACES) == "en"
    assert alpha.findtext("dc:issued", namespaces=_NAMESPACES) == (
        "2026-08-05T12:30:45Z"
    )
    acquisition = next(
        link
        for link in alpha.findall("atom:link", _NAMESPACES)
        if link.attrib["rel"] == OPDS_ACQUISITION_REL
    )
    assert acquisition.attrib == {
        "rel": OPDS_ACQUISITION_REL,
        "href": (
            "http://catalog.example/opds/v1.2/acquisitions/artifact-alpha?revision=7"
        ),
        "type": "application/vnd.comicbook+zip",
        "title": "alpha.cbz",
        "length": str(len(catalog_fixture.payload)),
    }


def test_xml_text_is_escaped_and_empty_summary_falls_back_to_title(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        title="A <Title> & More\x01",
        summary="   ",
    )
    window = CatalogRecentArtifactWindow(
        revision=replace(
            catalog_fixture.catalog.revision,
            publication_count=1,
            artifact_count=1,
        ),
        order=CatalogRecentOrder.UPLOADED,
        publications=(publication,),
    )

    root = ElementTree.fromstring(
        recent_acquisition_feed_document(
            _serialization_request(),
            opds_config,
            window,
            endpoint="opds12_recent_uploaded",
            title="Recent <Uploads> & More\ud800",
        )
    )

    assert root.findtext("atom:title", namespaces=_NAMESPACES) == (
        "Recent <Uploads> & More\N{REPLACEMENT CHARACTER}"
    )
    assert root.findtext("atom:entry/atom:title", namespaces=_NAMESPACES) == (
        "A <Title> & More\N{REPLACEMENT CHARACTER}"
    )
    assert root.findtext("atom:entry/atom:content", namespaces=_NAMESPACES) == (
        "A <Title> & More\N{REPLACEMENT CHARACTER}"
    )
