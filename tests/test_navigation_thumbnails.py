from dataclasses import replace
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree

import pytest
from h2hdb import (
    CatalogPublication,
    CatalogRevision,
    CatalogRevisionNotFoundError,
    CatalogSubject,
    CatalogTagBundle,
    CatalogTagCursor,
)
from httpx import Response

from h2hdb_opds import OPDSConfig, create_app
from h2hdb_opds.atom import ATOM_NAMESPACE
from h2hdb_opds.browse import BrowseTarget
from h2hdb_opds.catalog_service import CatalogIntegrityError, CatalogService
from h2hdb_opds.library import LibraryReadCoordinator

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client
from .test_opds_conformance import _assert_valid_atom, _opds2_validation

_ATOM = f"{{{ATOM_NAMESPACE}}}"
_THUMBNAIL_REL = "http://opds-spec.org/image/thumbnail"
_DIRECTORIES = (
    ("artists", "artist"),
    ("groups", "group"),
    ("parodies", "parody"),
    ("characters", "character"),
)


def _publication(
    template: CatalogPublication,
    *,
    gid: int,
    title: str,
    uploaded: int,
    downloaded: int,
    subjects: tuple[tuple[str, str], ...],
) -> CatalogPublication:
    return replace(
        template,
        gid=gid,
        publication_id=f"urn:h2h:gallery:{gid}",
        title=title,
        sort_title=title.casefold(),
        published_at=template.published_at + timedelta(days=uploaded),
        downloaded_at=template.downloaded_at + timedelta(days=downloaded),
        subjects=tuple(
            CatalogSubject(name=value, code=namespace, scheme="tag")
            for namespace, value in subjects
        ),
        artifacts=(
            replace(
                template.artifacts[0],
                artifact_id=template.artifacts[0].artifact_id.replace(
                    f":{template.gid}:", f":{gid}:"
                ),
                name=f"{gid}.cbz",
            ),
        ),
    )


def _catalog(fixture: CatalogFixture) -> FakeCatalog:
    records = (
        (2101, "First in catalog", 1, 9, "old", "Team-old", ""),
        (2102, "Zulu", 5, 1, "Beta", "Team-B", "uncensored"),
        (2103, "Alpha", 5, 2, "Alpha", "Team-A", "soushuuhen"),
        (2104, "apple", 5, 3, "Beta", "Team-B", "uncensored"),
        (2105, "Series", 4, 4, "old", "Team-old", "multi-work series"),
    )
    return FakeCatalog(
        tuple(
            _publication(
                fixture.publications[0],
                gid=gid,
                title=title,
                uploaded=uploaded,
                downloaded=downloaded,
                subjects=(
                    ("artist", artist),
                    ("group", group),
                    ("parody", artist),
                    ("character", artist),
                    ("other", other),
                    ("other", "goudoushi")
                    if other == "uncensored"
                    else ("goudoushi", "goudoushi"),
                ),
            )
            for gid, title, uploaded, downloaded, artist, group, other in records
        )
    )


def _service(catalog: FakeCatalog, config: OPDSConfig) -> CatalogService:
    return CatalogService(
        reader=lambda: catalog,
        library_reads=LibraryReadCoordinator(
            library_root=config.library_root,
            coordination_root=config.coordination_root,
        ),
        default_page_size=config.default_page_size,
        maximum_page_size=config.maximum_page_size,
    )


def _navigation(response: Response) -> dict[str, tuple[str, str | None]]:
    assert response.status_code == 200
    if response.headers["content-type"].startswith("application/atom+xml"):
        root = ElementTree.fromstring(response.content)
        result: dict[str, tuple[str, str | None]] = {}
        for entry in root.findall(f"{_ATOM}entry"):
            title = entry.findtext(f"{_ATOM}title")
            assert title is not None
            links = {
                link.attrib["rel"]: link.attrib
                for link in entry.findall(f"{_ATOM}link")
            }
            image = links.get(_THUMBNAIL_REL)
            if image is not None:
                assert image["type"] == "image/jpeg"
                assert int(image["length"]) > 0
            result[title] = (
                links["subsection"]["href"],
                None if image is None else image["href"],
            )
        return result
    document = response.json()
    items = [
        *document.get("navigation", []),
        *(item for group in document.get("groups", []) for item in group["navigation"]),
    ]
    result = {}
    for item in items:
        assert "images" not in item
        alternates = item.get("alternate", [])
        assert len(alternates) <= 1
        if alternates:
            image = alternates[0]
            assert image["rel"] == "icon" and image["type"] == "image/jpeg"
            assert (image["width"], image["height"]) == (213, 320)
            assert image["size"] > 0
        result[item["title"]] = (
            item["href"],
            None if not alternates else alternates[0]["href"],
        )
    return result


