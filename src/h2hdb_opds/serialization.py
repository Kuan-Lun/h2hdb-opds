__all__ = [
    "OPDS_FEED_MEDIA_TYPE",
    "OPDS_PUBLICATION_MEDIA_TYPE",
    "discovery_document",
    "facet_navigation_document",
    "navigation_document",
    "publication_document",
    "recent_document",
]

from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from h2hdb import (
    CatalogDiscoveryCursor,
    CatalogDiscoveryPage,
    CatalogDiscoveryQuery,
    CatalogFacetCursor,
    CatalogFacetKind,
    CatalogFacetPage,
    CatalogImageResource,
    CatalogPublication,
    CatalogRecentWindow,
    CatalogRevision,
)

from .auth import AUTHENTICATION_DOCUMENT_REL, AUTHENTICATION_MEDIA_TYPE
from .config import OPDSConfig
from .cursor import encode_discovery_cursor, encode_facet_cursor
from .discovery import discovery_query_parameters, query_with_facet
from .language import normalize_bcp47
from .publication import acquisition_relation, publication_identifier
from .urls import external_url

OPDS_FEED_MEDIA_TYPE = "application/opds+json"
OPDS_PUBLICATION_MEDIA_TYPE = "application/opds-publication+json"
OPDS_SORT_NEW_REL = "http://opds-spec.org/sort/new"
OPDS_IMAGE_REL = "http://opds-spec.org/image"
OPDS_THUMBNAIL_REL = "http://opds-spec.org/image/thumbnail"

_FACET_TITLES = {
    CatalogFacetKind.LANGUAGE: "Language",
    CatalogFacetKind.SUBJECT: "Tag",
    CatalogFacetKind.CONTRIBUTOR: "Contributor",
}


