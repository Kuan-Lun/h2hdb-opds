from dataclasses import replace
from datetime import UTC, datetime

from h2hdb import CatalogContributor, CatalogRevision, CatalogSubject
from pydantic import SecretStr

from h2hdb_opds import BasicAuthConfig, OPDSConfig, ServerConfig, create_app
from h2hdb_opds.auth import (
    AUTHENTICATION_DOCUMENT_REL,
    AUTHENTICATION_MEDIA_TYPE,
)
from h2hdb_opds.serialization import (
    OPDS_FEED_MEDIA_TYPE,
    OPDS_PUBLICATION_MEDIA_TYPE,
)

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client


async def test_authentication_document_is_public_and_basic_auth_is_enforced(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = OPDSConfig(
        artifact_root=opds_config.artifact_root,
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
        document_response = await client.get("/opds/v2/authentication")
        unauthorized = await client.get("/opds/v2")
        wrong_password = await client.get("/opds/v2", auth=("reader", "wrong"))
        authorized = await client.get("/opds/v2", auth=("reader", "secret"))

    assert document_response.status_code == 200
    assert document_response.headers["content-type"] == AUTHENTICATION_MEDIA_TYPE
    assert document_response.json()["authentication"][0]["type"].endswith("/basic")
    for response in (unauthorized, wrong_password):
        assert response.status_code == 401
        assert response.headers["content-type"] == AUTHENTICATION_MEDIA_TYPE
        assert response.headers["www-authenticate"] == (
            'Basic realm="Private", charset="UTF-8"'
        )
        assert f'rel="{AUTHENTICATION_DOCUMENT_REL}"' in response.headers["link"]
        assert response.json()["links"][0]["rel"] == "self"
    assert authorized.status_code == 200


async def test_navigation_has_self_publications_and_search_template(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get("/opds/v2")

    assert response.status_code == 200
    assert response.headers["content-type"] == OPDS_FEED_MEDIA_TYPE
    document = response.json()
    assert document["metadata"]["numberOfItems"] == 3
    assert document["links"][0] == {
        "rel": "self",
        "href": "http://catalog.example/opds/v2?revision=7",
        "type": OPDS_FEED_MEDIA_TYPE,
    }
    search_link = next(link for link in document["links"] if link["rel"] == "search")
    assert search_link["templated"] is True
    assert search_link["href"].endswith(
        "/opds/v2/search?revision=7{&query,offset,limit}"
    )
    assert document["navigation"][0]["href"].endswith(
        "/opds/v2/publications?revision=7"
    )


async def test_publications_search_pagination_and_single_document(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        first_page = await client.get("/opds/v2/publications")
        second_page = await client.get("/opds/v2/publications?offset=2&limit=2")
        search = await client.get("/opds/v2/search?query=cobalt&offset=0&limit=1")
        publication = await client.get("/opds/v2/publications/publication-alpha")
        missing = await client.get("/opds/v2/publications/missing")

    assert first_page.status_code == 200
    assert first_page.headers["content-type"] == OPDS_FEED_MEDIA_TYPE
    first_document = first_page.json()
    assert first_document["metadata"] == {
        "@type": "http://schema.org/DataFeed",
        "title": "All Publications",
        "modified": "2026-08-05T12:00:00Z",
        "numberOfItems": 3,
        "itemsPerPage": 2,
        "currentPage": 1,
    }
    assert len(first_document["publications"]) == 2
    assert "revision=7" in first_document["links"][0]["href"]
    assert any(link["rel"] == "next" for link in first_document["links"])
    acquisition = next(
        link
        for link in first_document["publications"][0]["links"]
        if link["rel"] == "http://opds-spec.org/acquisition"
    )
    assert acquisition["type"] == "application/vnd.comicbook+zip"
    assert acquisition["href"].endswith(
        "/opds/v2/acquisitions/artifact-alpha?revision=7"
    )

    assert second_page.json()["metadata"]["currentPage"] == 2
    assert len(second_page.json()["publications"]) == 1
    search_document = search.json()
    assert search_document["metadata"]["numberOfItems"] == 2
    assert search_document["metadata"]["itemsPerPage"] == 1
    assert search_document["publications"][0]["metadata"]["title"] == "Alpha Gallery"
    assert catalog_fixture.catalog.list_calls[-1] == ("cobalt", 0, 1)

    assert publication.status_code == 200
    assert publication.headers["content-type"] == OPDS_PUBLICATION_MEDIA_TYPE
    publication_metadata = publication.json()["metadata"]
    assert publication_metadata["identifier"] == "publication-alpha"
    assert publication_metadata["title"] == "Alpha Gallery"
    assert publication_metadata["subject"] == [
        {"name": "fantasy", "scheme": "tag", "code": "f"}
    ]
    assert publication.json()["links"][0]["rel"] == "self"
    assert publication.json()["links"][0]["href"].endswith(
        "/opds/v2/publications/publication-alpha?revision=7"
    )
    assert all(
        revision == catalog_fixture.catalog.revision
        for revision in catalog_fixture.catalog.list_revisions
    )
    assert catalog_fixture.catalog.publication_revisions == [
        catalog_fixture.catalog.revision,
        catalog_fixture.catalog.revision,
    ]
    assert missing.status_code == 404


async def test_missing_revision_returns_404_before_catalog_reads(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        navigation = await client.get("/opds/v2?revision=404")
        feed = await client.get("/opds/v2/publications?revision=404")
        search = await client.get("/opds/v2/search?query=alpha&revision=404")
        detail = await client.get(
            "/opds/v2/publications/publication-alpha?revision=404"
        )
        acquisition = await client.get(
            "/opds/v2/acquisitions/artifact-alpha?revision=404"
        )

    for response in (navigation, feed, search, detail, acquisition):
        assert response.status_code == 404
        assert response.json()["detail"] == "Catalog revision 404 not found"
    assert catalog_fixture.catalog.revision_lookups == [404] * 5
    assert catalog_fixture.catalog.list_revisions == []
    assert catalog_fixture.catalog.publication_revisions == []
    assert catalog_fixture.catalog.artifact_revisions == []


async def test_old_revision_next_page_remains_readable_after_current_advances(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(
        opds_config.model_copy(update={"default_page_size": 2, "maximum_page_size": 2}),
        catalog_fixture.catalog,
    )
    historical_revision = catalog_fixture.catalog.revision

    async with app_client(app) as client:
        first_page = await client.get("/opds/v2/publications")
        next_url = next(
            link["href"] for link in first_page.json()["links"] if link["rel"] == "next"
        )
        catalog_fixture.catalog.add_revision(
            CatalogRevision(
                revision=8,
                published_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
                publication_count=1,
            ),
            (catalog_fixture.publications[1],),
        )

        old_next_page = await client.get(next_url)
        old_detail = await client.get(
            "/opds/v2/publications/publication-alpha?revision=7"
        )
        old_acquisition = await client.get(
            "/opds/v2/acquisitions/artifact-alpha?revision=7"
        )
        new_current_page = await client.get("/opds/v2/publications")

    assert first_page.status_code == 200
    assert old_next_page.status_code == 200
    old_document = old_next_page.json()
    assert old_document["metadata"]["numberOfItems"] == 3
    assert old_document["metadata"]["currentPage"] == 2
    assert [
        publication["metadata"]["identifier"]
        for publication in old_document["publications"]
    ] == ["publication-gamma"]
    assert all(
        "revision=7" in link["href"]
        for link in old_document["links"]
        if link["rel"] != AUTHENTICATION_DOCUMENT_REL
    )
    assert old_detail.status_code == 200
    assert old_detail.json()["metadata"]["identifier"] == "publication-alpha"
    assert old_acquisition.status_code == 200
    assert old_acquisition.content == catalog_fixture.payload

    new_document = new_current_page.json()
    assert new_document["metadata"]["numberOfItems"] == 1
    assert "revision=8" in new_document["links"][0]["href"]
    assert catalog_fixture.catalog.revision_lookups[-4:] == [7, 7, 7, None]
    assert catalog_fixture.catalog.list_revisions[-2:] == [
        historical_revision,
        catalog_fixture.catalog.revision,
    ]
    assert catalog_fixture.catalog.publication_revisions[-1] == historical_revision
    assert catalog_fixture.catalog.artifact_revisions[-1] == historical_revision


async def test_artifactless_publications_are_filtered_with_accurate_counts(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    artifactless = replace(catalog_fixture.publications[0], artifacts=())
    catalog = FakeCatalog((artifactless, catalog_fixture.publications[1]))
    app = create_app(opds_config, catalog)

    async with app_client(app) as client:
        navigation = await client.get("/opds/v2")
        feed = await client.get("/opds/v2/publications")
        detail = await client.get("/opds/v2/publications/publication-alpha")

    assert navigation.json()["metadata"]["numberOfItems"] == 1
    assert feed.json()["metadata"]["numberOfItems"] == 1
    assert [item["metadata"]["identifier"] for item in feed.json()["publications"]] == [
        "publication-beta"
    ]
    assert detail.status_code == 404
    assert catalog.require_artifact_calls == [True, True]


async def test_search_is_normalized_and_pagination_requires_page_alignment(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        normalized = await client.get(
            "/opds/v2/search",
            params={"query": "  cobalt\t adventure  ", "limit": 2},
        )
        blank = await client.get("/opds/v2/search", params={"query": " \t "})
        misaligned = await client.get(
            "/opds/v2/publications",
            params={"offset": 1, "limit": 2},
        )

    assert normalized.status_code == 200
    assert catalog_fixture.catalog.list_calls[-1] == (
        "cobalt adventure",
        0,
        2,
    )
    assert "query=cobalt+adventure" in normalized.json()["links"][0]["href"]
    assert blank.status_code == 422
    assert misaligned.status_code == 422


async def test_publication_omits_blank_metadata_and_uses_standard_link_size(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        title=" ",
        sort_title=" ",
        summary=" ",
        language="ZH_hant_tw",
        contributors=(
            CatalogContributor(name=" ", role="artist"),
            CatalogContributor(name=" Artist ", role="ARTIST", sort_as=" "),
        ),
        subjects=(
            CatalogSubject(name=" ", scheme="", code=""),
            CatalogSubject(name=" Theme ", scheme=" \t ", code="\n"),
        ),
    )
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        response = await client.get("/opds/v2/publications/publication-alpha")

    document = response.json()
    metadata = document["metadata"]
    assert metadata["title"] == "publication-alpha"
    assert metadata["language"] == "zh-Hant-TW"
    assert "sortAs" not in metadata
    assert "description" not in metadata
    assert metadata["subject"] == [{"name": "Theme"}]
    assert metadata["artist"] == [{"name": "Artist", "role": "artist"}]
    acquisition = next(
        link for link in document["links"] if link["rel"].startswith("http://opds")
    )
    assert acquisition["size"] == len(catalog_fixture.payload)
    assert "properties" not in acquisition


async def test_invalid_language_is_omitted_and_host_header_is_not_trusted(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        language="not a language tag",
    )
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        feed = await client.get(
            "/opds/v2/publications",
            headers={"Host": "attacker.invalid"},
        )

    document = feed.json()
    assert "language" not in document["publications"][0]["metadata"]
    assert all(
        link["href"].startswith("http://catalog.example/") for link in document["links"]
    )
    assert "attacker.invalid" not in str(document)


async def test_dynamic_link_identifiers_are_percent_encoded(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        publication_id="publication alpha?#",
    )
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        response = await client.get("/opds/v2/publications")

    self_link = response.json()["publications"][0]["links"][0]["href"]
    assert "/publication%20alpha%3F%23?revision=7" in self_link
