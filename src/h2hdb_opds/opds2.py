__all__ = ["create_opds2_router"]

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from h2hdb import CatalogDiscoveryQuery, CatalogFacetKind, CatalogRecentOrder

from .acquisition import serve_artifact
from .auth import (
    AUTHENTICATION_MEDIA_TYPE,
    BasicAuthenticator,
    authentication_document,
)
from .catalog_service import CatalogService
from .config import OPDSConfig
from .discovery import discovery_query
from .serialization import (
    OPDS_FEED_MEDIA_TYPE,
    OPDS_PUBLICATION_MEDIA_TYPE,
    discovery_document,
    facet_navigation_document,
    navigation_document,
    publication_document,
    recent_document,
)

_INT63_MAX = (1 << 63) - 1


def create_opds2_router(
    config: OPDSConfig,
    authenticator: BasicAuthenticator,
    catalog: CatalogService,
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/opds/v2/authentication",
        name="authentication_document",
        response_class=JSONResponse,
    )
    def get_authentication_document(request: Request) -> JSONResponse:
        return JSONResponse(
            authentication_document(request, config),
            media_type=AUTHENTICATION_MEDIA_TYPE,
        )

    protected = APIRouter(
        prefix="/opds/v2",
        dependencies=[Depends(authenticator)],
    )

    @protected.get("", name="navigation", response_class=JSONResponse)
    def navigation(
        request: Request,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> JSONResponse:
        selected = catalog.revision(revision)
        return JSONResponse(
            navigation_document(request, config, selected),
            media_type=OPDS_FEED_MEDIA_TYPE,
        )

    def discovery_response(
        request: Request,
        *,
        query: CatalogDiscoveryQuery,
        cursor: str | None,
        limit: int | None,
        revision: int | None,
        endpoint: str,
        title: str,
    ) -> JSONResponse:
        selection = catalog.discovery_feed(
            query=query,
            cursor=cursor,
            limit=limit,
            revision=revision,
        )
        return JSONResponse(
            discovery_document(
                request,
                config,
                selection.page,
                cursor=selection.cursor,
                query=query,
                facet_pages=selection.facets,
                endpoint=endpoint,
                title=title,
            ),
            media_type=OPDS_FEED_MEDIA_TYPE,
        )

    @protected.get(
        "/publications",
        name="list_publications",
        response_class=JSONResponse,
    )
    def list_publications(
        request: Request,
        language: Annotated[str | None, Query(max_length=1024)] = None,
        tag: Annotated[str | None, Query(max_length=1024)] = None,
        tag_namespace: Annotated[str | None, Query(max_length=1024)] = None,
        contributor: Annotated[str | None, Query(max_length=1024)] = None,
        role: Annotated[str | None, Query(max_length=1024)] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int | None, Query(ge=1, le=128)] = None,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
        offset: Annotated[str | None, Query(include_in_schema=False)] = None,
    ) -> JSONResponse:
        if offset is not None:
            raise HTTPException(
                status_code=422,
                detail="offset pagination was removed; follow the cursor links",
            )
        try:
            query = discovery_query(
                search=None,
                language=language,
                tag=tag,
                tag_namespace=tag_namespace,
                contributor=contributor,
                role=role,
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return discovery_response(
            request,
            query=query,
            cursor=cursor,
            limit=limit,
            revision=revision,
            endpoint="list_publications",
            title="All Publications",
        )

    def selected_query(
        *,
        search: str | None,
        require_search: bool = False,
        language: str | None,
        tag: str | None,
        tag_namespace: str | None,
        contributor: str | None,
        role: str | None,
    ) -> CatalogDiscoveryQuery:
        try:
            return discovery_query(
                search=search,
                required_search_field="query" if require_search else None,
                language=language,
                tag=tag,
                tag_namespace=tag_namespace,
                contributor=contributor,
                role=role,
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @protected.get(
        "/search",
        name="search_publications",
        response_class=JSONResponse,
    )
    def search_publications(
        request: Request,
        query: Annotated[str | None, Query(max_length=1024)] = None,
        language: Annotated[str | None, Query(max_length=1024)] = None,
        tag: Annotated[str | None, Query(max_length=1024)] = None,
        tag_namespace: Annotated[str | None, Query(max_length=1024)] = None,
        contributor: Annotated[str | None, Query(max_length=1024)] = None,
        role: Annotated[str | None, Query(max_length=1024)] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int | None, Query(ge=1, le=128)] = None,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
        offset: Annotated[str | None, Query(include_in_schema=False)] = None,
        q: Annotated[str | None, Query(include_in_schema=False)] = None,
    ) -> JSONResponse:
        if q is not None:
            raise HTTPException(
                status_code=422,
                detail="q was removed from OPDS 2 search; use query",
            )
        if offset is not None:
            raise HTTPException(
                status_code=422,
                detail="offset pagination was removed; follow the cursor links",
            )
        selected = selected_query(
            search=query,
            require_search=True,
            language=language,
            tag=tag,
            tag_namespace=tag_namespace,
            contributor=contributor,
            role=role,
        )
        return discovery_response(
            request,
            query=selected,
            cursor=cursor,
            limit=limit,
            revision=revision,
            endpoint="search_publications",
            title=(
                "Search Results"
                if selected.search is not None
                else "Browse Publications"
            ),
        )

    @protected.get(
        "/facets/{facet}",
        name="facet_values",
        response_class=JSONResponse,
    )
    def facet_values(
        request: Request,
        facet: str,
        query: Annotated[str | None, Query(max_length=1024)] = None,
        language: Annotated[str | None, Query(max_length=1024)] = None,
        tag: Annotated[str | None, Query(max_length=1024)] = None,
        tag_namespace: Annotated[str | None, Query(max_length=1024)] = None,
        contributor: Annotated[str | None, Query(max_length=1024)] = None,
        role: Annotated[str | None, Query(max_length=1024)] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int | None, Query(ge=1, le=128)] = None,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
        q: Annotated[str | None, Query(include_in_schema=False)] = None,
    ) -> JSONResponse:
        if q is not None:
            raise HTTPException(
                status_code=422,
                detail="q was removed from OPDS 2 search; use query",
            )
        try:
            facet_kind = CatalogFacetKind(facet)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Facet not found") from error
        selected = selected_query(
            search=query,
            language=language,
            tag=tag,
            tag_namespace=tag_namespace,
            contributor=contributor,
            role=role,
        )
        selection = catalog.facet_page(
            facet=facet_kind,
            query=selected,
            cursor=cursor,
            limit=limit,
            revision=revision,
        )
        return JSONResponse(
            facet_navigation_document(
                request,
                config,
                selection.page,
                cursor=selection.cursor,
                query=selected,
            ),
            media_type=OPDS_FEED_MEDIA_TYPE,
        )

    def recent_response(
        request: Request,
        *,
        order: CatalogRecentOrder,
        revision: int | None,
        endpoint: str,
        title: str,
    ) -> JSONResponse:
        if any(
            parameter in request.query_params
            for parameter in ("cursor", "limit", "offset")
        ):
            raise HTTPException(
                status_code=422,
                detail="recent feeds are fixed complete windows and are not paginated",
            )
        window = catalog.recent_publications(order=order, revision=revision)
        return JSONResponse(
            recent_document(
                request,
                config,
                window,
                endpoint=endpoint,
                title=title,
            ),
            media_type=OPDS_FEED_MEDIA_TYPE,
        )

    @protected.get(
        "/recent/uploaded",
        name="recently_uploaded",
        response_class=JSONResponse,
    )
    def recently_uploaded(
        request: Request,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> JSONResponse:
        return recent_response(
            request,
            order=CatalogRecentOrder.UPLOADED,
            revision=revision,
            endpoint="recently_uploaded",
            title="Recently Uploaded",
        )

    @protected.get(
        "/recent/downloaded",
        name="recently_downloaded",
        response_class=JSONResponse,
    )
    def recently_downloaded(
        request: Request,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> JSONResponse:
        return recent_response(
            request,
            order=CatalogRecentOrder.DOWNLOADED,
            revision=revision,
            endpoint="recently_downloaded",
            title="Recently Downloaded",
        )

    @protected.get(
        "/publications/{publication_id}",
        name="get_publication",
        response_class=JSONResponse,
    )
    def get_publication(
        request: Request,
        publication_id: str,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> JSONResponse:
        selection = catalog.publication(publication_id, revision=revision)
        return JSONResponse(
            publication_document(
                request,
                config,
                selection.publication,
                selection.revision.revision,
            ),
            media_type=OPDS_PUBLICATION_MEDIA_TYPE,
        )

    def artifact_response(
        request: Request,
        artifact_id: str,
        revision: int | None,
    ) -> Response:
        with catalog.artifact_read(artifact_id, revision=revision) as selected:
            return serve_artifact(
                request,
                selected.artifact,
                library_root=config.library_root,
                revalidate_head=selected.revalidate_head,
            )

    @protected.get(
        "/acquisitions/{artifact_id}",
        name="acquire_artifact",
        response_class=Response,
    )
    def acquire_artifact(
        request: Request,
        artifact_id: str,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return artifact_response(request, artifact_id, revision)

    @protected.head(
        "/acquisitions/{artifact_id}",
        name="head_artifact",
        response_class=Response,
    )
    def head_artifact(
        request: Request,
        artifact_id: str,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return artifact_response(request, artifact_id, revision)

    router.include_router(protected)
    return router
