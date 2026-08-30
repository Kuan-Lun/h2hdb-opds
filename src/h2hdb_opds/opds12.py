__all__ = ["create_opds12_router"]

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from h2hdb import CatalogRecentOrder

from .acquisition import serve_artifact
from .atom import (
    OPDS12_ACQUISITION_MEDIA_TYPE,
    OPDS12_NAVIGATION_MEDIA_TYPE,
    navigation_feed_document,
    recent_acquisition_feed_document,
)
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

    def reject_pagination(request: Request) -> None:
        if any(
            parameter in request.query_params
            for parameter in ("cursor", "limit", "offset")
        ):
            raise HTTPException(
                status_code=422,
                detail="OPDS 1.2 catalog does not support pagination",
            )

    def atom_response(
        request: Request,
        *,
        document: bytes,
        media_type: str,
    ) -> Response:
        headers = (
            {"Content-Length": str(len(document))} if request.method == "HEAD" else None
        )
        return Response(
            b"" if request.method == "HEAD" else document,
            media_type=media_type,
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
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        reject_pagination(request)
        selected = catalog.revision(revision)
        return atom_response(
            request,
            document=navigation_feed_document(request, config, selected),
            media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
        )

    def recent_feed_response(
        request: Request,
        *,
        order: CatalogRecentOrder,
        revision: int | None,
        endpoint: str,
        title: str,
    ) -> Response:
        reject_pagination(request)
        window = catalog.recent_publications(order=order, revision=revision)
        return atom_response(
            request,
            document=recent_acquisition_feed_document(
                request,
                config,
                window,
                endpoint=endpoint,
                title=title,
            ),
            media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
        )

    @router.api_route(
        "/recent/uploaded",
        methods=["GET", "HEAD"],
        name="opds12_recent_uploaded",
        response_class=Response,
    )
    def recently_uploaded_feed(
        request: Request,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return recent_feed_response(
            request,
            order=CatalogRecentOrder.UPLOADED,
            revision=revision,
            endpoint="opds12_recent_uploaded",
            title="Recently Uploaded",
        )

    @router.api_route(
        "/recent/downloaded",
        methods=["GET", "HEAD"],
        name="opds12_recent_downloaded",
        response_class=Response,
    )
    def recently_downloaded_feed(
        request: Request,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return recent_feed_response(
            request,
            order=CatalogRecentOrder.DOWNLOADED,
            revision=revision,
            endpoint="opds12_recent_downloaded",
            title="Recently Downloaded",
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
