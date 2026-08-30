__all__ = ["create_opds2_router"]

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from .acquisition import serve_artifact
from .auth import (
    AUTHENTICATION_MEDIA_TYPE,
    BasicAuthenticator,
    authentication_document,
)
from .catalog_service import CatalogService
from .config import OPDSConfig
from .serialization import (
    OPDS_FEED_MEDIA_TYPE,
    OPDS_PUBLICATION_MEDIA_TYPE,
    navigation_document,
    publication_document,
    publications_document,
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
            navigation_document(request, config, selected, selected.artifact_count),
            media_type=OPDS_FEED_MEDIA_TYPE,
        )

    @protected.get(
        "/publications",
        name="list_publications",
        response_class=JSONResponse,
    )
    def list_publications(
        request: Request,
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
        selection = catalog.list_publications(
            cursor=cursor,
            limit=limit,
            revision=revision,
        )
        return JSONResponse(
            publications_document(
                request,
                config,
                selection.page,
                cursor=selection.cursor,
                endpoint="list_publications",
            ),
            media_type=OPDS_FEED_MEDIA_TYPE,
        )

    @protected.get(
        "/search",
        name="search_publications",
        response_class=JSONResponse,
    )
    def search_publications(
        query: Annotated[str, Query(min_length=1, max_length=200)],
        limit: Annotated[int | None, Query(ge=1, le=128)] = None,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> JSONResponse:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise HTTPException(status_code=422, detail="query must not be blank")
        del limit, revision
        raise HTTPException(
            status_code=501,
            detail="Catalog search is unavailable until its bounded index is built",
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
