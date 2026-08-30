from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import pytest
from fastapi import FastAPI, Request
from h2hdb import CatalogArtifactPage

from h2hdb_opds import OPDSConfig
from h2hdb_opds.atom import (
    ATOM_NAMESPACE,
    DC_TERMS_NAMESPACE,
    OPDS12_ACQUISITION_MEDIA_TYPE,
    OPDS_ACQUISITION_REL,
    OPDS_NAMESPACE,
    acquisition_feed_document,
)

from .fakes import CatalogFixture

_NAMESPACES = {"atom": ATOM_NAMESPACE, "dc": DC_TERMS_NAMESPACE}


def _serialization_request() -> Request:
    app = FastAPI()

    @app.get("/opds/v1.2/catalog", name="opds12_catalog")
    def catalog() -> None:
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


def test_acquisition_feed_serializes_revision_bound_navigation_and_entries(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    page = catalog_fixture.catalog.list_artifact_publications(limit=2)

    document = acquisition_feed_document(
        _serialization_request(),
        opds_config,
        page,
        cursor=None,
    )

    assert document.startswith(b"<?xml")
    assert f'xmlns="{ATOM_NAMESPACE}"'.encode() in document
    assert f'xmlns:dc="{DC_TERMS_NAMESPACE}"'.encode() in document
    assert f'xmlns:opds="{OPDS_NAMESPACE}"'.encode() in document

    root = ElementTree.fromstring(document)
    assert root.tag == f"{{{ATOM_NAMESPACE}}}feed"
    assert root.findtext("atom:id", namespaces=_NAMESPACES) == (
        "http://catalog.example/opds/v1.2/catalog"
    )
    assert root.findtext("atom:title", namespaces=_NAMESPACES) == "All Publications"
    assert root.findtext("atom:updated", namespaces=_NAMESPACES) == (
        "2026-08-05T12:00:00Z"
    )
    assert root.findtext("atom:author/atom:name", namespaces=_NAMESPACES) == (
        opds_config.title
    )

    feed_links = _links_by_relation(root)
    assert set(feed_links) == {"self", "start", "first", "next"}
    for relation, link in feed_links.items():
        assert link.attrib["type"] == OPDS12_ACQUISITION_MEDIA_TYPE
        assert link.attrib["href"].startswith(
            "http://catalog.example/opds/v1.2/catalog?"
        )
        assert _query(link.attrib["href"])["revision"] == ["7"], relation
    assert _query(feed_links["self"].attrib["href"]) == {
        "limit": ["2"],
        "revision": ["7"],
    }
    assert feed_links["start"].attrib["href"] == feed_links["first"].attrib["href"]
    assert "cursor" in _query(feed_links["next"].attrib["href"])

    entries = root.findall("atom:entry", _NAMESPACES)
    assert len(entries) == 2
    alpha = entries[0]
    assert alpha.findtext("atom:id", namespaces=_NAMESPACES) == (
        "urn:h2hdb:publication:publication-alpha"
    )
    assert alpha.findtext("atom:title", namespaces=_NAMESPACES) == "Alpha Gallery"
    assert alpha.findtext("atom:updated", namespaces=_NAMESPACES) == (
        "2026-08-05T12:30:45Z"
    )
    assert alpha.find("atom:published", _NAMESPACES) is None
    assert alpha.findtext("atom:author/atom:name", namespaces=_NAMESPACES) == "Alice"
    assert alpha.findtext("atom:content", namespaces=_NAMESPACES) == (
        "A cobalt adventure"
    )
    content = alpha.find("atom:content", _NAMESPACES)
    assert content is not None
    assert content.attrib == {"type": "text"}
    assert alpha.findtext("dc:identifier", namespaces=_NAMESPACES) == (
        "publication-alpha"
    )
    assert alpha.findtext("dc:language", namespaces=_NAMESPACES) == "en"
    assert alpha.findtext("dc:issued", namespaces=_NAMESPACES) == (
        "2026-08-05T12:30:45Z"
    )
    assert [
        category.attrib for category in alpha.findall("atom:category", _NAMESPACES)
    ] == [{"term": "f", "label": "fantasy"}]

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


def test_feed_id_is_stable_across_pages_and_empty_summary_falls_back_to_title(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    first_page = catalog_fixture.catalog.list_artifact_publications(limit=2)
    assert first_page.next_cursor is not None
    gamma = replace(catalog_fixture.publications[2], summary="   ")
    second_page = CatalogArtifactPage(
        revision=first_page.revision,
        publications=(gamma,),
        next_cursor=None,
        limit=2,
        total=first_page.total,
    )
    request = _serialization_request()

    first = ElementTree.fromstring(
        acquisition_feed_document(
            request,
            opds_config,
            first_page,
            cursor=None,
        )
    )
    second = ElementTree.fromstring(
        acquisition_feed_document(
            request,
            opds_config,
            second_page,
            cursor=first_page.next_cursor,
        )
    )

    assert first.findtext("atom:id", namespaces=_NAMESPACES) == second.findtext(
        "atom:id", namespaces=_NAMESPACES
    )
    assert second.findtext("atom:author/atom:name", namespaces=_NAMESPACES) == (
        opds_config.title
    )
    assert set(_links_by_relation(second)) == {"self", "start", "first"}
    assert "cursor" in _query(_links_by_relation(second)["self"].attrib["href"])
    entry = second.find("atom:entry", _NAMESPACES)
    assert entry is not None
    assert entry.find("atom:summary", _NAMESPACES) is None
    assert entry.findtext("atom:content", namespaces=_NAMESPACES) == "Gamma Gallery"
    assert entry.findtext("atom:id", namespaces=_NAMESPACES) == (
        "urn:h2hdb:publication:publication-gamma"
    )


def test_xml_text_is_escaped_and_cursor_revision_must_match_page(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        title="A <Title> & More\x01",
        summary="One < two & three\ud800",
    )
    page = CatalogArtifactPage(
        revision=catalog_fixture.catalog.revision,
        publications=(publication,),
        next_cursor=None,
        limit=1,
        total=1,
    )
    request = _serialization_request()

    root = ElementTree.fromstring(
        acquisition_feed_document(request, opds_config, page, cursor=None)
    )

    assert root.findtext("atom:entry/atom:title", namespaces=_NAMESPACES) == (
        "A <Title> & More\N{REPLACEMENT CHARACTER}"
    )
    assert root.findtext("atom:entry/atom:content", namespaces=_NAMESPACES) == (
        "One < two & three\N{REPLACEMENT CHARACTER}"
    )

    next_cursor = catalog_fixture.catalog.list_artifact_publications(
        limit=1
    ).next_cursor
    assert next_cursor is not None
    mismatched_cursor = replace(next_cursor, revision=8)
    with pytest.raises(ValueError, match="cursor does not match page revision"):
        acquisition_feed_document(
            request,
            opds_config,
            page,
            cursor=mismatched_cursor,
        )


def test_publication_without_artifact_is_rejected(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publication = replace(catalog_fixture.publications[0], artifacts=())
    page = CatalogArtifactPage(
        revision=catalog_fixture.catalog.revision,
        publications=(publication,),
        next_cursor=None,
        limit=1,
        total=1,
    )

    with pytest.raises(ValueError, match="requires at least one artifact"):
        acquisition_feed_document(
            _serialization_request(),
            opds_config,
            page,
            cursor=None,
        )
