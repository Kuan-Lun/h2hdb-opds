__all__ = ["create_media_router"]

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response

from .acquisition import serve_image_resource
from .auth import BasicAuthenticator
from .catalog_service import CatalogService
from .config import OPDSConfig

_INT63_MAX = (1 << 63) - 1


def create_media_router(
    config: OPDSConfig,
    authenticator: BasicAuthenticator,
    catalog: CatalogService,
) -> APIRouter:
    router = APIRouter(
        prefix="/media/publications",
        dependencies=[Depends(authenticator)],
    )

    def page_response(
        request: Request,
        publication_id: str,
        page_number: int,
        revision: int | None,
    ) -> Response:
        with catalog.page_read(
            publication_id,
            page_number,
            revision=revision,
        ) as selected:
            return serve_image_resource(
                request,
                selected.resource,
                library_root=config.library_root,
                revalidate_head=selected.revalidate_head,
            )

    @router.get(
        "/{publication_id}/pages/{page_number}",
        name="publication_page",
        response_class=Response,
    )
    def get_page(
        request: Request,
        publication_id: str,
        page_number: Annotated[int, Path(ge=0, le=4095)],
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return page_response(request, publication_id, page_number, revision)

    @router.head(
        "/{publication_id}/pages/{page_number}",
        name="head_publication_page",
        response_class=Response,
    )
    def head_page(
        request: Request,
        publication_id: str,
        page_number: Annotated[int, Path(ge=0, le=4095)],
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return page_response(request, publication_id, page_number, revision)

    def thumbnail_response(
        request: Request,
        publication_id: str,
        revision: int | None,
    ) -> Response:
        with catalog.thumbnail_read(publication_id, revision=revision) as selected:
            return serve_image_resource(
                request,
                selected.resource,
                library_root=config.library_root,
                revalidate_head=selected.revalidate_head,
            )

    @router.get(
        "/{publication_id}/thumbnail",
        name="publication_thumbnail",
        response_class=Response,
    )
    def get_thumbnail(
        request: Request,
        publication_id: str,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return thumbnail_response(request, publication_id, revision)

    @router.head(
        "/{publication_id}/thumbnail",
        name="head_publication_thumbnail",
        response_class=Response,
    )
    def head_thumbnail(
        request: Request,
        publication_id: str,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        return thumbnail_response(request, publication_id, revision)

    return router
