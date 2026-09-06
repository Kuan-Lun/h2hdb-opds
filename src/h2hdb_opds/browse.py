__all__ = ["BROWSE_CATEGORIES", "BrowseTarget", "browse_url"]

from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import Request
from h2hdb import CatalogTagFilter

from .config import OPDSConfig
from .urls import external_url

BROWSE_CATEGORIES = {
    "artists": "Artists",
    "groups": "Groups",
    "soushuuhen": "Soushuuhen",
    "multi-work-series": "Multi-work Series",
    "uncensored": "Uncensored",
}


@dataclass(frozen=True, slots=True)
class BrowseTarget:
    category: str
    tag: str | None = None

    def __post_init__(self) -> None:
        if self.category not in BROWSE_CATEGORIES:
            raise LookupError("Browse category not found")
        if self.category not in {"artists", "groups"} and self.tag is not None:
            raise ValueError("tag is only supported by artist and group directories")
        if self.tag is not None:
            # Public immutable values validate the exact tag bytes and bounds.
            CatalogTagFilter(
                namespace=self.namespace,
                value=self.tag,
            )

    @property
    def namespace(self) -> str:
        if self.category == "artists":
            return "artist"
        return "group" if self.category == "groups" else "other"

    @property
    def subject(self) -> CatalogTagFilter | None:
        if self.namespace != "other" and self.tag is None:
            return None
        value = (
            self.tag
            if self.tag is not None
            else (
                "multi-work series"
                if self.category == "multi-work-series"
                else self.category
            )
        )
        return CatalogTagFilter(namespace=self.namespace, value=value)

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
