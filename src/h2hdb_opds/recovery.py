__all__ = ["CatalogRefreshRequired", "recover_catalog_revision"]

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlencode

from fastapi import Request
from h2hdb import CatalogDiscoveryQuery, CatalogFacetKind, CatalogReadError

from .browse import BrowseTarget, browse_url
from .catalog_service import CatalogIntegrityError, CatalogService, RevisionUnavailable
from .config import OPDSConfig
from .cursor import decode_discovery_cursor, decode_facet_cursor, decode_tag_cursor
from .discovery import discovery_query_parameters
from .urls import external_url


class CatalogRefreshRequired(Exception):
    def __init__(self, location: str) -> None:
        self.location = location
        super().__init__(location)


def _cursor_matches_revision(
    cursor: str | None,
    revision: int,
    facet: CatalogFacetKind | None,
    browse_target: BrowseTarget | None,
) -> bool:
    if cursor is None:
        return True
    try:
        if browse_target is not None and browse_target.subject is None:
            decoded_tag = decode_tag_cursor(cursor)
            return (
                decoded_tag.revision == revision
                and decoded_tag.namespace == browse_target.namespace
            )
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
    browse_target: BrowseTarget | None = None,
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
            or not _cursor_matches_revision(cursor, revision, facet, browse_target)
        ):
            raise
        try:
            current = catalog.revision(None)
        except CatalogReadError as failure:
            if browse_target is None:
                raise
            raise CatalogIntegrityError(
                "tag browse could not validate the refreshed catalog head"
            ) from failure
        if revision >= current.revision:
            raise

        if browse_target is not None:
            raise CatalogRefreshRequired(
                browse_url(
                    request, config, browse_target, endpoint=endpoint, limit=limit
                )
            ) from error

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
