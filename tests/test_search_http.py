from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import pytest
from h2hdb import CatalogSubject, CatalogSubjectFilter
from httpx import Response

from h2hdb_opds import OPDSConfig, create_app
from h2hdb_opds.atom import ATOM_NAMESPACE, OPDS_NAMESPACE
from h2hdb_opds.search import parse_search_query

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client

_NAMESPACES = {"atom": ATOM_NAMESPACE}


@pytest.fixture(params=("v1.2", "v2"))
def version(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _search_parameter(version: str) -> str:
    return "q" if version == "v1.2" else "query"


def _identifiers(response: Response) -> tuple[str, ...]:
    assert response.status_code == 200, response.text
    if "atom+xml" in response.headers["content-type"]:
        return tuple(
            entry.findtext("atom:id", namespaces=_NAMESPACES) or ""
            for entry in ElementTree.fromstring(response.content).findall(
                "atom:entry", _NAMESPACES
            )
        )
    return tuple(
        str(publication["metadata"]["identifier"])
        for publication in response.json().get("publications", ())
    )


def _link(response: Response, relation: str) -> str:
    if "atom+xml" in response.headers["content-type"]:
        return next(
            link.attrib["href"]
            for link in ElementTree.fromstring(response.content).findall(
                "atom:link", _NAMESPACES
            )
            if link.attrib["rel"] == relation
        )
    return next(
        str(link["href"])
        for link in response.json()["links"]
        if link["rel"] == relation
    )


def _tag_facet_links(response: Response) -> dict[str, str]:
    if "atom+xml" in response.headers["content-type"]:
        return {
            link.attrib["title"]: link.attrib["href"]
            for link in ElementTree.fromstring(response.content).findall(
                "atom:link", _NAMESPACES
            )
            if link.attrib.get(f"{{{OPDS_NAMESPACE}}}facetGroup") == "Tag"
        }
    return {
        str(link["title"]): str(link["href"])
        for facet in response.json()["facets"]
        if facet["metadata"]["title"] == "Tag"
        for link in facet["links"]
    }


@pytest.mark.parametrize(
    ("text", "gids"),
    (
        ("gid:1001", (1001,)),
        ("title:Cobalt", (1001, 1003)),
        ("title:Alice", ()),
        ("Alice", (1001,)),
        ("tag:f:fantasy", (1001,)),
        ("downloaded:2026-08-05 uploaded:2026-08-05 pages:0", (1002, 1003)),
        ("pages:1..4096", (1001,)),
        ("1001", ()),
    ),
)
async def test_search_routes_accept_text_and_filter_only_queries(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    version: str,
    text: str,
    gids: tuple[int, ...],
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    parameter = _search_parameter(version)
    async with app_client(app) as client:
        response = await client.get(f"/opds/{version}/search", params={parameter: text})
        replay = await client.get(_link(response, "self"))
    expected = tuple(f"urn:h2h:gallery:{gid}" for gid in gids)
    assert _identifiers(response) == expected
    assert _identifiers(replay) == expected
    assert catalog_fixture.catalog.list_calls[0][0] == parse_search_query(text)


async def test_search_http_rejects_malformed_dsl_before_catalog_reads(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig, version: str
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        for text in ("unknown:value", "gid:0", "pages:200..40", 'tag:artist:"bad'):
            response = await client.get(
                f"/opds/{version}/search", params={_search_parameter(version): text}
            )
            assert response.status_code == 422
            assert response.json()["detail"]
    assert catalog_fixture.catalog.list_calls == []


async def test_search_pagination_preserves_all_filters_and_fences_changed_queries(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig, version: str
) -> None:
    config = opds_config.model_copy(update={"default_page_size": 1})
    app = create_app(config, catalog_fixture.catalog)
    parameter = _search_parameter(version)
    text = "title:Gallery uploaded:2026-08-05 downloaded:2026-08-05 pages:0..1"
    async with app_client(app) as client:
        first = await client.get(f"/opds/{version}/search", params={parameter: text})
        next_url = _link(first, "next")
        parameters = parse_qs(urlsplit(next_url).query)
        assert parse_search_query(parameters[parameter][0]) == parse_search_query(text)
        assert parameters["revision"] == ["7"]
        second = await client.get(next_url)
        third = await client.get(_link(second, "next"))
        changed = await client.get(
            f"/opds/{version}/search",
            params={
                parameter: "pages:0",
                "cursor": parameters["cursor"][0],
                "revision": "7",
            },
        )
    assert _identifiers(first) + _identifiers(second) + _identifiers(third) == (
        "urn:h2h:gallery:1001",
        "urn:h2h:gallery:1002",
        "urn:h2h:gallery:1003",
    )
    assert changed.status_code == 422


@pytest.mark.parametrize("counts", ((39, 40, 200), (40, 200, 201)))
async def test_pages_40_through_200_include_both_bounds(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    version: str,
    counts: tuple[int, int, int],
) -> None:
    alpha = catalog_fixture.publications[0]
    publications = tuple(
        replace(
            publication, page_count=count, cover=alpha.cover, thumbnail=alpha.thumbnail
        )
        for publication, count in zip(catalog_fixture.publications, counts, strict=True)
    )
    app = create_app(opds_config, FakeCatalog(publications))
    async with app_client(app) as client:
        response = await client.get(
            f"/opds/{version}/search",
            params={_search_parameter(version): "pages:40..200"},
        )
    assert _identifiers(response) == tuple(
        publication.publication_id
        for publication in publications
        if 40 <= publication.page_count <= 200
    )


async def test_multitag_http_and_facet_links_preserve_other_search_conditions(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig, version: str
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        subjects=(
            CatalogSubject(name="a  b", scheme="tag", code="artist"),
            CatalogSubject(name="chinese", scheme="tag", code="language"),
            CatalogSubject(name="extra", scheme="tag", code="other"),
        ),
    )
    catalog = FakeCatalog((publication,))
    app = create_app(opds_config, catalog)
    parameter = _search_parameter(version)
    text = "title:Alpha gid:1001 tag:language:chinese pages:1 uploaded:2026-08-05"
    expected = parse_search_query(text)
    async with app_client(app) as client:
        response = await client.get(
            f"/opds/{version}/search",
            params={parameter: text, "tag_namespace": "artist", "tag": "a  b"},
        )
        assert _identifiers(response) == (publication.publication_id,)
        assert catalog.list_calls[0][0].subjects == (
            CatalogSubjectFilter("artist", "a  b"),
            CatalogSubjectFilter("language", "chinese"),
        )
        facets = _tag_facet_links(response)
        for title, subjects in (
            ("All", ()),
            ("extra", (CatalogSubjectFilter("other", "extra"),)),
        ):
            parameters = parse_qs(urlsplit(facets[title]).query)
            assert parse_search_query(parameters[parameter][0]) == replace(
                expected, subjects=subjects
            )
            assert _identifiers(await client.get(facets[title])) == (
                publication.publication_id,
            )


async def test_maximum_exact_tag_and_long_text_have_followable_generated_links(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig, version: str
) -> None:
    tag = "x" * 1024
    text = " ".join(["abc"] * 256)
    publications = tuple(
        replace(
            publication,
            title=text,
            subjects=(CatalogSubject(name=tag, scheme="tag", code="f"),),
        )
        for publication in catalog_fixture.publications
    )
    config = opds_config.model_copy(update={"default_page_size": 1})
    app = create_app(config, FakeCatalog(publications))
    parameter = _search_parameter(version)
    async with app_client(app) as client:
        exact = await client.get(
            f"/opds/{version}/publications", params={"tag_namespace": "f", "tag": tag}
        )
        assert _identifiers(exact) == (publications[0].publication_id,)
        assert (await client.get(_link(exact, "self"))).content == exact.content
        assert _identifiers(await client.get(_link(exact, "next"))) == (
            publications[1].publication_id,
        )
        searched = await client.get(
            f"/opds/{version}/search", params={parameter: f'"{text}"'}
        )
        assert _identifiers(searched) == (publications[0].publication_id,)
        selected = await client.get(_tag_facet_links(searched)[tag])
        assert _identifiers(selected) == (publications[0].publication_id,)
        next_url = _link(selected, "next")
        combined = parse_search_query(parse_qs(urlsplit(next_url).query)[parameter][0])
        assert combined.search == text
        assert combined.subjects == (CatalogSubjectFilter("f", tag),)
        assert _identifiers(await client.get(next_url)) == (
            publications[1].publication_id,
        )
