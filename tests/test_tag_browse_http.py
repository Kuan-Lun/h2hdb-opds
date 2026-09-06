from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import pytest
from h2hdb import (
    CatalogDiscoveryPage,
    CatalogReadError,
    CatalogRevision,
    CatalogSubject,
    CatalogTagPage,
)
from httpx import Response

from h2hdb_opds import OPDSConfig, create_app
from h2hdb_opds.atom import ATOM_NAMESPACE
from h2hdb_opds.browse import BrowseTarget
from h2hdb_opds.cursor import decode_tag_cursor, encode_tag_cursor

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client
from .test_opds_conformance import _assert_valid_atom, _opds2_validation

_ATOM = f"{{{ATOM_NAMESPACE}}}"
_SPECIAL_TAG = ' Older  名/字 & "quoted" '
_DIRECTORIES = {
    "artists": "artist",
    "groups": "group",
    "parodies": "parody",
    "characters": "character",
}
_CATEGORIES = (
    *_DIRECTORIES,
    "soushuuhen",
    "multi-work-series",
    "uncensored",
    "goudoushi",
)


def _links(response: Response) -> dict[str, str]:
    if response.headers["content-type"].startswith("application/atom+xml"):
        return {
            link.attrib["rel"]: link.attrib["href"]
            for link in ElementTree.fromstring(response.content).findall(f"{_ATOM}link")
        }
    return {link["rel"]: link["href"] for link in response.json()["links"]}


def _titles(response: Response) -> list[str | None]:
    if response.headers["content-type"].startswith("application/atom+xml"):
        return [
            entry.findtext(f"{_ATOM}title")
            for entry in ElementTree.fromstring(response.content).findall(
                f"{_ATOM}entry"
            )
        ]
    document = response.json()
    if "publications" in document:
        return [item["metadata"]["title"] for item in document["publications"]]
    return [item["title"] for item in document.get("navigation", [])]


def _children(response: Response) -> list[str]:
    if response.headers["content-type"].startswith("application/atom+xml"):
        return [
            link.attrib["href"]
            for entry in ElementTree.fromstring(response.content).findall(
                f"{_ATOM}entry"
            )
            for link in entry.findall(f"{_ATOM}link")
            if link.attrib["rel"] == "subsection"
        ]
    return [item["href"] for item in response.json()["navigation"]]