def _format_datetime(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_trimmed(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _optional_uri(value: str | None) -> str | None:
    selected = _optional_trimmed(value)
    if selected is None:
        return None
    try:
        selected.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        return None
    return selected if urlsplit(selected).scheme else None


def _url_with_query(url: str, parameters: dict[str, str | int]) -> str:
    return url if not parameters else f"{url}?{urlencode(parameters)}"


def _discovery_query_parameters(query: CatalogDiscoveryQuery) -> dict[str, str]:
    return discovery_query_parameters(query, search_parameter="query")


def _common_links(
    request: Request,
    config: OPDSConfig,
    revision: int,
) -> list[dict[str, object]]:
    links: list[dict[str, object]] = [
        {
            "rel": "start",
            "href": _url_with_query(
                external_url(request, config, "navigation"),
                {"revision": revision},
            ),
            "type": OPDS_FEED_MEDIA_TYPE,
        },
        {
            "rel": AUTHENTICATION_DOCUMENT_REL,
            "href": external_url(request, config, "authentication_document"),
            "type": AUTHENTICATION_MEDIA_TYPE,
        },
    ]
    if not config.auth.enabled:
        links[-1]["properties"] = {"optional": True}
    return links


def _search_link(
    request: Request, config: OPDSConfig, revision: int
) -> dict[str, object]:
    base = _url_with_query(
        external_url(request, config, "search_publications"),
        {"revision": revision},
    )
    return {
        "rel": "search",
        "href": (f"{base}{{&query,language,tag,tag_namespace,contributor,role,limit}}"),
        "type": OPDS_FEED_MEDIA_TYPE,
        "templated": True,
        "title": "Search",
    }


def navigation_document(
    request: Request,
    config: OPDSConfig,
    revision: CatalogRevision,
) -> dict[str, object]:
    selected_revision = revision.revision
    self_url = _url_with_query(
        external_url(request, config, "navigation"),
        {"revision": selected_revision},
    )
    links = [
        {"rel": "self", "href": self_url, "type": OPDS_FEED_MEDIA_TYPE},
        _search_link(request, config, selected_revision),
        *_common_links(request, config, selected_revision),
    ]
    count = revision.artifact_count
    return {
        "metadata": {
            "@type": "http://schema.org/DataFeed",
            "title": config.title,
            "modified": _format_datetime(revision.published_at),
        },
        "links": links,
        "navigation": [
            {
                "title": "All Publications",
                "href": _url_with_query(
                    external_url(request, config, "list_publications"),
                    {"revision": selected_revision},
                ),
                "type": OPDS_FEED_MEDIA_TYPE,
                "rel": "subsection",
                "properties": {"numberOfItems": count},
            },
            {
                "title": "Recently Uploaded",
                "href": _url_with_query(
                    external_url(request, config, "recently_uploaded"),
                    {"revision": selected_revision},
                ),
                "type": OPDS_FEED_MEDIA_TYPE,
                "rel": OPDS_SORT_NEW_REL,
                "properties": {"numberOfItems": min(128, count)},
            },
            {
                "title": "Recently Downloaded",
                "href": _url_with_query(
                    external_url(request, config, "recently_downloaded"),
                    {"revision": selected_revision},
                ),
                "type": OPDS_FEED_MEDIA_TYPE,
                "rel": "subsection",
                "properties": {"numberOfItems": min(128, count)},
            },
        ],
    }


def _contributor_metadata(publication: CatalogPublication) -> dict[str, object]:
    contributors: dict[str, list[dict[str, str]]] = {}
    supported_roles = {
        "artist",
        "author",
        "colorist",
        "contributor",
        "editor",
        "illustrator",
        "letterer",
        "narrator",
        "translator",
    }
    for contributor in publication.contributors:
        name = contributor.name.strip()
        if not name:
            continue
        role = contributor.role.strip().casefold()
        key = role if role in supported_roles else "contributor"
        contributors.setdefault(key, []).append(
            {"name": name, "role": role or "contributor"}
        )
    return dict(contributors)


def _image_document(
    request: Request,
    config: OPDSConfig,
    publication_id: str,
    revision: int,
    resource: CatalogImageResource,
    *,
    endpoint: str,
    relation: str,
    page_number: int | None = None,
) -> dict[str, object]:
    parameters: dict[str, object] = {"publication_id": publication_id}
    if page_number is not None:
        parameters["page_number"] = page_number
    return {
        "rel": relation,
        "href": _url_with_query(
            external_url(request, config, endpoint, **parameters),
            {"revision": revision},
        ),
        "type": resource.media_type,
        "width": resource.width,
        "height": resource.height,
        "size": resource.extent.length,
    }


def publication_document(
    request: Request,
    config: OPDSConfig,
    publication: CatalogPublication,
    revision: int,
) -> dict[str, object]:
    identifier = publication_identifier(
        publication.publication_id,
        expected_gid=publication.gid,
    )
    title = publication.title.strip() or identifier
    metadata: dict[str, object] = {
        "@type": "http://schema.org/Book",
        "identifier": identifier,
        "title": title,
        "published": _format_datetime(publication.published_at),
        "modified": _format_datetime(publication.modified_at),
        "subject": [
            {
                key: value
                for key, value in {
                    "name": subject.name.strip(),
                    "scheme": _optional_uri(subject.scheme),
                    "code": _optional_trimmed(subject.code),
                }.items()
                if value is not None
            }
            for subject in publication.subjects
            if subject.name.strip()
        ],
        **_contributor_metadata(publication),
    }
    if publication.sort_title.strip():
        metadata["sortAs"] = publication.sort_title.strip()
    language = normalize_bcp47(publication.language)
    if language is not None:
        metadata["language"] = language
    if publication.summary.strip():
        metadata["description"] = publication.summary.strip()
    if publication.page_count > 0:
        metadata["numberOfPages"] = publication.page_count
    if not metadata["subject"]:
        del metadata["subject"]

    links: list[dict[str, object]] = [
        {
            "rel": "self",
            "href": _url_with_query(
                external_url(
                    request,
                    config,
                    "get_publication",
                    publication_id=publication.publication_id,
                ),
                {"revision": revision},
            ),
            "type": OPDS_PUBLICATION_MEDIA_TYPE,
        }
    ]
    links.extend(
        {
            "rel": acquisition_relation(config),
            "href": _url_with_query(
                external_url(
                    request,
                    config,
                    "acquire_artifact",
                    artifact_id=artifact.artifact_id,
                ),
                {"revision": revision},
            ),
            "type": artifact.media_type,
            **({"title": artifact.name.strip()} if artifact.name.strip() else {}),
            "size": artifact.storage_object.size_bytes,
        }
        for artifact in publication.artifacts
    )
    document: dict[str, object] = {"metadata": metadata, "links": links}
    images: list[dict[str, object]] = []
    if publication.cover is not None:
        images.append(
            _image_document(
                request,
                config,
                publication.publication_id,
                revision,
                publication.cover,
                endpoint="publication_page",
                relation=OPDS_IMAGE_REL,
                page_number=0,
            )
        )
    if publication.thumbnail is not None:
        images.append(
            _image_document(
                request,
                config,
                publication.publication_id,
                revision,
                publication.thumbnail,
                endpoint="publication_thumbnail",
                relation=OPDS_THUMBNAIL_REL,
            )
        )
    if images:
        document["images"] = images
    return document


def _page_url(
    request: Request,
    config: OPDSConfig,
    page: CatalogDiscoveryPage,
    *,
    cursor: CatalogDiscoveryCursor | None,
    endpoint: str,
    query: CatalogDiscoveryQuery,
) -> str:
    parameters: dict[str, str | int] = {
        **_discovery_query_parameters(query),
        "limit": page.limit,
        "revision": page.revision.revision,
    }
    if cursor is not None:
        parameters["cursor"] = encode_discovery_cursor(cursor)
    return _url_with_query(external_url(request, config, endpoint), parameters)


def _pagination_links(
    request: Request,
    config: OPDSConfig,
    page: CatalogDiscoveryPage,
    *,
    cursor: CatalogDiscoveryCursor | None,
    endpoint: str,
    query: CatalogDiscoveryQuery,
) -> list[dict[str, object]]:
    links: list[dict[str, object]] = [
        {
            "rel": "self",
            "href": _page_url(
                request,
                config,
                page,
                cursor=cursor,
                endpoint=endpoint,
                query=query,
            ),
            "type": OPDS_FEED_MEDIA_TYPE,
        },
        {
            "rel": "first",
            "href": _page_url(
                request,
                config,
                page,
                cursor=None,
                endpoint=endpoint,
                query=query,
            ),
            "type": OPDS_FEED_MEDIA_TYPE,
        },
    ]
    if page.next_cursor is not None:
        links.append(
            {
                "rel": "next",
                "href": _page_url(
                    request,
                    config,
                    page,
                    cursor=page.next_cursor,
                    endpoint=endpoint,
                    query=query,
                ),
                "type": OPDS_FEED_MEDIA_TYPE,
            }
        )
    return links


def _facet_link(
    request: Request,
    config: OPDSConfig,
    query: CatalogDiscoveryQuery,
    revision: int,
    limit: int,
    *,
    title: str,
    count: int | None,
    active: bool,
) -> dict[str, object]:
    endpoint = "list_publications" if query.search is None else "search_publications"
    link: dict[str, object] = {
        "href": _url_with_query(
            external_url(request, config, endpoint),
            {
                **_discovery_query_parameters(query),
                "limit": limit,
                "revision": revision,
            },
        ),
        "type": OPDS_FEED_MEDIA_TYPE,
        "title": title,
    }
    if active:
        link["rel"] = "self"
    if count is not None:
        link["properties"] = {"numberOfItems": count}
    return link


def _facet_document(
    request: Request,
    config: OPDSConfig,
    page: CatalogFacetPage,
    query: CatalogDiscoveryQuery,
    *,
    revision: int,
    limit: int,
) -> dict[str, object] | None:
    if not page.values:
        return None
    selected: str | None
    selected_namespace: str | None = None
    selected_role: str | None = None
    if page.facet is CatalogFacetKind.LANGUAGE:
        selected = query.language
    elif page.facet is CatalogFacetKind.SUBJECT:
        selected = None if query.subject is None else query.subject.value
        selected_namespace = None if query.subject is None else query.subject.namespace
    else:
        selected = None if query.contributor is None else query.contributor.name
        selected_role = None if query.contributor is None else query.contributor.role
    links = [
        _facet_link(
            request,
            config,
            query_with_facet(query, page.facet, None),
            revision,
            limit,
            title="All",
            count=None,
            active=selected is None,
        )
    ]
    for value in page.values:
        active = selected == value.value
        if page.facet is CatalogFacetKind.SUBJECT:
            active = active and selected_namespace == value.namespace
        if page.facet is CatalogFacetKind.CONTRIBUTOR:
            active = active and selected_role == (value.role or "contributor")
        links.append(
            _facet_link(
                request,
                config,
                query_with_facet(query, page.facet, value),
                revision,
                limit,
                title=value.label,
                count=value.publication_count,
                active=active,
            )
        )
    if page.next_cursor is not None:
        links.append(
            {
                "rel": "next",
                "href": _url_with_query(
                    external_url(
                        request,
                        config,
                        "facet_values",
                        facet=page.facet.value,
                    ),
                    {
                        **_discovery_query_parameters(query),
                        "cursor": encode_facet_cursor(page.next_cursor),
                        "limit": limit,
                        "revision": revision,
                    },
                ),
                "type": OPDS_FEED_MEDIA_TYPE,
                "title": f"More {_FACET_TITLES[page.facet]} values",
            }
        )
    return {"metadata": {"title": _FACET_TITLES[page.facet]}, "links": links}


def _facet_page_url(
    request: Request,
    config: OPDSConfig,
    page: CatalogFacetPage,
    *,
    cursor: CatalogFacetCursor | None,
    query: CatalogDiscoveryQuery,
) -> str:
    parameters: dict[str, str | int] = {
        **_discovery_query_parameters(query),
        "limit": page.limit,
        "revision": page.revision.revision,
    }
    if cursor is not None:
        parameters["cursor"] = encode_facet_cursor(cursor)
    return _url_with_query(
        external_url(
            request,
            config,
            "facet_values",
            facet=page.facet.value,
        ),
        parameters,
    )


def facet_navigation_document(
    request: Request,
    config: OPDSConfig,
    page: CatalogFacetPage,
    *,
    cursor: CatalogFacetCursor | None,
    query: CatalogDiscoveryQuery,
) -> dict[str, object]:
    revision = page.revision.revision
    links: list[dict[str, object]] = [
        {
            "rel": "self",
            "href": _facet_page_url(
                request,
                config,
                page,
                cursor=cursor,
                query=query,
            ),
            "type": OPDS_FEED_MEDIA_TYPE,
        },
        {
            "rel": "first",
            "href": _facet_page_url(
                request,
                config,
                page,
                cursor=None,
                query=query,
            ),
            "type": OPDS_FEED_MEDIA_TYPE,
        },
        *_common_links(request, config, revision),
    ]
    if page.next_cursor is not None:
        links.append(
            {
                "rel": "next",
                "href": _facet_page_url(
                    request,
                    config,
                    page,
                    cursor=page.next_cursor,
                    query=query,
                ),
                "type": OPDS_FEED_MEDIA_TYPE,
            }
        )

    clear_query = query_with_facet(query, page.facet, None)
    clear_endpoint = (
        "list_publications" if clear_query.search is None else "search_publications"
    )
    navigation: list[dict[str, object]] = [
        {
            "title": f"All {_FACET_TITLES[page.facet]} values",
            "href": _url_with_query(
                external_url(request, config, clear_endpoint),
                {
                    **_discovery_query_parameters(clear_query),
                    "revision": revision,
                },
            ),
            "type": OPDS_FEED_MEDIA_TYPE,
            "rel": "subsection",
        }
    ]
    for value in page.values:
        selected_query = query_with_facet(query, page.facet, value)
        selected_endpoint = (
            "list_publications"
            if selected_query.search is None
            else "search_publications"
        )
        navigation.append(
            {
                "title": value.label,
                "href": _url_with_query(
                    external_url(request, config, selected_endpoint),
                    {
                        **_discovery_query_parameters(selected_query),
                        "revision": revision,
                    },
                ),
                "type": OPDS_FEED_MEDIA_TYPE,
                "rel": "subsection",
                "properties": {"numberOfItems": value.publication_count},
            }
        )
    return {
        "metadata": {
            "@type": "http://schema.org/DataFeed",
            "title": f"{_FACET_TITLES[page.facet]} Facet Values",
            "modified": _format_datetime(page.revision.published_at),
            "itemsPerPage": page.limit,
        },
        "links": links,
        "navigation": navigation,
    }


def _empty_navigation(
    request: Request,
    config: OPDSConfig,
    revision: int,
) -> list[dict[str, object]]:
    return [
        {
            "title": "All Publications",
            "href": _url_with_query(
                external_url(request, config, "list_publications"),
                {"revision": revision},
            ),
            "type": OPDS_FEED_MEDIA_TYPE,
            "rel": "subsection",
        }
    ]


def discovery_document(
    request: Request,
    config: OPDSConfig,
    page: CatalogDiscoveryPage,
    *,
    cursor: CatalogDiscoveryCursor | None,
    query: CatalogDiscoveryQuery,
    facet_pages: tuple[CatalogFacetPage, ...],
    endpoint: str,
    title: str,
) -> dict[str, object]:
    links = _pagination_links(
        request,
        config,
        page,
        cursor=cursor,
        endpoint=endpoint,
        query=query,
    )
    links.extend(_common_links(request, config, page.revision.revision))
    metadata: dict[str, object] = {
        "@type": "http://schema.org/DataFeed",
        "title": title,
        "modified": _format_datetime(page.revision.published_at),
        "itemsPerPage": page.limit,
    }
    if page.total is not None:
        metadata["numberOfItems"] = page.total
    document: dict[str, object] = {"metadata": metadata, "links": links}
    if page.publications:
        document["publications"] = [
            publication_document(request, config, publication, page.revision.revision)
            for publication in page.publications
        ]
    else:
        document["navigation"] = _empty_navigation(
            request, config, page.revision.revision
        )
    facets = [
        facet
        for page_item in facet_pages
        if (
            facet := _facet_document(
                request,
                config,
                page_item,
                query,
                revision=page.revision.revision,
                limit=page.limit,
            )
        )
        is not None
    ]
    if facets:
        document["facets"] = facets
    return document


def recent_document(
    request: Request,
    config: OPDSConfig,
    window: CatalogRecentWindow,
    *,
    endpoint: str,
    title: str,
) -> dict[str, object]:
    revision = window.revision.revision
    links = [
        {
            "rel": "self",
            "href": _url_with_query(
                external_url(request, config, endpoint),
                {"revision": revision},
            ),
            "type": OPDS_FEED_MEDIA_TYPE,
        },
        *_common_links(request, config, revision),
    ]
    document: dict[str, object] = {
        "metadata": {
            "@type": "http://schema.org/DataFeed",
            "title": title,
            "modified": _format_datetime(window.revision.published_at),
            "numberOfItems": len(window.publications),
        },
        "links": links,
    }
    if window.publications:
        document["publications"] = [
            publication_document(request, config, publication, revision)
            for publication in window.publications
        ]
    else:
        document["navigation"] = _empty_navigation(request, config, revision)
    return document
