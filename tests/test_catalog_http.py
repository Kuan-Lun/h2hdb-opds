from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from h2hdb import (
    CatalogDiscoveryBundle,
    CatalogDiscoveryQuery,
    CatalogFacetKind,
    CatalogRevision,
    CatalogSubject,
    CatalogSubjectFilter,
)
from pydantic import SecretStr

from h2hdb_opds import BasicAuthConfig, OPDSConfig, ServerConfig, create_app
from h2hdb_opds.auth import AUTHENTICATION_MEDIA_TYPE
from h2hdb_opds.catalog_service import (
    CatalogService,
    RevisionUnavailable,
)
from h2hdb_opds.library import LibraryReadCoordinator
from h2hdb_opds.publication import (
    OPDS_ACQUISITION_REL,
    OPDS_OPEN_ACCESS_REL,
    publication_identifier,
)
from h2hdb_opds.search import parse_search_query
from h2hdb_opds.serialization import (
    OPDS_FEED_MEDIA_TYPE,
    OPDS_PUBLICATION_MEDIA_TYPE,
)

from .fakes import ALPHA_ARTIFACT_ID, CatalogFixture, FakeCatalog
from .http_client import app_client

_ALPHA_ACQUISITION_V1_PATH = f"/opds/v1.2/acquisitions/{ALPHA_ARTIFACT_ID}"
_ALPHA_ACQUISITION_V2_PATH = f"/opds/v2/acquisitions/{ALPHA_ARTIFACT_ID}"


async def test_authentication_document_is_public_and_catalog_is_protected(
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
            realm="Private",
        ),
        server=ServerConfig(trusted_proxy_ips=("127.0.0.1",)),
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app, base_url="https://testserver") as client:
        authentication = await client.get("/opds/v2/authentication")
        unauthorized = await client.get("/opds/v2")
        authorized = await client.get("/opds/v2", auth=("reader", "secret"))
        publication = await client.get(
            "/opds/v2/publications/urn:h2h:gallery:1001",
            auth=("reader", "secret"),
        )

    assert authentication.status_code == 200
    assert authentication.headers["content-type"] == AUTHENTICATION_MEDIA_TYPE
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    acquisition = next(
        link
        for link in publication.json()["links"]
        if link["rel"] == OPDS_ACQUISITION_REL
    )
    assert acquisition["rel"] == OPDS_ACQUISITION_REL


