__all__ = [
    "ATOM_NAMESPACE",
    "DC_TERMS_NAMESPACE",
    "OPDS12_ACQUISITION_MEDIA_TYPE",
    "OPDS12_ENTRY_MEDIA_TYPE",
    "OPDS12_NAVIGATION_MEDIA_TYPE",
    "OPDS12_RECENT_LIMIT",
    "OPDS_NAMESPACE",
    "OPDS_SORT_NEW_REL",
    "OPEN_SEARCH_MEDIA_TYPE",
    "OPEN_SEARCH_NAMESPACE",
    "PSE_NAMESPACE",
    "acquisition_feed_document",
    "facet_navigation_feed_document",
    "navigation_feed_document",
    "opensearch_description_document",
    "publication_entry_document",
    "recent_acquisition_feed_document",
]

import re
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlencode
from xml.etree import ElementTree

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

from .config import OPDSConfig
from .cursor import encode_discovery_cursor, encode_facet_cursor
from .discovery import (
    discovery_query_parameters,
    query_with_facet,
)
from .language import normalize_bcp47
from .publication import acquisition_relation, publication_identifier
from .uri import optional_uri
from .urls import external_url

ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
DC_TERMS_NAMESPACE = "http://purl.org/dc/terms/"
OPDS_NAMESPACE = "http://opds-spec.org/2010/catalog"
OPEN_SEARCH_NAMESPACE = "http://a9.com/-/spec/opensearch/1.1/"
PSE_NAMESPACE = "http://vaemendis.net/opds-pse/ns"
THREADING_NAMESPACE = "http://purl.org/syndication/thread/1.0"
OPDS12_ACQUISITION_MEDIA_TYPE = (
    "application/atom+xml;profile=opds-catalog;kind=acquisition"
)
OPDS12_NAVIGATION_MEDIA_TYPE = (
    "application/atom+xml;profile=opds-catalog;kind=navigation"
)
OPDS12_ENTRY_MEDIA_TYPE = "application/atom+xml;type=entry;profile=opds-catalog"
OPEN_SEARCH_MEDIA_TYPE = "application/opensearchdescription+xml"
OPDS_SORT_NEW_REL = "http://opds-spec.org/sort/new"
OPDS_IMAGE_REL = "http://opds-spec.org/image"
OPDS_THUMBNAIL_REL = "http://opds-spec.org/image/thumbnail"
OPDS_FACET_REL = "http://opds-spec.org/facet"
PSE_STREAM_REL = "http://vaemendis.net/opds-pse/stream"
OPDS12_RECENT_LIMIT = 128

_CREATOR_ROLES = frozenset({"artist", "author", "illustrator"})
_INVALID_XML10_CHARACTER = re.compile(
    "[^\\x09\\x0A\\x0D\\x20-\\uD7FF\\uE000-\\uFFFD\\U00010000-\\U0010FFFF]"
)
_FACET_TITLES = {
    CatalogFacetKind.LANGUAGE: "Language",
    CatalogFacetKind.SUBJECT: "Tag",
    CatalogFacetKind.CONTRIBUTOR: "Contributor",
}

ElementTree.register_namespace("", ATOM_NAMESPACE)
ElementTree.register_namespace("dc", DC_TERMS_NAMESPACE)
ElementTree.register_namespace("opds", OPDS_NAMESPACE)
ElementTree.register_namespace("opensearch", OPEN_SEARCH_NAMESPACE)
ElementTree.register_namespace("pse", PSE_NAMESPACE)
ElementTree.register_namespace("thr", THREADING_NAMESPACE)


def _atom(local_name: str) -> str:
    return f"{{{ATOM_NAMESPACE}}}{local_name}"


