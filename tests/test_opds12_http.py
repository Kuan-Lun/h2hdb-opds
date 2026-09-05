from dataclasses import replace
from urllib.parse import parse_qs, quote, urlsplit
from xml.etree import ElementTree

import pytest
from h2hdb import CatalogSubject
from httpx import Response
from lxml import etree

from h2hdb_opds import OPDSConfig, create_app
from h2hdb_opds.atom import (
    ATOM_NAMESPACE,
    OPDS12_ACQUISITION_MEDIA_TYPE,
    OPDS12_ENTRY_MEDIA_TYPE,
    OPDS12_NAVIGATION_MEDIA_TYPE,
    OPDS_NAMESPACE,
    OPEN_SEARCH_MEDIA_TYPE,
    OPEN_SEARCH_NAMESPACE,
    PSE_NAMESPACE,
)
from h2hdb_opds.publication import OPDS_OPEN_ACCESS_REL

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client

_NAMESPACES = {
    "atom": ATOM_NAMESPACE,
    "opds": OPDS_NAMESPACE,
    "opensearch": OPEN_SEARCH_NAMESPACE,
    "pse": PSE_NAMESPACE,
}


def _xml(response: Response, media_type: str) -> ElementTree.Element:
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    return ElementTree.fromstring(response.content)


def _entry_titles(root: ElementTree.Element) -> list[str | None]:
    return [
        entry.findtext("atom:title", namespaces=_NAMESPACES)
        for entry in root.findall("atom:entry", _NAMESPACES)
    ]


def _link(root: ElementTree.Element, relation: str) -> ElementTree.Element:
    return next(
        link
        for link in root.findall("atom:link", _NAMESPACES)
        if link.attrib["rel"] == relation
    )


def _entry_link(
    root: ElementTree.Element,
    title: str,
) -> ElementTree.Element:
    return next(
        link
        for entry in root.findall("atom:entry", _NAMESPACES)
        if entry.findtext("atom:title", namespaces=_NAMESPACES) == title
        for link in entry.findall("atom:link", _NAMESPACES)
    )