def _thumbnail_gid(href: str | None, *, revision: int = 7, prefix: str = "") -> int:
    assert href is not None
    parsed = urlsplit(href)
    assert parsed.netloc == ("trusted.example" if prefix else "catalog.example")
    assert parse_qs(parsed.query) == {"revision": [str(revision)]}
    path = unquote(parsed.path)
    start = f"{prefix}/media/publications/urn:h2h:gallery:"
    assert path.startswith(start) and path.endswith("/thumbnail")
    return int(path.removeprefix(start).removesuffix("/thumbnail"))


def _next(response: Response) -> str | None:
    if response.headers["content-type"].startswith("application/atom+xml"):
        links = ElementTree.fromstring(response.content).findall(f"{_ATOM}link")
        return next(
            (link.attrib["href"] for link in links if link.attrib["rel"] == "next"),
            None,
        )
    return next(
        (link["href"] for link in response.json()["links"] if link["rel"] == "next"),
        None,
    )


def test_navigation_uses_each_targets_authoritative_first_publication(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig
) -> None:
    catalog = _catalog(catalog_fixture)
    selected = _service(catalog, opds_config).navigation(None)
    assert {
        key: publication.gid for key, publication in selected.publications.items()
    } == {
        "all": 2101,
        "recently-uploaded": 2104,
        "recently-downloaded": 2101,
        "artists": 2103,
        "groups": 2103,
        "parodies": 2103,
        "characters": 2103,
        "soushuuhen": 2103,
        "multi-work-series": 2105,
        "uncensored": 2104,
        "goudoushi": 2104,
    }
    assert [limit for _, _, limit in catalog.list_calls] == [1]
    assert len(catalog.recent_list_calls) == 2
    assert [
        (namespace, limit) for namespace, _, limit, _ in catalog.tag_bundle_calls
    ] == [("artist", 1), ("group", 1), ("parody", 1), ("character", 1)]
    assert [limit for _, limit, _ in catalog.tag_calls] == [1] * 8
    assert all(revision == selected.revision for revision in catalog.list_revisions)
    assert all(revision == selected.revision for _, _, revision in catalog.tag_calls)
    assert (
        not catalog.facet_calls
        and not catalog.publication_revisions
        and not catalog.presentation_revisions
    )


@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
async def test_root_thumbnails_use_canonical_revision_pinned_media_urls(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig, protocol: str
) -> None:
    config = opds_config.model_copy(
        update={"public_base_url": "https://trusted.example/library"}
    )
    path = f"/opds/{protocol}" + ("/catalog" if protocol == "v1.2" else "")
    async with app_client(create_app(config, _catalog(catalog_fixture))) as client:
        response = await client.get(path, headers={"Host": "attacker.invalid"})
    entries = _navigation(response)
    assert {
        title: _thumbnail_gid(image, prefix="/library")
        for title, (_, image) in entries.items()
    } == {
        "All Publications": 2101,
        "Recently Uploaded": 2104,
        "Recently Downloaded": 2101,
        "Artists": 2103,
        "Groups": 2103,
        "Parodies": 2103,
        "Characters": 2103,
        "Soushuuhen": 2103,
        "Multi-work Series": 2105,
        "Uncensored": 2104,
        "Goudoushi": 2104,
    }
    for href, _image in entries.values():
        assert href.startswith("https://trusted.example/library/")
        assert parse_qs(urlsplit(href).query)["revision"] == ["7"]
    assert response.headers["cache-control"] == "no-store"
    if protocol == "v1.2":
        _assert_valid_atom(response.content)
    else:
        result = _opds2_validation("feed", response.json())
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("category", "namespace"), _DIRECTORIES)
@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
async def test_directory_thumbnails_page_fifty_without_per_tag_reads(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    category: str,
    namespace: str,
    protocol: str,
) -> None:
    catalog = FakeCatalog(
        tuple(
            _publication(
                catalog_fixture.publications[0],
                gid=3000 + index,
                title=f"Book {index:02}",
                uploaded=index,
                downloaded=index,
                subjects=((namespace, f"Tag {index:02}"),),
            )
            for index in range(61)
        )
    )
    async with app_client(create_app(opds_config, catalog)) as client:
        first = await client.get(f"/opds/{protocol}/browse/{category}")
        next_href = _next(first)
        assert next_href is not None
        second = await client.get(next_href)
    first_entries, second_entries = _navigation(first), _navigation(second)
    assert (len(first_entries), len(second_entries)) == (50, 11)
    assert _next(second) is None
    combined = [*first_entries.items(), *second_entries.items()]
    assert [title for title, _ in combined] == [
        f"Tag {index:02}" for index in range(60, -1, -1)
    ]
    assert [_thumbnail_gid(image) for _, (_, image) in combined] == list(
        range(3060, 2999, -1)
    )
    assert [(name, limit) for name, _, limit, _ in catalog.tag_bundle_calls] == [
        (namespace, 50)
    ] * 2
    assert [limit for _, limit, _ in catalog.tag_calls] == [50, 50]
    assert (
        not catalog.list_calls
        and not catalog.publication_revisions
        and not catalog.presentation_revisions
    )


