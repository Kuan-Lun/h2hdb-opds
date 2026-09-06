__all__ = ["BROWSE_CATEGORIES", "BrowseTarget", "browse_url"]

from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import Request
from h2hdb import CatalogTagFilter

from .config import OPDSConfig
from .urls import external_url


@dataclass(frozen=True, slots=True)
class _BrowseDefinition:
    title: str
    namespace: str
    value: str | None = None


_BROWSE_DEFINITIONS = {
    "artists": _BrowseDefinition("Artists", "artist"),
    "groups": _BrowseDefinition("Groups", "group"),
    "parodies": _BrowseDefinition("Parodies", "parody"),
    "characters": _BrowseDefinition("Characters", "character"),
    "soushuuhen": _BrowseDefinition("Soushuuhen", "other", "soushuuhen"),
    "multi-work-series": _BrowseDefinition(
        "Multi-work Series", "other", "multi-work series"
    ),
    "uncensored": _BrowseDefinition("Uncensored", "other", "uncensored"),
    "goudoushi": _BrowseDefinition("Goudoushi", "other", "goudoushi"),
}
BROWSE_CATEGORIES = {
    category: definition.title for category, definition in _BROWSE_DEFINITIONS.items()
}


@dataclass(frozen=True, slots=True)
class BrowseTarget:
    category: str
    tag: str | None = None

    def __post_init__(self) -> None:
        definition = _BROWSE_DEFINITIONS.get(self.category)
        if definition is None:
            raise LookupError("Browse category not found")
        if definition.value is not None and self.tag is not None:
            raise ValueError("tag is only supported by namespace directories")
        if self.tag is not None:
            # Public immutable values validate the exact tag bytes and bounds.
            CatalogTagFilter(
                namespace=self.namespace,
                value=self.tag,
            )

    @property
    def namespace(self) -> str:
        return _BROWSE_DEFINITIONS[self.category].namespace

    @property
    def subject(self) -> CatalogTagFilter | None:
        value = (
            self.tag
            if self.tag is not None
            else _BROWSE_DEFINITIONS[self.category].value
        )
        return (
            None
            if value is None
            else CatalogTagFilter(namespace=self.namespace, value=value)
        )

    @property
    def title(self) -> str:
        return (
            BROWSE_CATEGORIES[self.category]
            if self.tag is None
            else (self.tag or "(empty tag)")
        )


def browse_url(
    request: Request,
    config: OPDSConfig,
    target: BrowseTarget,
    *,
    endpoint: str,
    revision: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> str:
    parameters: dict[str, str | int] = {}
    if target.tag is not None:
        parameters["tag"] = target.tag
    if revision is not None:
        parameters["revision"] = revision
    if limit is not None:
        parameters["limit"] = limit
    if cursor is not None:
        parameters["cursor"] = cursor
    url = external_url(request, config, endpoint, category=target.category)
    return f"{url}?{urlencode(parameters)}" if parameters else url
