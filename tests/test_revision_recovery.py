from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree

import pytest
from h2hdb import CatalogDiscoveryBundle, CatalogRevision, CatalogRevisionNotFoundError
from httpx import Response

from h2hdb_opds import OPDSConfig, create_app
from h2hdb_opds.discovery import discovery_query

from .fakes import ALPHA_ARTIFACT_ID, CatalogFixture
from .http_client import app_client

_NAVIGATION_ROUTES: tuple[tuple[str, dict[str, str]], ...] = (
    ("/opds/v1.2/catalog", {}),
    ("/opds/v1.2/publications", {}),
    ("/opds/v1.2/search", {"q": "cobalt"}),
    ("/opds/v1.2/facets/language", {}),
    ("/opds/v1.2/recent/uploaded", {}),
    ("/opds/v1.2/recent/downloaded", {}),
    ("/opds/v1.2/opensearch.xml", {}),
    ("/opds/v2", {}),
    ("/opds/v2/publications", {}),
    ("/opds/v2/search", {"query": "cobalt"}),
    ("/opds/v2/facets/language", {}),
    ("/opds/v2/recent/uploaded", {}),
    ("/opds/v2/recent/downloaded", {}),
)


def _next_link(response: Response) -> str:
    if response.headers["content-type"].startswith("application/atom+xml"):
        root = ElementTree.fromstring(response.content)
        return next(
            link.attrib["href"]
            for link in root.findall("{http://www.w3.org/2005/Atom}link")
            if link.attrib["rel"] == "next"
        )
    href = next(
        link["href"] for link in response.json()["links"] if link["rel"] == "next"
    )
    assert isinstance(href, str)
    return href


@pytest.mark.parametrize(("path", "parameters"), _NAVIGATION_ROUTES)
async def test_navigation_recovers_only_after_a_new_revision_is_published(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
    parameters: dict[str, str],
) -> None:
    catalog = catalog_fixture.catalog
    app = create_app(opds_config, catalog)
    async with app_client(app) as client:
        current = await client.get(path, params={**parameters, "revision": "7"})
        catalog.revision = replace(catalog.revision, revision=8)
        stale = await client.get(path, params={**parameters, "revision": "7"})
        recovered = await client.get(stale.headers["location"])

    assert current.status_code == recovered.status_code == 200
    assert stale.status_code == 303
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (current, stale, recovered)
    )
    destination = urlsplit(stale.headers["location"])
    assert destination.netloc == "catalog.example"
    assert destination.path == path
    assert parse_qs(destination.query) == {
        key: [value] for key, value in parameters.items()
    }