@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
@pytest.mark.parametrize("category", ("artists", "parodies", "characters"))
async def test_navigation_thumbnail_link_serves_selected_thumbnail_bytes(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
) -> None:
    async with app_client(create_app(opds_config, _catalog(catalog_fixture))) as client:
        directory = await client.get(f"/opds/{protocol}/browse/{category}")
        thumbnail_url = _navigation(directory)["Alpha"][1]
        assert thumbnail_url is not None
        thumbnail = await client.get(thumbnail_url)
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] == "image/jpeg"
    assert thumbnail.content == catalog_fixture.thumbnail_payload


@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
@pytest.mark.parametrize("category", ("artists", "parodies", "characters"))
async def test_directory_thumbnail_uses_title_tie_breaker_for_each_tag(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    category: str,
) -> None:
    catalog = _catalog(catalog_fixture)
    async with app_client(create_app(opds_config, catalog)) as client:
        response = await client.get(f"/opds/{protocol}/browse/{category}")
    assert {
        title: _thumbnail_gid(image)
        for title, (_, image) in _navigation(response).items()
    } == {"Alpha": 2103, "Beta": 2104, "old": 2105}


@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
@pytest.mark.parametrize("empty", (False, True))
async def test_navigation_omits_thumbnail_when_exact_first_book_has_no_pages(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig, protocol: str, empty: bool
) -> None:
    subjects = (
        ("artist", "Artist"),
        ("group", "Group"),
        ("parody", "Parody"),
        ("character", "Character"),
        ("other", "soushuuhen"),
        ("other", "multi-work series"),
        ("other", "uncensored"),
        ("other", "goudoushi"),
    )
    first = _publication(
        catalog_fixture.publications[0],
        gid=4001,
        title="First",
        uploaded=2,
        downloaded=2,
        subjects=subjects,
    )
    second = _publication(
        catalog_fixture.publications[0],
        gid=4002,
        title="Second",
        uploaded=1,
        downloaded=1,
        subjects=subjects,
    )
    catalog = (
        FakeCatalog(())
        if empty
        else FakeCatalog(
            (replace(first, page_count=0, cover=None, thumbnail=None), second)
        )
    )
    root = f"/opds/{protocol}" + ("/catalog" if protocol == "v1.2" else "")
    async with app_client(create_app(opds_config, catalog)) as client:
        for path in (
            root,
            *(
                f"/opds/{protocol}/browse/{category}"
                for category, _namespace in _DIRECTORIES
            ),
        ):
            response = await client.get(path)
            assert all(image is None for _, image in _navigation(response).values())
    if empty:
        assert (
            not catalog.list_calls
            and not catalog.tag_calls
            and not catalog.recent_list_calls
        )