def _catalog(fixture: CatalogFixture) -> FakeCatalog:
    template = fixture.publications[0]
    # Deliberately unsorted. The last publication has contributor metadata but
    # no matching subject tags, so it must not enter any directory or the
    # other:goudoushi entry through a same-named namespace.
    records = (
        (2001, "Zulu", "Beta", 2),
        (2002, "Alpha", "Alpha", 2),
        (2003, "Older", "Alpha", 0),
        (2004, "Apple", "Beta", 2),
        (2005, "Apple", "Beta", 2),
        (2006, "Special", _SPECIAL_TAG, 1),
        (2007, "Contributor only", None, 3),
    )
    return FakeCatalog(
        tuple(
            replace(
                template,
                gid=gid,
                publication_id=f"urn:h2h:gallery:{gid}",
                title=title,
                sort_title=title.casefold(),
                published_at=template.published_at + timedelta(days=days),
                artifacts=(
                    replace(
                        template.artifacts[0],
                        artifact_id=(
                            f"urn:h2h:artifact:acquisition:{gid}:sha256:"
                            f"{template.artifacts[0].storage_object.sha256}"
                        ),
                    ),
                ),
                subjects=(
                    (CatalogSubject(name="goudoushi", code="goudoushi", scheme="tag"),)
                    if artist is None
                    else (
                        *(
                            CatalogSubject(name=artist, code=namespace, scheme="tag")
                            for namespace in _DIRECTORIES.values()
                        ),
                        *(
                            CatalogSubject(name=tag, code="other", scheme="tag")
                            for tag in (
                                "soushuuhen",
                                "multi-work series",
                                "uncensored",
                                "goudoushi",
                            )
                        ),
                    )
                ),
            )
            for gid, title, artist, days in records
        )
    )


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("category", _DIRECTORIES)
async def test_directory_uses_only_its_exact_subject_namespace(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
) -> None:
    namespace = _DIRECTORIES[category]
    wrong_namespace = "artist" if namespace != "artist" else "parody"
    catalog = FakeCatalog(
        (
            replace(
                catalog_fixture.publications[0],
                subjects=(CatalogSubject(name="Shared", code=namespace),),
            ),
            replace(
                catalog_fixture.publications[1],
                subjects=(CatalogSubject(name="Shared", code=wrong_namespace),),
            ),
            replace(
                catalog_fixture.publications[2],
                subjects=(
                    CatalogSubject(name="Wrong namespace", code=wrong_namespace),
                ),
            ),
        )
    )
    async with app_client(create_app(opds_config, catalog)) as client:
        directory = await client.get(f"/opds/{protocol}/browse/{category}")
        assert _titles(directory) == ["Shared"]
        publications = await client.get(_children(directory)[0])
    assert _titles(publications) == ["Alpha Gallery"]
    assert [name for name, _, _ in catalog.tag_calls] == [namespace, namespace]


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
async def test_goudoushi_uses_only_exact_other_tag(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig, protocol: str
) -> None:
    catalog = FakeCatalog(
        tuple(
            replace(publication, subjects=(CatalogSubject(name=value, code=namespace),))
            for publication, (namespace, value) in zip(
                catalog_fixture.publications,
                (
                    ("other", "goudoushi"),
                    ("goudoushi", "goudoushi"),
                    ("other", "Goudoushi"),
                ),
                strict=True,
            )
        )
    )
    async with app_client(create_app(opds_config, catalog)) as client:
        publications = await client.get(f"/opds/{protocol}/browse/goudoushi")
    assert _titles(publications) == ["Alpha Gallery"]
    assert [name for name, _, _ in catalog.tag_calls] == ["other"]


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("category", _DIRECTORIES)
async def test_tag_directories_page_and_link_exact_tags(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
) -> None:
    catalog = _catalog(catalog_fixture)
    app = create_app(opds_config, catalog)
    async with app_client(app) as client:
        page = await client.get(f"/opds/{protocol}/browse/{category}?limit=1")
        seen = []
        for expected in ("Alpha", "Beta", _SPECIAL_TAG):
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            assert _titles(page) == [expected]
            child = _children(page)[0]
            assert parse_qs(urlsplit(child).query)["tag"] == [expected]
            result = await client.get(child)
            assert result.status_code == 200
            assert len(_titles(result)) == 1
            assert "first" in _links(page)
            seen.extend(_titles(page))
            if expected != _SPECIAL_TAG:
                page = await client.get(_links(page)["next"])
        assert "next" not in _links(page)
        restarted = await client.get(_links(page)["first"])
        assert _titles(restarted) == ["Alpha"]
    assert seen == ["Alpha", "Beta", _SPECIAL_TAG]
    assert not catalog.facet_calls and not catalog.list_calls
    assert all(
        limit == 1 and revision == catalog.revision
        for _, limit, revision in catalog.tag_calls
    )


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("category", _CATEGORIES)
async def test_tag_publications_keep_core_order_across_pages(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
) -> None:
    catalog = _catalog(catalog_fixture)
    app = create_app(opds_config, catalog)
    parameters: dict[str, str | int] = {"limit": 2}
    if category in _DIRECTORIES:
        parameters["tag"] = "Beta"
        expected = ["Apple", "Apple", "Zulu"]
    else:
        expected = ["Alpha", "Apple", "Apple", "Zulu", "Special", "Older"]
    async with app_client(app) as client:
        page = await client.get(
            f"/opds/{protocol}/browse/{category}", params=parameters
        )
        seen = []
        while True:
            assert page.status_code == 200
            assert len(_titles(page)) <= 2
            seen.extend(_titles(page))
            links = _links(page)
            assert "revision=7" in links["self"]
            if "next" not in links:
                break
            page = await client.get(links["next"])
    assert seen == expected
    assert not catalog.list_calls and not catalog.facet_calls


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("category", _DIRECTORIES)
async def test_directory_default_window_is_fifty_and_next_is_followable(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
) -> None:
    namespace = _DIRECTORIES[category]
    template = catalog_fixture.publications[0]
    catalog = FakeCatalog(
        tuple(
            replace(
                template,
                gid=3000 + index,
                publication_id=f"urn:h2h:gallery:{3000 + index}",
                artifacts=(
                    replace(
                        template.artifacts[0],
                        artifact_id=(
                            f"urn:h2h:artifact:acquisition:{3000 + index}:sha256:"
                            f"{template.artifacts[0].storage_object.sha256}"
                        ),
                    ),
                ),
                subjects=(CatalogSubject(name=f"Name {index:03}", code=namespace),),
            )
            for index in range(53)
        )
    )
    async with app_client(create_app(opds_config, catalog)) as client:
        first = await client.get(f"/opds/{protocol}/browse/{category}")
        assert first.status_code == 200
        assert len(_titles(first)) == 50
        second = await client.get(_links(first)["next"])
    assert _titles(second) == ["Name 050", "Name 051", "Name 052"]
    assert "next" not in _links(second)
    assert [limit for _, limit, _ in catalog.tag_calls] == [50, 50]


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("category", _CATEGORIES)
async def test_empty_and_nonempty_browse_feeds_conform_to_official_schemas(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
) -> None:
    for catalog in (_catalog(catalog_fixture), FakeCatalog(())):
        async with app_client(create_app(opds_config, catalog)) as client:
            response = await client.get(f"/opds/{protocol}/browse/{category}")
        assert response.status_code == 200
        if protocol == "v1.2":
            _assert_valid_atom(response.content)
        else:
            result = _opds2_validation("feed", response.json())
            assert result.returncode == 0, result.stderr
            assert response.json().get("publications") != []


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize(
    ("category", "query", "status"),
    [
        ("unknown", "", 404),
        ("artists", "limit=0", 422),
        ("artists", "limit=129", 422),
        ("artists", "offset=1", 422),
        ("artists", "cursor=invalid", 422),
        ("uncensored", "tag=unexpected", 422),
        ("goudoushi", "tag=unexpected", 422),
        ("parodies", "limit=129", 422),
        ("characters", "offset=1", 422),
        ("groups", "revision=8", 404),
    ],
)
async def test_browse_rejects_invalid_requests(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
    query: str,
    status: int,
) -> None:
    async with app_client(create_app(opds_config, _catalog(catalog_fixture))) as client:
        response = await client.get(f"/opds/{protocol}/browse/{category}?{query}")
    assert response.status_code == status
    if status == 404:
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("other_category", ("groups", "parodies", "characters"))
async def test_directory_cursor_cannot_cross_namespace_or_forge_boundary(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    other_category: str,
) -> None:
    async with app_client(create_app(opds_config, _catalog(catalog_fixture))) as client:
        first = await client.get(f"/opds/{protocol}/browse/artists?limit=1")
        query = parse_qs(urlsplit(_links(first)["next"]).query)
        cursor = query["cursor"][0]
        cross = await client.get(
            f"/opds/{protocol}/browse/{other_category}", params={"cursor": cursor}
        )
        forged = encode_tag_cursor(
            replace(decode_tag_cursor(cursor), value_sha256="0" * 64)
        )
        invalid = await client.get(
            f"/opds/{protocol}/browse/artists", params={"cursor": forged}
        )
    assert cross.status_code == invalid.status_code == 422


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize(
    ("category", "tag"),
    [
        *((category, tag) for category in _DIRECTORIES for tag in (None, _SPECIAL_TAG)),
        ("goudoushi", None),
    ],
)
async def test_stale_browse_revision_restarts_exact_selection_and_drops_cursor(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
    tag: str | None,
) -> None:
    catalog = _catalog(catalog_fixture)
    parameters = {"revision": "7", "limit": "1"}
    if tag is not None:
        parameters["tag"] = tag
    async with app_client(create_app(opds_config, catalog)) as client:
        first = await client.get(
            f"/opds/{protocol}/browse/{category}", params=parameters
        )
        stale_url = _links(first).get("next", _links(first)["self"])
        catalog.add_revision(
            replace(catalog.revision, revision=8), catalog.publications
        )
        stale = await client.get(
            stale_url, follow_redirects=False, headers={"Host": "attacker.invalid"}
        )
        assert stale.status_code == 303
        assert stale.headers["cache-control"] == "no-store"
        location = stale.headers["location"]
        assert location.startswith(
            f"http://catalog.example/opds/{protocol}/browse/{category}?"
        )
        parameters.pop("revision")
        assert parse_qs(urlsplit(location).query) == {
            key: [value] for key, value in parameters.items()
        }
        refreshed = await client.get(location)
    assert refreshed.status_code == 200
    assert "revision=8" in _links(refreshed)["self"]


