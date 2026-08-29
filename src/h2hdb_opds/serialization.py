__all__ = [
    "OPDS_FEED_MEDIA_TYPE",
    "OPDS_PUBLICATION_MEDIA_TYPE",
    "navigation_document",
    "publication_document",
    "publications_document",
]

from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import Request
from h2hdb import (
    CatalogArtifactCursor,
    CatalogArtifactPage,
    CatalogPublication,
    CatalogRevision,
)

from .auth import AUTHENTICATION_DOCUMENT_REL, AUTHENTICATION_MEDIA_TYPE
from .config import OPDSConfig
from .cursor import encode_artifact_cursor
from .language import normalize_bcp47
from .urls import external_url

OPDS_FEED_MEDIA_TYPE = "application/opds+json"
OPDS_PUBLICATION_MEDIA_TYPE = "application/opds-publication+json"
ACQUISITION_REL = "http://opds-spec.org/acquisition"


def _format_datetime(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_trimmed(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _url_with_query(url: str, parameters: dict[str, str | int]) -> str:
    return f"{url}?{urlencode(parameters)}"


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


def navigation_document(
    request: Request,
    config: OPDSConfig,
    revision: CatalogRevision,
    publication_count: int,
) -> dict[str, object]:
    self_url = _url_with_query(
        external_url(request, config, "navigation"),
        {"revision": revision.revision},
    )
    links = [
        {
            "rel": "self",
            "href": self_url,
            "type": OPDS_FEED_MEDIA_TYPE,
        },
        *_common_links(request, config, revision.revision),
    ]
    return {
        "metadata": {
            "@type": "http://schema.org/DataFeed",
            "title": config.title,
            "modified": _format_datetime(revision.published_at),
            "numberOfItems": publication_count,
        },
        "links": links,
        "navigation": [
            {
                "title": "All Publications",
                "href": _url_with_query(
                    external_url(request, config, "list_publications"),
                    {"revision": revision.revision},
                ),
                "type": OPDS_FEED_MEDIA_TYPE,
                "rel": "subsection",
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
        serialized = {"name": name, "role": role or "contributor"}
        contributors.setdefault(key, []).append(serialized)
    result: dict[str, object] = {}
    for key, value in contributors.items():
        result[key] = value
    return result


def publication_document(
    request: Request,
    config: OPDSConfig,
    publication: CatalogPublication,
    revision: int,
) -> dict[str, object]:
    title = publication.title.strip() or publication.publication_id
    metadata: dict[str, object] = {
        "@type": "http://schema.org/Book",
        "identifier": publication.publication_id,
        "title": title,
        "published": _format_datetime(publication.published_at),
        "modified": _format_datetime(publication.modified_at),
        "subject": [
            {
                key: value
                for key, value in {
                    "name": subject.name.strip(),
                    "scheme": _optional_trimmed(subject.scheme),
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
    if not metadata["subject"]:
        del metadata["subject"]
    links: list[dict[str, object]] = [
        {
            "rel": "self",
            "href": _url_with_query(
                str(
                    external_url(
                        request,
                        config,
                        "get_publication",
                        publication_id=publication.publication_id,
                    )
                ),
                {"revision": revision},
            ),
            "type": OPDS_PUBLICATION_MEDIA_TYPE,
        }
    ]
    links.extend(
        {
            "rel": ACQUISITION_REL,
            "href": _url_with_query(
                str(
                    external_url(
                        request,
                        config,
                        "acquire_artifact",
                        artifact_id=artifact.artifact_id,
                    )
                ),
                {"revision": revision},
            ),
            "type": artifact.media_type,
            **({"title": artifact.name.strip()} if artifact.name.strip() else {}),
            **({"size": artifact.size_bytes} if artifact.size_bytes > 0 else {}),
        }
        for artifact in publication.artifacts
    )
    return {"metadata": metadata, "links": links}


def _pagination_links(
    request: Request,
    page: CatalogArtifactPage,
    *,
    cursor: CatalogArtifactCursor | None,
    endpoint: str,
    config: OPDSConfig,
) -> list[dict[str, object]]:
    base_url = external_url(request, config, endpoint)

    def page_url(selected_cursor: CatalogArtifactCursor | None) -> str:
        parameters: dict[str, str | int] = {
            "limit": page.limit,
            "revision": page.revision.revision,
        }
        if selected_cursor is not None:
            parameters["cursor"] = encode_artifact_cursor(selected_cursor)
        return _url_with_query(base_url, parameters)

    links: list[dict[str, object]] = [
        {
            "rel": "self",
            "href": page_url(cursor),
            "type": OPDS_FEED_MEDIA_TYPE,
        },
        {
            "rel": "first",
            "href": page_url(None),
            "type": OPDS_FEED_MEDIA_TYPE,
        },
    ]
    if page.next_cursor is not None:
        links.append(
            {
                "rel": "next",
                "href": page_url(page.next_cursor),
                "type": OPDS_FEED_MEDIA_TYPE,
            }
        )
    return links


def publications_document(
    request: Request,
    config: OPDSConfig,
    page: CatalogArtifactPage,
    *,
    cursor: CatalogArtifactCursor | None,
    endpoint: str,
) -> dict[str, object]:
    links = _pagination_links(
        request,
        page,
        cursor=cursor,
        endpoint=endpoint,
        config=config,
    )
    links.extend(_common_links(request, config, page.revision.revision))
    return {
        "metadata": {
            "@type": "http://schema.org/DataFeed",
            "title": "All Publications",
            "modified": _format_datetime(page.revision.published_at),
            "numberOfItems": page.total,
            "itemsPerPage": page.limit,
        },
        "links": links,
        "publications": [
            publication_document(request, config, publication, page.revision.revision)
            for publication in page.publications
        ],
    }