@pytest.mark.parametrize("surface", ("root", "directory"))
@pytest.mark.parametrize(
    "corruption",
    ("revision", "namespace", "cardinality", "membership", "uploaded", "artifact"),
)
def test_navigation_rejects_contradictory_tag_preview_bundles(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    corruption: str,
) -> None:
    catalog = _catalog(catalog_fixture)
    original = catalog.list_tag_values_with_publications

    def corrupted(**kwargs: Any) -> CatalogTagBundle:
        bundle = original(**kwargs)
        if corruption == "revision":
            object.__setattr__(
                bundle,
                "page",
                replace(
                    bundle.page,
                    revision=replace(bundle.page.revision, revision=8),
                    next_cursor=None,
                ),
            )
        elif corruption == "namespace":
            object.__setattr__(
                bundle,
                "page",
                replace(bundle.page, namespace="wrong", next_cursor=None),
            )
        elif corruption == "cardinality":
            object.__setattr__(bundle, "publications", ())
        else:
            first = bundle.publications[0]
            if corruption == "membership":
                first = replace(first, subjects=())
            elif corruption == "uploaded":
                first = replace(
                    first, published_at=first.published_at - timedelta(days=1)
                )
            else:
                first = replace(first, artifacts=())
            object.__setattr__(
                bundle, "publications", (first, *bundle.publications[1:])
            )
        return bundle

    monkeypatch.setattr(catalog, "list_tag_values_with_publications", corrupted)
    service = _service(catalog, opds_config)
    with pytest.raises(CatalogIntegrityError):
        if surface == "root":
            service.navigation(None)
        else:
            service.browse_page(
                target=BrowseTarget("artists"), cursor=None, limit=None, revision=None
            )


def test_metadata_only_navigation_skips_all_collection_and_preview_reads(
    catalog_fixture: CatalogFixture, opds_config: OPDSConfig
) -> None:
    catalog = _catalog(catalog_fixture)
    catalog.revision = replace(catalog.revision, artifact_count=0)
    service = _service(catalog, opds_config)
    navigation = service.navigation(None)
    assert navigation.publications == {}
    directory = service.browse_page(
        target=BrowseTarget("artists"), cursor=None, limit=None, revision=None
    )
    assert directory.directory_publications == ()
    assert (
        not catalog.list_calls
        and not catalog.tag_calls
        and not catalog.recent_list_calls
    )


@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
@pytest.mark.parametrize("surface", ("root", "directory"))
async def test_navigation_preview_treats_naive_uploaded_time_as_utc(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    protocol: str,
    surface: str,
) -> None:
    class NaivePreviewCatalog(FakeCatalog):
        def list_tag_values_with_publications(
            self,
            *,
            namespace: str,
            after: CatalogTagCursor | None = None,
            limit: int = 50,
            revision: CatalogRevision | int | None = None,
        ) -> CatalogTagBundle:
            bundle = super().list_tag_values_with_publications(
                namespace=namespace, after=after, limit=limit, revision=revision
            )
            return replace(
                bundle,
                publications=tuple(
                    replace(
                        publication,
                        published_at=publication.published_at.replace(tzinfo=None),
                    )
                    for publication in bundle.publications
                ),
            )

    catalog = NaivePreviewCatalog(_catalog(catalog_fixture).publications)
    path = (
        f"/opds/{protocol}" + ("/catalog" if protocol == "v1.2" else "")
        if surface == "root"
        else f"/opds/{protocol}/browse/artists"
    )
    async with app_client(create_app(opds_config, catalog)) as client:
        response = await client.get(path)
    title = "Artists" if surface == "root" else "Alpha"
    assert _thumbnail_gid(_navigation(response)[title][1]) == 2103


@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
@pytest.mark.parametrize("explicit", (False, True))
@pytest.mark.parametrize(
    "stage",
    (
        "discover_publications",
        "list_recent_publications",
        "list_tag_values_with_publications",
        "list_tag_publications",
        "final-head",
    ),
)
async def test_root_thumbnail_reads_recover_only_explicit_stale_revision(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    explicit: bool,
    stage: str,
) -> None:
    catalog = _catalog(catalog_fixture)
    if stage == "final-head":
        original = catalog.get_catalog_revision
        calls = 0

        def advance_final_head(requested: int | None = None) -> CatalogRevision:
            nonlocal calls
            calls += 1
            if calls == 2:
                catalog.revision = replace(catalog.revision, revision=8)
            return original(requested)

        monkeypatch.setattr(catalog, "get_catalog_revision", advance_final_head)
    else:

        def advance_during_read(**_kwargs: Any) -> Any:
            catalog.revision = replace(catalog.revision, revision=8)
            raise CatalogRevisionNotFoundError(7)

        monkeypatch.setattr(catalog, stage, advance_during_read)
    path = f"/opds/{protocol}" + ("/catalog" if protocol == "v1.2" else "")
    async with app_client(create_app(opds_config, catalog)) as client:
        response = await client.get(
            path,
            params={"revision": 7} if explicit else {},
            headers={"Host": "attacker.invalid"},
            follow_redirects=False,
        )
    assert response.status_code == (303 if explicit else 404)
    assert response.headers["cache-control"] == "no-store"
    if explicit:
        assert response.headers["location"] == f"http://catalog.example{path}"
    else:
        assert "location" not in response.headers