async def test_opds12_browse_head_matches_get_headers(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    async with app_client(create_app(opds_config, _catalog(catalog_fixture))) as client:
        for path in _CATEGORIES:
            get = await client.get(f"/opds/v1.2/browse/{path}")
            head = await client.head(f"/opds/v1.2/browse/{path}")
            assert get.status_code == head.status_code == 200
            assert head.content == b""
            assert head.headers["content-length"] == str(len(get.content))
            assert head.headers["content-type"] == get.headers["content-type"]


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("tag", ["", "漢" * 400], ids=["empty", "over-search-budget"])
@pytest.mark.parametrize("category", _DIRECTORIES)
async def test_browse_preserves_empty_and_long_source_tags(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    tag: str,
    category: str,
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        subjects=(CatalogSubject(name=tag, code=_DIRECTORIES[category], scheme="tag"),),
    )
    catalog = FakeCatalog((publication,))
    async with app_client(create_app(opds_config, catalog)) as client:
        directory = await client.get(f"/opds/{protocol}/browse/{category}")
        assert directory.status_code == 200
        assert _titles(directory) == [tag or "(empty tag)"]
        child = _children(directory)[0]
        assert parse_qs(urlsplit(child).query, keep_blank_values=True)["tag"] == [tag]
        response = await client.get(child)
    assert response.status_code == 200
    assert _titles(response) == [publication.title]


@pytest.mark.parametrize("category", _DIRECTORIES)
def test_browse_tag_boundary_uses_source_utf8_budget(category: str) -> None:
    maximum = "漢" * 21845 + "x"
    subject = BrowseTarget(category, maximum).subject
    assert subject is not None and subject.value == maximum
    with pytest.raises(ValueError):
        BrowseTarget(category, maximum + "x")


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
async def test_publication_cursor_cannot_cross_tag_or_discovery_domain(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
) -> None:
    async with app_client(create_app(opds_config, _catalog(catalog_fixture))) as client:
        first = await client.get(f"/opds/{protocol}/browse/soushuuhen?limit=1")
        cursor = parse_qs(urlsplit(_links(first)["next"]).query)["cursor"][0]
        for path in (
            "browse/uncensored",
            "browse/goudoushi",
            "publications",
            "browse/artists",
            "browse/parodies",
            "browse/characters",
        ):
            response = await client.get(
                f"/opds/{protocol}/{path}", params={"cursor": cursor}
            )
            assert response.status_code == 422


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
async def test_browse_respects_configured_page_cap_and_activation_lock(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    catalog = _catalog(catalog_fixture)
    async with app_client(create_app(config, catalog)) as client:
        too_large = await client.get(f"/opds/{protocol}/browse/artists?limit=3")
        assert too_large.status_code == 422
        assert not catalog.tag_calls
        (config.coordination_root / "ACTIVATING").touch()
        unavailable = await client.get(f"/opds/{protocol}/browse/artists")
    assert unavailable.status_code == 503
    assert unavailable.headers["retry-after"] == "1"
    assert unavailable.headers["cache-control"] == "no-store"
    assert not catalog.tag_calls


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize("directory", [False, True])
async def test_browse_fails_closed_when_reader_returns_wrong_page_family(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    directory: bool,
) -> None:
    catalog = _catalog(catalog_fixture)

    def wrong_page(**_: object) -> CatalogTagPage | CatalogDiscoveryPage:
        if directory:
            return CatalogDiscoveryPage(
                revision=catalog.revision,
                publications=catalog.publications[:1],
                next_cursor=None,
                limit=50,
                total=None,
            )
        return CatalogTagPage(
            revision=catalog.revision,
            namespace="other",
            values=(),
            next_cursor=None,
            limit=50,
        )

    method = (
        "list_tag_values_with_publications" if directory else "list_tag_publications"
    )
    monkeypatch.setattr(catalog, method, wrong_page)
    category = "artists" if directory else "uncensored"
    async with app_client(create_app(opds_config, catalog)) as client:
        response = await client.get(f"/opds/{protocol}/browse/{category}")
    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
@pytest.mark.parametrize(
    ("method", "category"),
    [
        ("get_catalog_revision", "artists"),
        ("get_catalog_revision", "uncensored"),
        ("list_tag_values_with_publications", "artists"),
        ("list_tag_publications", "uncensored"),
    ],
)
async def test_browse_maps_public_core_read_errors_to_integrity_responses(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    method: str,
    category: str,
) -> None:
    catalog = _catalog(catalog_fixture)

    def corrupt_read(*_args: object, **_kwargs: object) -> object:
        raise CatalogReadError("sealed catalog authority is corrupt")

    monkeypatch.setattr(catalog, method, corrupt_read)
    async with app_client(create_app(opds_config, catalog)) as client:
        response = await client.get(f"/opds/{protocol}/browse/{category}")
    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert response.headers["cache-control"] == "no-store"
    assert "retry-after" not in response.headers
    assert "location" not in response.headers


@pytest.mark.parametrize("protocol", ["v1.2", "v2"])
async def test_browse_recovery_rejects_corrupt_fresh_head(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    catalog = _catalog(catalog_fixture)
    catalog.add_revision(replace(catalog.revision, revision=8), catalog.publications)
    calls = 0

    def corrupt_recovery_head(_revision: int | None = None) -> CatalogRevision:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise CatalogReadError("refreshed catalog authority is corrupt")
        return catalog.revision

    monkeypatch.setattr(catalog, "get_catalog_revision", corrupt_recovery_head)
    async with app_client(create_app(opds_config, catalog)) as client:
        response = await client.get(
            f"/opds/{protocol}/browse/artists?revision=7", follow_redirects=False
        )
    assert calls == 2
    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert response.headers["cache-control"] == "no-store"
    assert "retry-after" not in response.headers
    assert "location" not in response.headers
