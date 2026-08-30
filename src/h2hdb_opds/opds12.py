__all__ = ["create_opds12_router"]

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from .acquisition import serve_artifact
from .atom import OPDS12_ACQUISITION_MEDIA_TYPE, acquisition_feed_document
from .auth import BasicAuthenticator
from .catalog_service import CatalogService
from .config import OPDSConfig

_INT63_MAX = (1 << 63) - 1


def create_opds12_router(
    config: OPDSConfig,
    authenticator: BasicAuthenticator,
    catalog: CatalogService,
) -> APIRouter:
    router = APIRouter(
        prefix="/opds/v1.2",
        dependencies=[Depends(authenticator)],
    )

    def catalog_response(
        request: Request,
        *,
        cursor: str | None,
        limit: int | None,
        revision: int | None,
        offset: str | None,
    ) -> Response:
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
        document = acquisition_feed_document(
            request,
            config,
            selection.page,
            cursor=selection.cursor,
            endpoint="opds12_catalog",
            acquisition_endpoint="opds12_acquire_artifact",
        )
        headers = (
            {"Content-Length": str(len(document))} if request.method == "HEAD" else None
        )
        return Response(
            b"" if request.method == "HEAD" else document,
            media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
            headers=headers,
        )

    @router.api_route(
        "/catalog",
        methods=["GET", "HEAD"],
        name="opds12_catalog",
        response_class=Response,
    )
    def catalog_feed(
        request: Request,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int | None, Query(ge=1, le=128)] = None,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
        offset: Annotated[str | None, Query(include_in_schema=False)] = None,
    ) -> Response:
        return catalog_response(
            request,
            cursor=cursor,
            limit=limit,
            revision=revision,
            offset=offset,
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

    @router.get(
        "/acquisitions/{artifact_id}",
        name="opds12_acquire_artifact",
        response_class=Response,
    )
    def acquire_artifact(
        request: Request,
        artifact_id: str,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return artifact_response(request, artifact_id, revision)

    @router.head(
        "/acquisitions/{artifact_id}",
        name="opds12_head_artifact",
        response_class=Response,
    )
    def head_artifact(
        request: Request,
        artifact_id: str,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return artifact_response(request, artifact_id, revision)

    return router
