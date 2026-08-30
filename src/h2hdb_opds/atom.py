__all__ = [
    "ATOM_NAMESPACE",
    "DC_TERMS_NAMESPACE",
    "OPDS12_ACQUISITION_MEDIA_TYPE",
    "OPDS12_NAVIGATION_MEDIA_TYPE",
    "OPDS12_RECENT_LIMIT",
    "OPDS_ACQUISITION_REL",
    "OPDS_NAMESPACE",
    "OPDS_SORT_NEW_REL",
    "navigation_feed_document",
    "recent_acquisition_feed_document",
]

import re
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

from fastapi import Request
from h2hdb import (
    CatalogPublication,
    CatalogRecentArtifactWindow,
    CatalogRevision,
)

from .config import OPDSConfig
from .language import normalize_bcp47
from .urls import external_url

ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
DC_TERMS_NAMESPACE = "http://purl.org/dc/terms/"
OPDS_NAMESPACE = "http://opds-spec.org/2010/catalog"
OPDS12_ACQUISITION_MEDIA_TYPE = (
    "application/atom+xml;profile=opds-catalog;kind=acquisition"
)
OPDS12_NAVIGATION_MEDIA_TYPE = (
    "application/atom+xml;profile=opds-catalog;kind=navigation"
)
OPDS_ACQUISITION_REL = "http://opds-spec.org/acquisition"
OPDS_SORT_NEW_REL = "http://opds-spec.org/sort/new"
OPDS12_RECENT_LIMIT = 128

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


def _feed(
    *,
    identifier: str,
    title: str,
    updated: datetime,
    author: str,
) -> ElementTree.Element:
    feed = ElementTree.Element(_atom("feed"), {"xmlns:opds": OPDS_NAMESPACE})
    _text_element(feed, "id", identifier)
    _text_element(feed, "title", title)
    _text_element(feed, "updated", _format_datetime(updated))
    _person(feed, "author", author)
    return feed


def _serialized(feed: ElementTree.Element) -> bytes:
    return cast(
        bytes,
        ElementTree.tostring(feed, encoding="utf-8", xml_declaration=True),
    )


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
    """Serialize the two-entry OPDS 1.2 catalog root."""
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
    feed.append(
        _navigation_entry(
            title="Recently Uploaded",
            identifier="urn:h2hdb:navigation:recently-uploaded",
            description="Up to 128 publications with the latest source upload times.",
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
            description=(
                "Up to 128 publications with the latest source download times."
            ),
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


def recent_acquisition_feed_document(
    request: Request,
    config: OPDSConfig,
    window: CatalogRecentArtifactWindow,
    *,
    endpoint: str,
    title: str,
    acquisition_endpoint: str = "opds12_acquire_artifact",
) -> bytes:
    """Serialize one complete, revision-pinned, hard-capped recent window."""
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