@pytest.mark.parametrize(
    ("path", "parameters"),
    tuple(item for item in _NAVIGATION_ROUTES if item[0].startswith("/opds/v1.2/")),
)
async def test_opds12_head_recovers_without_a_body(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
    parameters: dict[str, str],
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.head(path, params={**parameters, "revision": "6"})
        recovered = await client.head(response.headers["location"])
    assert response.status_code == 303
    assert recovered.status_code == 200
    assert response.content == recovered.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert recovered.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("prefix", ("/opds/v1.2", "/opds/v2"))
@pytest.mark.parametrize("resource", ("publications", "facets/language"))
async def test_stale_next_link_restarts_first_page_and_preserves_limit(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    prefix: str,
    resource: str,
) -> None:
    catalog = catalog_fixture.catalog
    app = create_app(opds_config, catalog)
    async with app_client(app) as client:
        first = await client.get(f"{prefix}/{resource}", params={"limit": 1})
        next_link = _next_link(first)
        catalog.revision = replace(catalog.revision, revision=8)
        stale = await client.get(next_link)
        cursor_only = await client.get(
            f"{prefix}/{resource}",
            params={"cursor": parse_qs(urlsplit(next_link).query)["cursor"][0]},
        )
        recovered = await client.get(stale.headers["location"])

    assert stale.status_code == 303
    assert recovered.status_code == 200
    assert parse_qs(urlsplit(stale.headers["location"]).query) == {"limit": ["1"]}
    assert cursor_only.status_code == 404
    assert "location" not in cursor_only.headers
    if resource == "publications":
        assert catalog.list_calls[-1][1:] == (None, 1)
    else:
        assert catalog.facet_calls[-1][2:] == (None, 1)


@pytest.mark.parametrize(
    ("prefix", "search_parameter"), (("/opds/v1.2", "q"), ("/opds/v2", "query"))
)
@pytest.mark.parametrize("resource", ("publications", "search", "facets/subject"))
async def test_recovery_preserves_exact_filters_through_canonical_search(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    prefix: str,
    search_parameter: str,
    resource: str,
) -> None:
    search = None if resource == "publications" else 'title:cobalt female:"a  b"'
    filters = {
        "language": " zh ",
        "tag": 'm\u0301  &"/值',
        "tag_namespace": "name:space",
        "contributor": " A  & B ",
        "role": "artist",
    }
    parameters = {**filters, "revision": "6", "limit": "2"}
    if search is not None:
        parameters[search_parameter] = search
    expected = discovery_query(search=search, **filters)
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.get(f"{prefix}/{resource}", params=parameters)
        recovered = await client.get(response.headers["location"])

    assert response.status_code == 303
    assert recovered.status_code == 200
    destination = urlsplit(response.headers["location"])
    assert destination.path == (
        f"{prefix}/facets/subject"
        if resource.startswith("facets/")
        else f"{prefix}/search"
    )
    selected = parse_qs(destination.query)
    assert selected["limit"] == ["2"]
    assert "revision" not in selected
    assert "cursor" not in selected
    assert "tag" not in selected
    actual = (
        catalog_fixture.catalog.facet_calls[-1][1]
        if resource.startswith("facets/")
        else catalog_fixture.catalog.list_calls[-1][0]
    )
    assert actual == expected


@pytest.mark.parametrize("path", ("/opds/v1.2/catalog", "/opds/v2"))
async def test_recovery_uses_configured_canonical_prefix_not_host(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
) -> None:
    config = opds_config.model_copy(
        update={"public_base_url": "https://trusted.example/library"}
    )
    app = create_app(config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.get(
            path, params={"revision": 6}, headers={"Host": "attacker.example"}
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"https://trusted.example/library{path}"


@pytest.mark.parametrize(
    ("path", "parameters", "status"),
    (
        ("/opds/v1.2/search", {"q": "tag:f:fantasy"}, 422),
        ("/opds/v2/search", {"query": "cobalt", "q": "cobalt"}, 422),
        ("/opds/v2/search", {"query": ""}, 422),
        ("/opds/v1.2/publications", {"tag": "fantasy"}, 422),
        ("/opds/v2/publications", {"offset": "1"}, 422),
        ("/opds/v1.2/recent/uploaded", {"cursor": "bad"}, 422),
        ("/opds/v2/recent/downloaded", {"limit": "1"}, 422),
        ("/opds/v1.2/opensearch.xml", {"limit": "1"}, 422),
        ("/opds/v2/facets/unknown", {}, 404),
        ("/opds/v1.2/publications", {"cursor": "bad"}, 404),
        ("/opds/v2/facets/language", {"cursor": "bad"}, 404),
    ),
)
async def test_invalid_navigation_is_not_hidden_by_recovery(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
    parameters: dict[str, str],
    status: int,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.get(path, params={"revision": "6", **parameters})
    assert response.status_code == status
    assert "location" not in response.headers


@pytest.mark.parametrize("revision", ("0", "-1", "bad", str(2**63), "8"))
@pytest.mark.parametrize("path", ("/opds/v1.2/catalog", "/opds/v2"))
async def test_future_and_malformed_revisions_do_not_redirect(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    revision: str,
    path: str,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.get(path, params={"revision": revision})
    assert response.status_code == (404 if revision == "8" else 422)
    assert "location" not in response.headers


@pytest.mark.parametrize("path", ("/opds/v1.2/catalog", "/opds/v2"))
async def test_missing_current_head_remains_unavailable(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    def missing_head(revision: int | None = None) -> CatalogRevision:
        raise CatalogRevisionNotFoundError(7 if revision is None else revision)

    monkeypatch.setattr(catalog_fixture.catalog, "get_catalog_revision", missing_head)
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.get(path, params={"revision": 6})
    assert response.status_code == 404
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("prefix", ("/opds/v1.2", "/opds/v2"))
async def test_activation_is_not_redirected_and_recovers_after_unlock(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    prefix: str,
) -> None:
    marker = opds_config.coordination_root / "ACTIVATING"
    marker.touch()
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        busy = await client.get(f"{prefix}/publications", params={"revision": 6})
        health = await client.get("/health")
        marker.unlink()
        recovered = await client.get(f"{prefix}/publications", params={"revision": 6})
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "1"
    assert busy.headers["cache-control"] == "no-store"
    assert "location" not in busy.headers
    assert health.status_code == 200
    assert recovered.status_code == 303


async def test_recovery_does_not_override_cursor_mismatch_or_configured_limit(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 1, "maximum_page_size": 1}
    )
    app = create_app(config, catalog_fixture.catalog)
    async with app_client(app) as client:
        first = await client.get("/opds/v2/facets/language", params={"limit": 1})
        parameters = {
            key: values[0]
            for key, values in parse_qs(urlsplit(_next_link(first)).query).items()
        }
        catalog_fixture.catalog.revision = replace(
            catalog_fixture.catalog.revision, revision=8
        )
        wrong_facet = await client.get("/opds/v2/facets/subject", params=parameters)
        wrong_revision = await client.get(
            "/opds/v2/facets/language", params={**parameters, "revision": "6"}
        )
        over_limit = await client.get(
            "/opds/v1.2/publications", params={"revision": 7, "limit": 2}
        )
    for response in (wrong_facet, wrong_revision, over_limit):
        assert response.status_code == 404
        assert "location" not in response.headers


async def test_activation_during_recovery_head_recheck_remains_503(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = catalog_fixture.catalog.discover_publications_with_facets

    def activate_after_stale_read(**kwargs: Any) -> CatalogDiscoveryBundle:
        try:
            return original(**kwargs)
        except CatalogRevisionNotFoundError:
            (opds_config.coordination_root / "ACTIVATING").touch()
            raise

    monkeypatch.setattr(
        catalog_fixture.catalog,
        "discover_publications_with_facets",
        activate_after_stale_read,
    )
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.get("/opds/v2/publications", params={"revision": 6})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "path",
    (
        "/opds/v1.2/publications/urn:h2h:gallery:1001",
        "/opds/v2/publications/urn:h2h:gallery:1001",
        f"/opds/v1.2/acquisitions/{ALPHA_ARTIFACT_ID}",
        f"/opds/v2/acquisitions/{ALPHA_ARTIFACT_ID}",
        "/media/publications/urn:h2h:gallery:1001/pages/0",
        "/media/publications/urn:h2h:gallery:1001/thumbnail",
    ),
)
async def test_stale_publication_and_bytes_remain_strictly_pinned(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.get(f"{path}?{urlencode({'revision': 6})}")
        current = await client.get(path)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    assert current.status_code == 200
    if "/acquisitions/" in path or path.startswith("/media/"):
        assert "no-store" not in current.headers.get("cache-control", "")
        assert "etag" in current.headers
    else:
        assert current.headers["cache-control"] == "no-store"
