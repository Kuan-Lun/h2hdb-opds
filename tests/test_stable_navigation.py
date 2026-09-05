from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import pytest
from httpx import Response

from h2hdb_opds import OPDSConfig, create_app
from h2hdb_opds.atom import ATOM_NAMESPACE
from h2hdb_opds.auth import AUTHENTICATION_DOCUMENT_REL

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client

_ATOM = f"{{{ATOM_NAMESPACE}}}"


def _feed_links(response: Response) -> list[tuple[str, str]]:
    assert response.status_code == 200
    if response.headers["content-type"].startswith("application/atom+xml"):
        root = ElementTree.fromstring(response.content)
        return [
            (link.attrib["rel"], link.attrib["href"])
            for link in root.findall(f"{_ATOM}link")
        ]
    return [(link["rel"], link["href"]) for link in response.json()["links"]]


def _content_hrefs(response: Response) -> list[str]:
    if response.headers["content-type"].startswith("application/atom+xml"):
        root = ElementTree.fromstring(response.content)
        return [
            link.attrib["href"] for link in root.findall(f"{_ATOM}entry/{_ATOM}link")
        ]
    document = response.json()
    return [
        *(
            link["href"]
            for group in document.get("groups", [])
            for link in group["navigation"]
        ),
        *(link["href"] for link in document.get("navigation", [])),
        *(
            link["href"]
            for publication in document.get("publications", [])
            for link in [*publication["links"], *publication.get("images", [])]
        ),
        *(
            link["href"]
            for facet in document.get("facets", [])
            for link in facet["links"]
        ),
    ]


def _assert_revision(href: str, revision: int) -> None:
    # Search and PSE links contain URI-template expressions after the query.
    query = urlsplit(href.split("{", maxsplit=1)[0]).query
    assert parse_qs(query)["revision"] == [str(revision)]


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("explicit_revision", [False, True])
async def test_saved_root_links_open_current_catalog_after_publication(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    explicit_revision: bool,
) -> None:
    root_path = f"/opds/{protocol}" + ("/catalog" if protocol == "v1.2" else "")
    expected_root = f"http://catalog.example{root_path}"
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        original = await client.get(
            root_path,
            params={"revision": "7"} if explicit_revision else {},
            headers={"Host": "attacker.invalid"},
        )
        links = dict(_feed_links(original))
        assert links["self"] == links["start"] == expected_root
        children = _content_hrefs(original)
        assert len(children) >= 3
        for href in [*children, links["search"]]:
            _assert_revision(href, 7)

        catalog_fixture.catalog.add_revision(
            replace(catalog_fixture.catalog.revision, revision=8),
            catalog_fixture.publications,
        )
        refreshed = await client.get(links["self"])

    refreshed_links = dict(_feed_links(refreshed))
    assert refreshed_links["self"] == refreshed_links["start"] == expected_root
    for href in [*_content_hrefs(refreshed), refreshed_links["search"]]:
        _assert_revision(href, 8)


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "surface",
    [
        "publications",
        "search",
        "facets/language",
        "recent/uploaded",
        "recent/downloaded",
    ],
)
async def test_feed_home_links_are_stable_and_other_links_remain_pinned(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    empty: bool,
    surface: str,
) -> None:
    catalog = FakeCatalog(()) if empty else catalog_fixture.catalog
    app = create_app(opds_config, catalog)
    root_path = f"/opds/{protocol}" + ("/catalog" if protocol == "v1.2" else "")
    parameters = {"revision": "7"}
    if surface == "search":
        parameters["q" if protocol == "v1.2" else "query"] = "cobalt"
    if not surface.startswith("recent/"):
        parameters["limit"] = "1"

    async with app_client(app) as client:
        response = await client.get(
            f"/opds/{protocol}/{surface}",
            params=parameters,
            headers={"Host": "attacker.invalid"},
        )

    links = _feed_links(response)
    assert ("start", f"http://catalog.example{root_path}") in links
    if protocol == "v1.2" and not surface.startswith("facets/"):
        assert ("up", f"http://catalog.example{root_path}") in links
    pinned = [
        href
        for relation, href in links
        if relation not in {"start", "up", AUTHENTICATION_DOCUMENT_REL}
    ]
    assert pinned
    for href in [*pinned, *_content_hrefs(response)]:
        # PSE templates put {pageNumber} in the path, before the query.
        _assert_revision(href.replace("{pageNumber}", "0"), 7)
    if not empty and surface in {"publications", "search", "facets/language"}:
        assert "next" in dict(links)