async def test_root_groups_the_same_three_catalogs_and_advertises_search(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get("/opds/v2")

    assert response.status_code == 200
    assert response.headers["content-type"] == OPDS_FEED_MEDIA_TYPE
    document = response.json()
    assert "numberOfItems" not in document["metadata"]
    assert [group["metadata"]["title"] for group in document["groups"]] == [
        "Browse",
        "Recent Activity",
    ]
    assert "navigation" not in document
    assert [entry["title"] for entry in document["groups"][0]["navigation"]] == [
        "All Publications"
    ]
    assert [entry["title"] for entry in document["groups"][1]["navigation"]] == [
        "Recently Uploaded",
        "Recently Downloaded",
    ]
    assert all("numberOfItems" not in group["metadata"] for group in document["groups"])
    navigation = [
        entry for group in document["groups"] for entry in group["navigation"]
    ]
    assert [entry["properties"]["numberOfItems"] for entry in navigation] == [
        3,
        3,
        3,
    ]
    assert [entry["rel"] for entry in navigation] == [
        "subsection",
        "http://opds-spec.org/sort/new",
        "subsection",
    ]
    assert all("revision=7" in entry["href"] for entry in navigation)
    search = next(link for link in document["links"] if link["rel"] == "search")
    assert search["templated"] is True
    assert search["href"].endswith(
        "?revision=7{&query,language,tag,tag_namespace,contributor,role,limit}"
    )


async def test_all_publications_uses_seek_pagination_and_stable_uri_identifiers(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        first = await client.get("/opds/v2/publications")
        next_url = next(
            link["href"] for link in first.json()["links"] if link["rel"] == "next"
        )
        second = await client.get(next_url)
        detail = await client.get("/opds/v2/publications/urn:h2h:gallery:1001")

    assert first.status_code == 200
    assert len(first.json()["publications"]) == 2
    assert len(second.json()["publications"]) == 1
    assert "offset=" not in next_url
    metadata = detail.json()["metadata"]
    assert detail.headers["content-type"] == OPDS_PUBLICATION_MEDIA_TYPE
    assert metadata["identifier"] == "urn:h2h:gallery:1001"
    assert metadata["numberOfPages"] == 1
    images = detail.json()["images"]
    assert [image["rel"] for image in images] == [
        "http://opds-spec.org/image",
        "http://opds-spec.org/image/thumbnail",
    ]
    assert images[0]["href"].endswith(
        "/media/publications/urn%3Ah2h%3Agallery%3A1001/pages/0?revision=7"
    )
    acquisition = next(
        link for link in detail.json()["links"] if link["rel"] == OPDS_OPEN_ACCESS_REL
    )
    assert acquisition["size"] == len(catalog_fixture.payload)


@pytest.mark.parametrize("path", ["/opds/v1.2/publications", "/opds/v2/publications"])
async def test_discovery_feed_uses_one_revision_pinned_bundle_read(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert catalog_fixture.catalog.revision_lookups == []
    assert catalog_fixture.catalog.bundle_calls == [
        (CatalogDiscoveryQuery(), None, 50, 128, None)
    ]
    assert len(catalog_fixture.catalog.list_calls) == 1
    assert len(catalog_fixture.catalog.facet_calls) == len(CatalogFacetKind)


async def test_discovery_feed_preserves_revision_first_error_precedence(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get(
            "/opds/v2/publications",
            params={"revision": 8, "cursor": "bad"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Catalog revision 8 not found"}
    assert catalog_fixture.catalog.revision_lookups == [None]
    assert catalog_fixture.catalog.bundle_calls == []


def test_discovery_service_preserves_revision_before_limit_error_precedence(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    service = CatalogService(
        reader=lambda: catalog_fixture.catalog,
        library_reads=LibraryReadCoordinator(
            library_root=opds_config.library_root,
            coordination_root=opds_config.coordination_root,
        ),
        default_page_size=opds_config.default_page_size,
        maximum_page_size=opds_config.maximum_page_size,
    )

    with pytest.raises(RevisionUnavailable) as raised:
        service.discovery_feed(
            query=CatalogDiscoveryQuery(),
            cursor=None,
            limit=129,
            revision=8,
        )

    assert raised.value.revision == 8
    assert catalog_fixture.catalog.revision_lookups == [None]
    assert catalog_fixture.catalog.bundle_calls == []


async def test_discovery_feed_rejects_noncanonical_facet_bundle(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = catalog_fixture.catalog.discover_publications_with_facets

    def corrupted_bundle(**kwargs: Any) -> CatalogDiscoveryBundle:
        bundle = original(**kwargs)
        object.__setattr__(bundle, "facets", tuple(reversed(bundle.facets)))
        return bundle

    monkeypatch.setattr(
        catalog_fixture.catalog,
        "discover_publications_with_facets",
        corrupted_bundle,
    )
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get("/opds/v2/publications")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "catalog discovery facet families are incomplete or unordered",
        "code": "catalog_integrity_error",
    }
    assert "retry-after" not in response.headers


async def test_search_and_facets_are_bounded_and_revision_pinned(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        search = await client.get("/opds/v2/search", params={"query": "cobalt"})
        filtered = await client.get(
            "/opds/v2/search",
            params={"query": "cobalt", "language": "en"},
        )
        invalid = await client.get(
            "/opds/v2/search",
            params={"query": "cobalt", "contributor": "Alice"},
        )
        removed_q = await client.get(
            "/opds/v2/search",
            params={"q": "cobalt"},
        )

    assert search.status_code == 200
    assert [item["metadata"]["title"] for item in search.json()["publications"]] == [
        "Alpha Gallery",
        "Gamma Gallery",
    ]
    assert [item["metadata"]["title"] for item in filtered.json()["publications"]] == [
        "Alpha Gallery"
    ]
    facets = search.json()["facets"]
    assert {facet["metadata"]["title"] for facet in facets} >= {"Language", "Tag"}
    language = next(
        facet for facet in facets if facet["metadata"]["title"] == "Language"
    )
    assert language["links"][0]["title"] == "All"
    assert all("revision=7" in link["href"] for link in language["links"])
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "contributor and role must be provided together"
    assert removed_q.status_code == 422
    assert removed_q.json()["detail"] == "q was removed from OPDS 2 search; use query"


async def test_search_uses_core_fields_and_all_lexemes(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        title_and_source = await client.get(
            "/opds/v2/search",
            params={"query": "alpha cobalt"},
        )
        contributor = await client.get(
            "/opds/v2/search",
            params={"query": "alice"},
        )
        subject = await client.get(
            "/opds/v2/search",
            params={"query": "fantasy"},
        )
        summary_only = await client.get(
            "/opds/v2/search",
            params={"query": "adventure"},
        )
        identifier_only = await client.get(
            "/opds/v2/search",
            params={"query": "1001"},
        )

    for response in (title_and_source, contributor, subject):
        assert [
            publication["metadata"]["title"]
            for publication in response.json()["publications"]
        ] == ["Alpha Gallery"]
    for response in (summary_only, identifier_only):
        assert "publications" not in response.json()


async def test_subject_facets_round_trip_exact_namespace_and_value(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    subject = CatalogSubject(
        name="少女  漫畫",
        scheme="source-tag",
        code="女性  類別",
    )
    publication = replace(
        catalog_fixture.publications[0],
        subjects=(subject,),
    )
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        search = await client.get("/opds/v2/publications")
        tag_facet = next(
            facet
            for facet in search.json()["facets"]
            if facet["metadata"]["title"] == "Tag"
        )
        selected = next(
            link for link in tag_facet["links"] if link.get("title") == subject.name
        )
        filtered = await client.get(selected["href"])
        missing_namespace = await client.get(
            "/opds/v2/search",
            params={"query": "少女", "tag": subject.name},
        )

    parameters = parse_qs(urlsplit(selected["href"]).query)
    assert parse_search_query(parameters["query"][0]).subjects == (
        CatalogSubjectFilter(namespace=subject.code or "", value=subject.name),
    )
    assert [
        item["metadata"]["identifier"] for item in filtered.json()["publications"]
    ] == [publication.publication_id]
    assert missing_namespace.status_code == 422
    assert (
        missing_namespace.json()["detail"]
        == "tag and tag_namespace must be provided together"
    )


@pytest.mark.parametrize(
    "query", (None, "gid:1001 title:Alpha pages:1 uploaded:2026-08-05")
)
async def test_large_facet_sets_have_a_bounded_followable_next_page(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    query: str | None,
) -> None:
    subjects = tuple(
        CatalogSubject(name=f"tag-{index:03d}", scheme="tag", code=f"t{index:03d}")
        for index in range(130)
    )
    publication = replace(catalog_fixture.publications[0], subjects=subjects)
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        search = await client.get(
            "/opds/v2/publications" if query is None else "/opds/v2/search",
            params={} if query is None else {"query": query},
        )
        tag_facet = next(
            facet
            for facet in search.json()["facets"]
            if facet["metadata"]["title"] == "Tag"
        )
        more = next(link for link in tag_facet["links"] if link.get("rel") == "next")
        next_page = await client.get(more["href"])

    assert more["title"] == "More Tag values"
    assert next_page.status_code == 200
    assert [entry["title"] for entry in next_page.json()["navigation"]] == [
        "All Tag values",
        "tag-128",
        "tag-129",
    ]


@pytest.mark.parametrize(
    "value",
    [None, "", "   \t", "!!!", " ".join(f"term{index}" for index in range(17))],
)
@pytest.mark.parametrize(
    ("path", "parameter"),
    [
        ("/opds/v1.2/search", "q"),
        ("/opds/v2/search", "query"),
    ],
)
async def test_search_rejects_absent_or_unsearchable_queries(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
    parameter: str,
    value: str | None,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    parameters = {} if value is None else {parameter: value}

    async with app_client(app) as client:
        response = await client.get(path, params=parameters)

    assert response.status_code == 422


async def test_recent_feeds_use_authoritative_orders_and_reject_pagination(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        uploaded = await client.get("/opds/v2/recent/uploaded")
        downloaded = await client.get("/opds/v2/recent/downloaded")
        rejected = await client.get("/opds/v2/recent/uploaded", params={"limit": 1})

    assert [item["metadata"]["title"] for item in uploaded.json()["publications"]] == [
        "Beta Gallery",
        "Alpha Gallery",
        "Gamma Gallery",
    ]
    assert [
        item["metadata"]["title"] for item in downloaded.json()["publications"]
    ] == [
        "Gamma Gallery",
        "Alpha Gallery",
        "Beta Gallery",
    ]
    assert rejected.status_code == 422


async def test_empty_discovery_feed_omits_invalid_empty_publications_array(
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
        root = await client.get("/opds/v2")
        all_publications = await client.get("/opds/v2/publications")
        search = await client.get(
            "/opds/v2/search",
            params={"query": "missing"},
        )

    assert root.status_code == 200
    for response in (all_publications, search):
        document = response.json()
        assert "publications" not in document
        assert document["navigation"][0]["title"] == "All Publications"
        assert document["metadata"].get("numberOfItems") in {None, 0}


@pytest.mark.parametrize(
    "path",
    [
        "/opds/v1.2/publications/urn:h2h:gallery:1001",
        "/opds/v2/publications/urn:h2h:gallery:1001",
        "/media/publications/urn:h2h:gallery:1001/pages/0",
        "/media/publications/urn:h2h:gallery:1001/thumbnail",
        _ALPHA_ACQUISITION_V1_PATH,
        _ALPHA_ACQUISITION_V2_PATH,
    ],
)
async def test_metadata_only_revision_hides_stale_reader_resources(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
) -> None:
    catalog = catalog_fixture.catalog
    catalog.revision = replace(catalog.revision, artifact_count=0)
    app = create_app(opds_config, catalog)

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert catalog.publication_revisions == []
    assert catalog.presentation_revisions == []
    assert catalog.page_revisions == []
    assert catalog.artifact_revisions == []


async def test_stale_pagination_restarts_while_invalid_cursor_fails_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(update={"default_page_size": 1})
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        first = await client.get("/opds/v2/publications")
        next_url = next(
            link["href"] for link in first.json()["links"] if link["rel"] == "next"
        )
        invalid = await client.get("/opds/v2/publications", params={"cursor": "bad"})
        catalog_fixture.catalog.revision = CatalogRevision(
            revision=8,
            published_at=catalog_fixture.catalog.revision.published_at,
            publication_count=3,
            artifact_count=3,
        )
        stale = await client.get(next_url)
        assert stale.status_code == 303
        assert stale.headers["Cache-Control"] == "no-store"
        target = urlsplit(stale.headers["Location"])
        assert target.netloc == "catalog.example"
        assert target.path == "/opds/v2/publications"
        assert parse_qs(target.query) == {"limit": ["1"]}
        restarted = await client.get(stale.headers["Location"])

    assert invalid.status_code == 422
    assert restarted.status_code == 200
    assert restarted.json()["publications"][0]["metadata"]["title"] == "Alpha Gallery"
    current_self = next(
        link["href"] for link in restarted.json()["links"] if link["rel"] == "self"
    )
    assert parse_qs(urlsplit(current_self).query)["revision"] == ["8"]


@pytest.mark.parametrize(
    "publication_id",
    [
        "not a URI",
        "https://books.example/publication/1",
        "urn:h2h:gallery:0",
        "urn:h2h:gallery:01",
        "urn:h2h:gallery:\uff11",
        f"urn:h2h:gallery:{1 << 63}",
        "urn:h2h:gallery:" + "9" * 10_000,
    ],
    ids=[
        "not-uri",
        "generic-uri",
        "zero",
        "leading-zero",
        "unicode-digit",
        "int63-overflow",
        "huge-gid",
    ],
)
@pytest.mark.parametrize(
    "path",
    ["/opds/v1.2/publications", "/opds/v2/publications"],
)
async def test_noncanonical_publication_identifier_fails_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    publication_id: str,
    path: str,
) -> None:
    invalid = replace(catalog_fixture.publications[0])
    object.__setattr__(invalid, "publication_id", publication_id)
    catalog = FakeCatalog((invalid,))
    app = create_app(opds_config, catalog)

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == (
        "catalog publication identity violates the canonical GID contract"
    )


def test_maximum_int63_publication_identifier_is_canonical() -> None:
    gid = (1 << 63) - 1
    identifier = f"urn:h2h:gallery:{gid}"

    assert publication_identifier(identifier, expected_gid=gid) == identifier


@pytest.mark.parametrize(
    "path",
    [
        "/opds/v1.2/publications",
        "/opds/v2/publications",
        "/opds/v1.2/publications/urn:h2h:gallery:9999",
        "/opds/v2/publications/urn:h2h:gallery:9999",
        "/media/publications/urn:h2h:gallery:9999/pages/0",
        "/media/publications/urn:h2h:gallery:9999/thumbnail",
    ],
)
async def test_publication_identifier_gid_mismatch_fails_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
) -> None:
    mismatched = replace(catalog_fixture.publications[0])
    object.__setattr__(mismatched, "publication_id", "urn:h2h:gallery:9999")
    app = create_app(opds_config, FakeCatalog((mismatched,)))

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == (
        "catalog publication identity violates the canonical GID contract"
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "overflow",
        "missing-cover",
        "missing-thumbnail",
        "zero-with-images",
        "png-cover",
        "png-thumbnail",
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        "/opds/v1.2/publications",
        "/opds/v2/publications",
        "/opds/v1.2/publications/urn:h2h:gallery:1001",
        "/opds/v2/publications/urn:h2h:gallery:1001",
        "/media/publications/urn:h2h:gallery:1001/pages/0",
        "/media/publications/urn:h2h:gallery:1001/thumbnail",
    ],
)
async def test_invalid_opds_presentation_shape_fails_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    corruption: str,
    path: str,
) -> None:
    publication = replace(catalog_fixture.publications[0])
    if corruption == "overflow":
        object.__setattr__(publication, "page_count", 4097)
    elif corruption == "missing-cover":
        object.__setattr__(publication, "cover", None)
    elif corruption == "missing-thumbnail":
        object.__setattr__(publication, "thumbnail", None)
    elif corruption == "zero-with-images":
        object.__setattr__(publication, "page_count", 0)
    elif corruption == "png-cover":
        assert publication.cover is not None
        object.__setattr__(
            publication,
            "cover",
            replace(publication.cover, media_type="image/png"),
        )
    else:
        assert publication.thumbnail is not None
        object.__setattr__(
            publication,
            "thumbnail",
            replace(publication.thumbnail, media_type="image/png"),
        )
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == (
        "catalog publication presentation violates the OPDS-PSE contract"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/opds/v1.2/publications",
        "/opds/v2/publications",
        "/opds/v1.2/publications/urn:h2h:gallery:1001",
        "/opds/v2/publications/urn:h2h:gallery:1001",
        _ALPHA_ACQUISITION_V1_PATH,
        _ALPHA_ACQUISITION_V2_PATH,
    ],
)
async def test_unsupported_artifact_media_type_fails_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
) -> None:
    artifact = replace(
        catalog_fixture.artifact,
        media_type="application/epub+zip",
    )
    publication = replace(
        catalog_fixture.publications[0],
        artifacts=(artifact,),
    )
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == (
        "catalog artifact is not a supported direct CBZ acquisition"
    )


@pytest.mark.parametrize("corruption", ["missing", "duplicate"])
@pytest.mark.parametrize(
    "path",
    [
        "/opds/v1.2/publications",
        "/opds/v2/publications",
        "/opds/v1.2/publications/urn:h2h:gallery:1001",
        "/opds/v2/publications/urn:h2h:gallery:1001",
    ],
)
async def test_publication_requires_exactly_one_artifact(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    corruption: str,
    path: str,
) -> None:
    artifacts = (
        ()
        if corruption == "missing"
        else (catalog_fixture.artifact, catalog_fixture.artifact)
    )
    publication = replace(catalog_fixture.publications[0])
    object.__setattr__(publication, "artifacts", artifacts)
    catalog = FakeCatalog((publication,))
    if corruption == "missing":
        catalog.discovery_corruption = "include-artifactless"
    catalog.revision = replace(catalog.revision, artifact_count=1)
    app = create_app(opds_config, catalog)

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == (
        "catalog publication must have exactly one direct CBZ acquisition"
    )


@pytest.mark.parametrize(
    ("corruption", "detail"),
    [
        ("order", "recent artifact window order differs from the request"),
        ("oversized", "recent window is not an acquisition-only top-128 set"),
        ("artifactless", "recent window is not an acquisition-only top-128 set"),
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        "/opds/v1.2/recent/uploaded",
        "/opds/v2/recent/uploaded",
    ],
)
async def test_corrupt_recent_window_fails_closed(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    corruption: str,
    detail: str,
    path: str,
) -> None:
    catalog_fixture.catalog.recent_corruption = corruption
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == detail


@pytest.mark.parametrize(
    "path",
    [
        "/opds/v1.2/publications/not-a-uri",
        "/opds/v2/publications/not-a-uri",
        "/media/publications/not-a-uri/pages/0",
        "/media/publications/not-a-uri/thumbnail",
    ],
)
async def test_noncanonical_requested_publication_identifier_is_not_queried(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert catalog_fixture.catalog.publication_revisions == []
    assert catalog_fixture.catalog.page_revisions == []
    assert catalog_fixture.catalog.presentation_revisions == []


@pytest.mark.parametrize(
    ("path", "parameters"),
    [
        ("/opds/v1.2/catalog", {}),
        ("/opds/v1.2/publications", {}),
        ("/opds/v1.2/search", {"q": "cobalt"}),
        ("/opds/v1.2/facets/language", {}),
        ("/opds/v1.2/recent/uploaded", {}),
        ("/opds/v1.2/opensearch.xml", {}),
        ("/opds/v1.2/publications/urn:h2h:gallery:1001", {}),
        (_ALPHA_ACQUISITION_V1_PATH, {}),
        ("/opds/v2", {}),
        ("/opds/v2/publications", {}),
        ("/opds/v2/search", {"query": "cobalt"}),
        ("/opds/v2/facets/language", {}),
        ("/opds/v2/recent/uploaded", {}),
        ("/opds/v2/publications/urn:h2h:gallery:1001", {}),
        (_ALPHA_ACQUISITION_V2_PATH, {}),
        ("/media/publications/urn:h2h:gallery:1001/pages/0", {}),
        ("/media/publications/urn:h2h:gallery:1001/thumbnail", {}),
    ],
)
async def test_partial_artifact_revision_fails_closed_on_every_catalog_surface(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    path: str,
    parameters: dict[str, str],
) -> None:
    revision = replace(catalog_fixture.catalog.revision)
    object.__setattr__(revision, "artifact_count", 1)
    catalog_fixture.catalog.revision = revision
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get(path, params=parameters)

    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == (
        "catalog revision violates the all-or-none artifact contract"
    )