def _format_datetime(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _xml_text(value: str) -> str:
    return _INVALID_XML10_CHARACTER.sub("\N{REPLACEMENT CHARACTER}", value)


def _text_element(
    parent: ElementTree.Element,
    name: str,
    value: str,
    *,
    namespace: str = ATOM_NAMESPACE,
    attributes: dict[str, str] | None = None,
) -> ElementTree.Element:
    element = ElementTree.SubElement(
        parent,
        f"{{{namespace}}}{name}",
        {} if attributes is None else attributes,
    )
    element.text = _xml_text(value)
    return element


def _url_with_query(url: str, parameters: dict[str, str | int]) -> str:
    return url if not parameters else f"{url}?{urlencode(parameters)}"


def _link(
    parent: ElementTree.Element,
    *,
    relation: str,
    href: str,
    media_type: str,
    title: str | None = None,
    length: int | None = None,
    extra_attributes: dict[str, str] | None = None,
) -> None:
    attributes = {
        "rel": _xml_text(relation),
        "href": _xml_text(href),
        "type": _xml_text(media_type),
    }
    if title is not None:
        attributes["title"] = _xml_text(title)
    if length is not None:
        attributes["length"] = str(length)
    if extra_attributes is not None:
        attributes.update(extra_attributes)
    ElementTree.SubElement(parent, _atom("link"), attributes)


def _person(
    parent: ElementTree.Element,
    element_name: str,
    person_name: str,
) -> None:
    person = ElementTree.SubElement(parent, _atom(element_name))
    _text_element(person, "name", person_name)


def _revision_url(
    request: Request,
    config: OPDSConfig,
    endpoint: str,
    revision: int,
) -> str:
    return _url_with_query(
        external_url(request, config, endpoint),
        {"revision": revision},
    )


def _media_url(
    request: Request,
    config: OPDSConfig,
    endpoint: str,
    publication_id: str,
    revision: int,
    *,
    page_number: int | None = None,
) -> str:
    path_parameters: dict[str, object] = {"publication_id": publication_id}
    if page_number is not None:
        path_parameters["page_number"] = page_number
    return _url_with_query(
        external_url(request, config, endpoint, **path_parameters),
        {"revision": revision},
    )


def _image_link(
    entry: ElementTree.Element,
    resource: CatalogImageResource | None,
    *,
    relation: str,
    href: str,
) -> None:
    if resource is None:
        return
    _link(
        entry,
        relation=relation,
        href=href,
        media_type=resource.media_type,
        length=resource.extent.length,
    )


def _publication_entry(
    publication: CatalogPublication,
    *,
    request: Request,
    config: OPDSConfig,
    revision: int,
    acquisition_endpoint: str,
    standalone: bool = False,
) -> ElementTree.Element:
    if not publication.artifacts:
        raise ValueError("an OPDS acquisition entry requires at least one artifact")

    identifier = publication_identifier(
        publication.publication_id,
        expected_gid=publication.gid,
    )
    entry = ElementTree.Element(_atom("entry"))
    title = publication.title.strip() or identifier
    _text_element(entry, "title", title)
    _text_element(entry, "id", identifier)
    _text_element(entry, "updated", _format_datetime(publication.modified_at))

    has_author = False
    for contributor in publication.contributors:
        name = contributor.name.strip()
        if not name:
            continue
        role = contributor.role.strip().casefold()
        element_name = "author" if role in _CREATOR_ROLES else "contributor"
        _person(entry, element_name, name)
        has_author = has_author or element_name == "author"
    if standalone and not has_author:
        _person(entry, "author", config.title)

    _text_element(entry, "identifier", identifier, namespace=DC_TERMS_NAMESPACE)
    language = normalize_bcp47(publication.language)
    if language is not None:
        _text_element(entry, "language", language, namespace=DC_TERMS_NAMESPACE)
    _text_element(
        entry,
        "issued",
        _format_datetime(publication.published_at),
        namespace=DC_TERMS_NAMESPACE,
    )

    for subject in publication.subjects:
        label = subject.name.strip()
        if not label:
            continue
        term = subject.code.strip() if subject.code is not None else ""
        term = term or label
        attributes = {"term": _xml_text(term), "label": _xml_text(label)}
        scheme = optional_uri(subject.scheme)
        if scheme is not None:
            attributes["scheme"] = _xml_text(scheme)
        ElementTree.SubElement(entry, _atom("category"), attributes)

    summary = publication.summary.strip()
    _text_element(entry, "content", summary or title, attributes={"type": "text"})

    standalone_url = _media_url(
        request,
        config,
        "opds12_publication",
        publication.publication_id,
        revision,
    )
    _link(
        entry,
        relation="self" if standalone else "alternate",
        href=standalone_url,
        media_type=OPDS12_ENTRY_MEDIA_TYPE,
    )
    for artifact in publication.artifacts:
        artifact_url = external_url(
            request,
            config,
            acquisition_endpoint,
            artifact_id=artifact.artifact_id,
        )
        artifact_name = artifact.name.strip()
        _link(
            entry,
            relation=acquisition_relation(config),
            href=_url_with_query(artifact_url, {"revision": revision}),
            media_type=artifact.media_type,
            title=artifact_name or None,
            length=artifact.storage_object.size_bytes,
        )

    if publication.page_count > 0:
        cover_url = _media_url(
            request,
            config,
            "publication_page",
            publication.publication_id,
            revision,
            page_number=0,
        )
        thumbnail_url = _media_url(
            request,
            config,
            "publication_thumbnail",
            publication.publication_id,
            revision,
        )
        _image_link(
            entry,
            publication.cover,
            relation=OPDS_IMAGE_REL,
            href=cover_url,
        )
        _image_link(
            entry,
            publication.thumbnail,
            relation=OPDS_THUMBNAIL_REL,
            href=thumbnail_url,
        )
        page_zero = external_url(
            request,
            config,
            "publication_page",
            publication_id=publication.publication_id,
            page_number=0,
        )
        if not page_zero.endswith("/0"):
            raise ValueError("publication page route does not end in its page number")
        template = _url_with_query(
            f"{page_zero[:-1]}{{pageNumber}}",
            {"revision": revision},
        )
        _link(
            entry,
            relation=PSE_STREAM_REL,
            href=template,
            media_type="image/jpeg",
            extra_attributes={f"{{{PSE_NAMESPACE}}}count": str(publication.page_count)},
        )
    return entry


def _feed(
    *,
    identifier: str,
    title: str,
    updated: datetime,
    author: str,
) -> ElementTree.Element:
    feed = ElementTree.Element(_atom("feed"))
    _text_element(feed, "id", identifier)
    _text_element(feed, "title", title)
    _text_element(feed, "updated", _format_datetime(updated))
    _person(feed, "author", author)
    return feed


def _serialized(root: ElementTree.Element) -> bytes:
    return cast(
        bytes,
        ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
    )


def _search_description_link(
    feed: ElementTree.Element,
    request: Request,
    config: OPDSConfig,
    revision: int,
) -> None:
    _link(
        feed,
        relation="search",
        href=_revision_url(request, config, "opds12_opensearch", revision),
        media_type=OPEN_SEARCH_MEDIA_TYPE,
        title="Search",
    )


def _navigation_entry(
    *,
    title: str,
    identifier: str,
    description: str,
    updated: datetime,
    relation: str,
    href: str,
) -> ElementTree.Element:
    entry = ElementTree.Element(_atom("entry"))
    _text_element(entry, "title", title)
    _text_element(entry, "id", identifier)
    _text_element(entry, "updated", _format_datetime(updated))
    _text_element(entry, "content", description, attributes={"type": "text"})
    _link(
        entry,
        relation=relation,
        href=href,
        media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
        title=title,
    )
    return entry


def navigation_feed_document(
    request: Request,
    config: OPDSConfig,
    revision: CatalogRevision,
) -> bytes:
    selected_revision = revision.revision
    feed_url = external_url(request, config, "opds12_catalog")
    self_url = _url_with_query(feed_url, {"revision": selected_revision})
    feed = _feed(
        identifier=feed_url,
        title=config.title,
        updated=revision.published_at,
        author=config.title,
    )
    _link(
        feed,
        relation="self",
        href=self_url,
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    _link(
        feed,
        relation="start",
        href=self_url,
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    _search_description_link(feed, request, config, selected_revision)
    feed.append(
        _navigation_entry(
            title="All Publications",
            identifier="urn:h2hdb:navigation:all-publications",
            description="Every downloadable publication in the current catalog.",
            updated=revision.published_at,
            relation="subsection",
            href=_revision_url(
                request,
                config,
                "opds12_publications",
                selected_revision,
            ),
        )
    )
    feed.append(
        _navigation_entry(
            title="Recently Uploaded",
            identifier="urn:h2hdb:navigation:recently-uploaded",
            description="Up to 128 publications with the latest upload times.",
            updated=revision.published_at,
            relation=OPDS_SORT_NEW_REL,
            href=_revision_url(
                request,
                config,
                "opds12_recent_uploaded",
                selected_revision,
            ),
        )
    )
    feed.append(
        _navigation_entry(
            title="Recently Downloaded",
            identifier="urn:h2hdb:navigation:recently-downloaded",
            description="Up to 128 publications with the latest download times.",
            updated=revision.published_at,
            relation="subsection",
            href=_revision_url(
                request,
                config,
                "opds12_recent_downloaded",
                selected_revision,
            ),
        )
    )
    return _serialized(feed)


def _page_url(
    request: Request,
    config: OPDSConfig,
    page: CatalogDiscoveryPage,
    *,
    endpoint: str,
    cursor: CatalogDiscoveryCursor | None,
    query: CatalogDiscoveryQuery,
) -> str:
    parameters: dict[str, str | int] = {
        **discovery_query_parameters(query),
        "limit": page.limit,
        "revision": page.revision.revision,
    }
    if cursor is not None:
        parameters["cursor"] = encode_discovery_cursor(cursor)
    return _url_with_query(external_url(request, config, endpoint), parameters)


def _facet_links(
    feed: ElementTree.Element,
    request: Request,
    config: OPDSConfig,
    pages: tuple[CatalogFacetPage, ...],
    *,
    endpoint: str,
    query: CatalogDiscoveryQuery,
    revision: int,
    limit: int,
) -> None:
    for page in pages:
        if not page.values:
            continue
        title = _FACET_TITLES[page.facet]
        clear_query = query_with_facet(query, page.facet, None)
        clear_parameters: dict[str, str | int] = {
            **discovery_query_parameters(clear_query),
            "limit": limit,
            "revision": revision,
        }
        selected_value = {
            CatalogFacetKind.LANGUAGE: query.language,
            CatalogFacetKind.SUBJECT: (
                None if query.subject is None else query.subject.value
            ),
            CatalogFacetKind.CONTRIBUTOR: (
                None if query.contributor is None else query.contributor.name
            ),
        }[page.facet]
        _link(
            feed,
            relation=OPDS_FACET_REL,
            href=_url_with_query(
                external_url(request, config, endpoint), clear_parameters
            ),
            media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
            title="All",
            extra_attributes={
                f"{{{OPDS_NAMESPACE}}}facetGroup": title,
                **(
                    {f"{{{OPDS_NAMESPACE}}}activeFacet": "true"}
                    if selected_value is None
                    else {}
                ),
            },
        )
        for value in page.values:
            selected_query = query_with_facet(query, page.facet, value)
            parameters: dict[str, str | int] = {
                **discovery_query_parameters(selected_query),
                "limit": limit,
                "revision": revision,
            }
            active = selected_value == value.value
            if page.facet is CatalogFacetKind.SUBJECT and query.subject:
                active = active and query.subject.namespace == value.namespace
            if page.facet is CatalogFacetKind.CONTRIBUTOR and query.contributor:
                active = active and query.contributor.role == (
                    value.role or "contributor"
                )
            _link(
                feed,
                relation=OPDS_FACET_REL,
                href=_url_with_query(
                    external_url(request, config, endpoint), parameters
                ),
                media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
                title=value.label,
                extra_attributes={
                    f"{{{OPDS_NAMESPACE}}}facetGroup": title,
                    f"{{{THREADING_NAMESPACE}}}count": str(value.publication_count),
                    **({f"{{{OPDS_NAMESPACE}}}activeFacet": "true"} if active else {}),
                },
            )
        if page.next_cursor is not None:
            _link(
                feed,
                relation="subsection",
                href=_url_with_query(
                    external_url(
                        request,
                        config,
                        "opds12_facet_values",
                        facet=page.facet.value,
                    ),
                    {
                        **discovery_query_parameters(query),
                        "cursor": encode_facet_cursor(page.next_cursor),
                        "limit": limit,
                        "revision": revision,
                    },
                ),
                media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
                title=f"More {title} values",
            )


def _facet_page_url(
    request: Request,
    config: OPDSConfig,
    page: CatalogFacetPage,
    *,
    cursor: CatalogFacetCursor | None,
    query: CatalogDiscoveryQuery,
) -> str:
    parameters: dict[str, str | int] = {
        **discovery_query_parameters(query),
        "limit": page.limit,
        "revision": page.revision.revision,
    }
    if cursor is not None:
        parameters["cursor"] = encode_facet_cursor(cursor)
    return _url_with_query(
        external_url(
            request,
            config,
            "opds12_facet_values",
            facet=page.facet.value,
        ),
        parameters,
    )


def facet_navigation_feed_document(
    request: Request,
    config: OPDSConfig,
    page: CatalogFacetPage,
    *,
    cursor: CatalogFacetCursor | None,
    query: CatalogDiscoveryQuery,
) -> bytes:
    revision = page.revision.revision
    feed_url = external_url(
        request,
        config,
        "opds12_facet_values",
        facet=page.facet.value,
    )
    root_url = _revision_url(request, config, "opds12_catalog", revision)
    title = _FACET_TITLES[page.facet]
    feed = _feed(
        identifier=feed_url,
        title=f"{title} Facet Values",
        updated=page.revision.published_at,
        author=config.title,
    )
    _link(
        feed,
        relation="self",
        href=_facet_page_url(
            request,
            config,
            page,
            cursor=cursor,
            query=query,
        ),
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    _link(
        feed,
        relation="first",
        href=_facet_page_url(
            request,
            config,
            page,
            cursor=None,
            query=query,
        ),
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    if page.next_cursor is not None:
        _link(
            feed,
            relation="next",
            href=_facet_page_url(
                request,
                config,
                page,
                cursor=page.next_cursor,
                query=query,
            ),
            media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
        )
    _link(
        feed,
        relation="start",
        href=root_url,
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    _search_description_link(feed, request, config, revision)

    selections = [(f"All {title} values", query_with_facet(query, page.facet, None))]
    selections.extend(
        (value.label, query_with_facet(query, page.facet, value))
        for value in page.values
    )
    for label, selected_query in selections:
        endpoint = (
            "opds12_publications" if selected_query.search is None else "opds12_search"
        )
        href = _url_with_query(
            external_url(request, config, endpoint),
            {
                **discovery_query_parameters(selected_query),
                "revision": revision,
            },
        )
        feed.append(
            _navigation_entry(
                title=label,
                identifier=href,
                description=f"Browse publications matching {label}.",
                updated=page.revision.published_at,
                relation="subsection",
                href=href,
            )
        )
    return _serialized(feed)


def acquisition_feed_document(
    request: Request,
    config: OPDSConfig,
    page: CatalogDiscoveryPage,
    *,
    cursor: CatalogDiscoveryCursor | None,
    query: CatalogDiscoveryQuery,
    facet_pages: tuple[CatalogFacetPage, ...],
    endpoint: str,
    title: str,
    acquisition_endpoint: str = "opds12_acquire_artifact",
) -> bytes:
    feed_url = external_url(request, config, endpoint)
    revision = page.revision.revision
    root_url = _revision_url(request, config, "opds12_catalog", revision)
    feed = _feed(
        identifier=feed_url,
        title=title,
        updated=page.revision.published_at,
        author=config.title,
    )
    _link(
        feed,
        relation="self",
        href=_page_url(
            request,
            config,
            page,
            endpoint=endpoint,
            cursor=cursor,
            query=query,
        ),
        media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
    )
    _link(
        feed,
        relation="first",
        href=_page_url(
            request,
            config,
            page,
            endpoint=endpoint,
            cursor=None,
            query=query,
        ),
        media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
    )
    if page.next_cursor is not None:
        _link(
            feed,
            relation="next",
            href=_page_url(
                request,
                config,
                page,
                endpoint=endpoint,
                cursor=page.next_cursor,
                query=query,
            ),
            media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
        )
    _link(
        feed,
        relation="start",
        href=root_url,
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    _link(
        feed,
        relation="up",
        href=root_url,
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    _search_description_link(feed, request, config, revision)
    _facet_links(
        feed,
        request,
        config,
        facet_pages,
        endpoint=endpoint,
        query=query,
        revision=revision,
        limit=page.limit,
    )
    if page.total is not None:
        _text_element(
            feed,
            "totalResults",
            str(page.total),
            namespace=OPEN_SEARCH_NAMESPACE,
        )
    _text_element(
        feed,
        "itemsPerPage",
        str(page.limit),
        namespace=OPEN_SEARCH_NAMESPACE,
    )
    for publication in page.publications:
        feed.append(
            _publication_entry(
                publication,
                request=request,
                config=config,
                revision=revision,
                acquisition_endpoint=acquisition_endpoint,
            )
        )
    return _serialized(feed)


def recent_acquisition_feed_document(
    request: Request,
    config: OPDSConfig,
    window: CatalogRecentWindow,
    *,
    endpoint: str,
    title: str,
    acquisition_endpoint: str = "opds12_acquire_artifact",
) -> bytes:
    if len(window.publications) > OPDS12_RECENT_LIMIT:
        raise ValueError("OPDS 1.2 recent feed exceeds its hard limit")

    revision = window.revision.revision
    feed_url = external_url(request, config, endpoint)
    root_url = _revision_url(request, config, "opds12_catalog", revision)
    feed = _feed(
        identifier=feed_url,
        title=title,
        updated=window.revision.published_at,
        author=config.title,
    )
    _link(
        feed,
        relation="self",
        href=_url_with_query(feed_url, {"revision": revision}),
        media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
    )
    _link(
        feed,
        relation="start",
        href=root_url,
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    _link(
        feed,
        relation="up",
        href=root_url,
        media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
    )
    _search_description_link(feed, request, config, revision)
    for publication in window.publications:
        feed.append(
            _publication_entry(
                publication,
                request=request,
                config=config,
                revision=revision,
                acquisition_endpoint=acquisition_endpoint,
            )
        )
    return _serialized(feed)


def publication_entry_document(
    request: Request,
    config: OPDSConfig,
    publication: CatalogPublication,
    revision: int,
) -> bytes:
    return _serialized(
        _publication_entry(
            publication,
            request=request,
            config=config,
            revision=revision,
            acquisition_endpoint="opds12_acquire_artifact",
            standalone=True,
        )
    )


def opensearch_description_document(
    request: Request,
    config: OPDSConfig,
    revision: CatalogRevision,
) -> bytes:
    root = ElementTree.Element(
        "OpenSearchDescription",
        {"xmlns": OPEN_SEARCH_NAMESPACE},
    )
    # Panels requires unprefixed OpenSearch element names. Declare the namespace
    # on this document without changing ElementTree's shared Atom registration.
    short_name = config.title.strip()[:16] or "H2HDB"
    ElementTree.SubElement(root, "ShortName").text = _xml_text(short_name)
    ElementTree.SubElement(root, "Description").text = _xml_text(
        f"Search {config.title}"
    )
    search_url = external_url(request, config, "opds12_search")
    # Reader search boxes only need a query. Exact filters and page limits remain
    # available through the API and concrete facet/pagination links.
    template = f"{search_url}?q={{searchTerms}}&revision={revision.revision}"
    ElementTree.SubElement(
        root,
        "Url",
        {"type": OPDS12_ACQUISITION_MEDIA_TYPE, "template": template},
    )
    ElementTree.SubElement(root, "InputEncoding").text = "UTF-8"
    ElementTree.SubElement(root, "OutputEncoding").text = "UTF-8"
    return _serialized(root)