async def test_root_has_all_and_both_recent_navigation_entries(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get("/opds/v1.2/catalog")

    root = _xml(response, OPDS12_NAVIGATION_MEDIA_TYPE)
    assert _entry_titles(root) == [
        "All Publications",
        "Recently Uploaded",
        "Recently Downloaded",
    ]
    all_link = _entry_link(root, "All Publications")
    assert all_link.attrib["href"].endswith("/opds/v1.2/publications?revision=7")
    search = _link(root, "search")
    assert search.attrib["type"] == OPEN_SEARCH_MEDIA_TYPE
    assert search.attrib["href"].endswith("/opds/v1.2/opensearch.xml?revision=7")


async def test_all_publications_is_seek_paged_and_entries_link_standalone_media(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        first_response = await client.get("/opds/v1.2/publications")
        first = _xml(first_response, OPDS12_ACQUISITION_MEDIA_TYPE)
        next_url = _link(first, "next").attrib["href"]
        second = _xml(
            await client.get(next_url),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )
        detail_url = next(
            link.attrib["href"]
            for link in first.findall("atom:entry/atom:link", _NAMESPACES)
            if link.attrib["rel"] == "alternate"
        )
        detail = _xml(await client.get(detail_url), OPDS12_ENTRY_MEDIA_TYPE)

    assert _entry_titles(first) == ["Alpha Gallery", "Beta Gallery"]
    assert _entry_titles(second) == ["Gamma Gallery"]
    assert "cursor=" in next_url and "offset=" not in next_url
    assert detail.findtext("atom:id", namespaces=_NAMESPACES) == (
        "urn:h2h:gallery:1001"
    )
    links = detail.findall("atom:link", _NAMESPACES)
    assert any(link.attrib["rel"] == OPDS_OPEN_ACCESS_REL for link in links)
    assert any(link.attrib["rel"] == "http://opds-spec.org/image" for link in links)
    assert any(
        link.attrib["rel"] == "http://opds-spec.org/image/thumbnail" for link in links
    )
    pse = next(
        link
        for link in links
        if link.attrib["rel"] == "http://vaemendis.net/opds-pse/stream"
    )
    assert "{pageNumber}" in pse.attrib["href"]
    assert "%7BpageNumber%7D" not in pse.attrib["href"]
    assert pse.attrib[f"{{{PSE_NAMESPACE}}}count"] == "1"
    assert "maxWidth" not in pse.attrib and "lastRead" not in pse.attrib


async def test_opensearch_description_uses_literal_standard_template(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        before = await client.get("/opds/v1.2/catalog")
        response = await client.get("/opds/v1.2/opensearch.xml")
        after = await client.get("/opds/v1.2/catalog")

    root = _xml(response, OPEN_SEARCH_MEDIA_TYPE)
    template = root.find(f"{{{OPEN_SEARCH_NAMESPACE}}}Url")
    assert template is not None
    assert template.attrib["template"].endswith(
        "/opds/v1.2/search?q={searchTerms}&revision=7"
    )
    descriptor = etree.fromstring(response.content)
    assert descriptor.tag == f"{{{OPEN_SEARCH_NAMESPACE}}}OpenSearchDescription"
    assert descriptor.nsmap[None] == OPEN_SEARCH_NAMESPACE
    assert all(not element.prefix for element in descriptor.iter())
    for feed_response in (before, after):
        feed = etree.fromstring(feed_response.content)
        assert feed.tag == f"{{{ATOM_NAMESPACE}}}feed"
        assert not feed.prefix
        assert feed.nsmap[None] == ATOM_NAMESPACE


@pytest.mark.parametrize("query", ("Alpha", "中文測試 Café & C++ #1", "gid:1834943"))
async def test_opensearch_template_can_be_followed_by_replacing_only_search_terms(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    query: str,
) -> None:
    publication = replace(catalog_fixture.publications[0], title=query)
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        root = _xml(
            await client.get("/opds/v1.2/catalog"), OPDS12_NAVIGATION_MEDIA_TYPE
        )
        descriptor = _xml(
            await client.get(_link(root, "search").attrib["href"]),
            OPEN_SEARCH_MEDIA_TYPE,
        )
        template = descriptor.find(f"{{{OPEN_SEARCH_NAMESPACE}}}Url")
        assert template is not None
        search_url = template.attrib["template"].replace(
            "{searchTerms}", quote(query, safe="")
        )
        response = await client.get(search_url)
        feed = _xml(response, OPDS12_ACQUISITION_MEDIA_TYPE)

    assert _entry_titles(feed) == [query]
    assert parse_qs(urlsplit(_link(feed, "self").attrib["href"]).query) == {
        "q": [query],
        "revision": ["7"],
        "limit": ["50"],
    }


async def test_opensearch_results_keep_pagination_and_revision_fencing(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(update={"default_page_size": 1})
    catalog = catalog_fixture.catalog
    app = create_app(config, catalog)

    async with app_client(app) as client:
        descriptor = _xml(
            await client.get("/opds/v1.2/opensearch.xml?revision=7"),
            OPEN_SEARCH_MEDIA_TYPE,
        )
        template = descriptor.find(f"{{{OPEN_SEARCH_NAMESPACE}}}Url")
        assert template is not None
        search_url = template.attrib["template"].replace("{searchTerms}", "cobalt")
        first = _xml(await client.get(search_url), OPDS12_ACQUISITION_MEDIA_TYPE)
        next_url = _link(first, "next").attrib["href"]
        second = _xml(await client.get(next_url), OPDS12_ACQUISITION_MEDIA_TYPE)
        assert _entry_titles(first) == ["Alpha Gallery"]
        assert _entry_titles(second) == ["Gamma Gallery"]
        assert parse_qs(urlsplit(next_url).query)["q"] == ["cobalt"]
        assert parse_qs(urlsplit(next_url).query)["revision"] == ["7"]

        catalog.add_revision(
            replace(catalog.revision, revision=8), catalog.publications
        )
        assert (await client.get(search_url)).status_code == 404
        assert (await client.get(next_url)).status_code == 404
        assert (
            await client.get("/opds/v1.2/opensearch.xml?revision=7")
        ).status_code == 404
        fresh = _xml(
            await client.get("/opds/v1.2/opensearch.xml"), OPEN_SEARCH_MEDIA_TYPE
        )
        fresh_template = fresh.find(f"{{{OPEN_SEARCH_NAMESPACE}}}Url")
        assert fresh_template is not None
        assert fresh_template.attrib["template"].endswith("&revision=8")


async def test_search_and_atom_facets_preserve_query_and_counts(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get(
            "/opds/v1.2/search",
            params={"q": "cobalt"},
        )

    root = _xml(response, OPDS12_ACQUISITION_MEDIA_TYPE)
    assert _entry_titles(root) == ["Alpha Gallery", "Gamma Gallery"]
    facets = [
        link
        for link in root.findall("atom:link", _NAMESPACES)
        if link.attrib["rel"] == "http://opds-spec.org/facet"
    ]
    assert facets
    assert {link.attrib[f"{{{OPDS_NAMESPACE}}}facetGroup"] for link in facets} >= {
        "Language",
        "Tag",
    }
    assert all(
        parse_qs(urlsplit(link.attrib["href"]).query)["q"] == ["cobalt"]
        for link in facets
    )
    tag = next(link for link in facets if link.attrib.get("title") == "fantasy")
    tag_parameters = parse_qs(urlsplit(tag.attrib["href"]).query)
    assert tag_parameters["tag"] == ["fantasy"]
    assert tag_parameters["tag_namespace"] == ["f"]


async def test_atom_large_facet_sets_link_to_bounded_navigation_pages(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        subjects=tuple(
            CatalogSubject(
                name=f"tag-{index:03d}",
                scheme="tag",
                code=f"t{index:03d}",
            )
            for index in range(130)
        ),
    )
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        search = _xml(
            await client.get("/opds/v1.2/publications"),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )
        more = next(
            link
            for link in search.findall("atom:link", _NAMESPACES)
            if link.attrib.get("title") == "More Tag values"
        )
        facet_page = _xml(
            await client.get(more.attrib["href"]),
            OPDS12_NAVIGATION_MEDIA_TYPE,
        )

    assert f"{{{OPDS_NAMESPACE}}}facetGroup" not in more.attrib
    assert _entry_titles(facet_page) == [
        "All Tag values",
        "tag-128",
        "tag-129",
    ]


async def test_recent_feeds_are_fixed_complete_windows(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        uploaded = _xml(
            await client.get("/opds/v1.2/recent/uploaded"),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )
        downloaded = _xml(
            await client.get("/opds/v1.2/recent/downloaded"),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )
        rejected = await client.get(
            "/opds/v1.2/recent/uploaded",
            params={"limit": 1},
        )

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
    assert not any(
        link.attrib["rel"] in {"first", "next"}
        for link in uploaded.findall("atom:link", _NAMESPACES)
    )
    assert rejected.status_code == 422


async def test_empty_atom_acquisition_feeds_are_successful(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publications = tuple(
        replace(publication, artifacts=())
        for publication in catalog_fixture.publications
    )
    catalog = FakeCatalog(publications)
    catalog.revision = replace(catalog.revision, artifact_count=0)
    app = create_app(opds_config, catalog)

    async with app_client(app) as client:
        all_feed = _xml(
            await client.get("/opds/v1.2/publications"),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )
        recent = _xml(
            await client.get("/opds/v1.2/recent/uploaded"),
            OPDS12_ACQUISITION_MEDIA_TYPE,
        )

    assert _entry_titles(all_feed) == []
    assert _entry_titles(recent) == []


async def test_atom_head_reports_get_length_without_a_body(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        get_response = await client.get("/opds/v1.2/publications")
        head_response = await client.head("/opds/v1.2/publications")

    assert head_response.status_code == 200
    assert head_response.content == b""
    assert head_response.headers["content-length"] == str(len(get_response.content))
