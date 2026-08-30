__all__ = [
    "ATOM_NAMESPACE",
    "DC_TERMS_NAMESPACE",
    "OPDS12_ACQUISITION_MEDIA_TYPE",
    "OPDS_ACQUISITION_REL",
    "OPDS_NAMESPACE",
    "acquisition_feed_document",
]

import re
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

from fastapi import Request
from h2hdb import (
    CatalogArtifactCursor,
    CatalogArtifactPage,
    CatalogPublication,
)

from .config import OPDSConfig
from .cursor import encode_artifact_cursor
from .language import normalize_bcp47
from .urls import external_url

ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
DC_TERMS_NAMESPACE = "http://purl.org/dc/terms/"
OPDS_NAMESPACE = "http://opds-spec.org/2010/catalog"
OPDS12_ACQUISITION_MEDIA_TYPE = (
    "application/atom+xml;profile=opds-catalog;kind=acquisition"
)
OPDS_ACQUISITION_REL = "http://opds-spec.org/acquisition"

_CREATOR_ROLES = frozenset({"artist", "author", "illustrator"})
_INVALID_XML10_CHARACTER = re.compile(
    "[^\\x09\\x0A\\x0D\\x20-\\uD7FF\\uE000-\\uFFFD\\U00010000-\\U0010FFFF]"
)

ElementTree.register_namespace("", ATOM_NAMESPACE)
ElementTree.register_namespace("dc", DC_TERMS_NAMESPACE)
ElementTree.register_namespace("opds", OPDS_NAMESPACE)


def _atom(local_name: str) -> str:
    return f"{{{ATOM_NAMESPACE}}}{local_name}"


def _dc(local_name: str) -> str:
    return f"{{{DC_TERMS_NAMESPACE}}}{local_name}"


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
    return f"{url}?{urlencode(parameters)}"


def _page_url(
    base_url: str,
    page: CatalogArtifactPage,
    cursor: CatalogArtifactCursor | None,
) -> str:
    parameters: dict[str, str | int] = {
        "limit": page.limit,
        "revision": page.revision.revision,
    }
    if cursor is not None:
        parameters["cursor"] = encode_artifact_cursor(cursor)
    return _url_with_query(base_url, parameters)


def _link(
    parent: ElementTree.Element,
    *,
    relation: str,
    href: str,
    media_type: str,
    title: str | None = None,
    length: int | None = None,
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
    ElementTree.SubElement(parent, _atom("link"), attributes)


def _person(
    parent: ElementTree.Element,
    element_name: str,
    person_name: str,
) -> None:
    person = ElementTree.SubElement(parent, _atom(element_name))
    _text_element(person, "name", person_name)


def _entry_id(publication_id: str) -> str:
    return f"urn:h2hdb:publication:{quote(_xml_text(publication_id), safe='')}"


def _publication_entry(
    publication: CatalogPublication,
    *,
    request: Request,
    config: OPDSConfig,
    revision: int,
    acquisition_endpoint: str,
) -> ElementTree.Element:
    if not publication.artifacts:
        raise ValueError(
            "an OPDS 1.2 acquisition-feed entry requires at least one artifact"
        )

    entry = ElementTree.Element(_atom("entry"))
    title = publication.title.strip() or publication.publication_id
    _text_element(entry, "title", title)
    _text_element(entry, "id", _entry_id(publication.publication_id))
    _text_element(entry, "updated", _format_datetime(publication.modified_at))

    for contributor in publication.contributors:
        name = contributor.name.strip()
        if not name:
            continue
        role = contributor.role.strip().casefold()
        element_name = "author" if role in _CREATOR_ROLES else "contributor"
        _person(entry, element_name, name)

    _text_element(
        entry,
        "identifier",
        publication.publication_id,
        namespace=DC_TERMS_NAMESPACE,
    )
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
        scheme = subject.scheme.strip() if subject.scheme is not None else ""
        if scheme and urlsplit(scheme).scheme:
            attributes["scheme"] = _xml_text(scheme)
        ElementTree.SubElement(entry, _atom("category"), attributes)

    summary = publication.summary.strip()
    _text_element(
        entry,
        "content",
        summary or title,
        attributes={"type": "text"},
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
            relation=OPDS_ACQUISITION_REL,
            href=_url_with_query(artifact_url, {"revision": revision}),
            media_type=artifact.media_type,
            title=artifact_name or None,
            length=artifact.size_bytes if artifact.size_bytes > 0 else None,
        )
    return entry


def acquisition_feed_document(
    request: Request,
    config: OPDSConfig,
    page: CatalogArtifactPage,
    *,
    cursor: CatalogArtifactCursor | None,
    endpoint: str = "opds12_catalog",
    acquisition_endpoint: str = "opds12_acquire_artifact",
    title: str = "All Publications",
) -> bytes:
    """Serialize one revision-pinned page as an OPDS 1.2 acquisition feed."""
    revision = page.revision.revision
    for selected_cursor in (cursor, page.next_cursor):
        if selected_cursor is not None and selected_cursor.revision != revision:
            raise ValueError("OPDS 1.2 pagination cursor does not match page revision")

    feed_url = external_url(request, config, endpoint)
    feed = ElementTree.Element(_atom("feed"), {"xmlns:opds": OPDS_NAMESPACE})
    _text_element(feed, "id", feed_url)
    _text_element(feed, "title", title)
    _text_element(feed, "updated", _format_datetime(page.revision.published_at))
    _person(feed, "author", config.title)

    first_url = _page_url(feed_url, page, None)
    _link(
        feed,
        relation="self",
        href=_page_url(feed_url, page, cursor),
        media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
    )
    _link(
        feed,
        relation="start",
        href=first_url,
        media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
    )
    _link(
        feed,
        relation="first",
        href=first_url,
        media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
    )
    if page.next_cursor is not None:
        _link(
            feed,
            relation="next",
            href=_page_url(feed_url, page, page.next_cursor),
            media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
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

    return cast(
        bytes,
        ElementTree.tostring(feed, encoding="utf-8", xml_declaration=True),
    )
