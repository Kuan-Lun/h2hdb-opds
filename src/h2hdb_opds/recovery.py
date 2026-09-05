__all__ = ["CatalogRefreshRequired", "recover_catalog_revision"]

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlencode

from fastapi import Request
from h2hdb import CatalogDiscoveryQuery, CatalogFacetKind

from .catalog_service import CatalogService, RevisionUnavailable
from .config import OPDSConfig
from .cursor import decode_discovery_cursor, decode_facet_cursor
from .discovery import discovery_query_parameters
from .urls import external_url


class CatalogRefreshRequired(Exception):
    def __init__(self, location: str) -> None:
        self.location = location
        super().__init__(location)


def _cursor_matches_revision(
    cursor: str | None, revision: int, facet: CatalogFacetKind | None
) -> bool:
    if cursor is None:
        return True
    try:
        if facet is not None:
            decoded_facet = decode_facet_cursor(cursor)
            return decoded_facet.revision == revision and decoded_facet.facet is facet
        return decode_discovery_cursor(cursor).revision == revision
    except ValueError:
        return False


@contextmanager
def recover_catalog_revision(
    request: Request,
    config: OPDSConfig,
    catalog: CatalogService,
    *,
    endpoint: str,
    revision: int | None,
    query: CatalogDiscoveryQuery | None = None,
    search_endpoint: str | None = None,
    search_parameter: str = "q",
    cursor: str | None = None,
    limit: int | None = None,
    facet: CatalogFacetKind | None = None,
) -> Iterator[None]:
    """Restart an explicitly stale navigation request after route validation."""
    try:
        yield
    except RevisionUnavailable as error:
        if (
            revision is None
            or revision <= 0
            or error.revision != revision
            or (limit is not None and limit > config.maximum_page_size)
            or not _cursor_matches_revision(cursor, revision, facet)
        ):
            raise
        current = catalog.revision(None)
        if revision >= current.revision:
            raise

        parameters: dict[str, str | int] = {}
        if query is not None:
            parameters.update(
                discovery_query_parameters(query, search_parameter=search_parameter)
            )
        if search_endpoint is not None and search_parameter in parameters:
            endpoint = search_endpoint
        if limit is not None:
            parameters["limit"] = limit
        path_parameters = {} if facet is None else {"facet": facet.value}
        location = external_url(request, config, endpoint, **path_parameters)
        if parameters:
            location = f"{location}?{urlencode(parameters)}"
        raise CatalogRefreshRequired(location) from error
