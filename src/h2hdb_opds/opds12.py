__all__ = ["create_opds12_router"]

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from h2hdb import CatalogDiscoveryQuery, CatalogFacetKind, CatalogRecentOrder

from .acquisition import serve_artifact
from .atom import (
    OPDS12_ACQUISITION_MEDIA_TYPE,
    OPDS12_ENTRY_MEDIA_TYPE,
    OPDS12_NAVIGATION_MEDIA_TYPE,
    OPEN_SEARCH_MEDIA_TYPE,
    acquisition_feed_document,
    facet_navigation_feed_document,
    navigation_feed_document,
    opensearch_description_document,
    publication_entry_document,
    recent_acquisition_feed_document,
)
from .auth import BasicAuthenticator
from .catalog_service import CatalogService
from .config import OPDSConfig
from .discovery import discovery_query
from .search import SEARCH_QUERY_MAXIMUM_BYTES

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

    def reject_parameters(request: Request, names: tuple[str, ...]) -> None:
        invalid = next(
            (name for name in names if name in request.query_params),
            None,
        )
        if invalid is not None:
            raise HTTPException(
                status_code=422,
                detail=f"{invalid} is not supported by this catalog resource",
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
        reject_parameters(request, ("cursor", "limit", "offset"))
        selected = catalog.revision(revision)
        return atom_response(
            request,
            document=navigation_feed_document(request, config, selected),
            media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
        )

    @router.api_route(
        "/publications",
        methods=["GET", "HEAD"],
        name="opds12_publications",
        response_class=Response,
    )
    def publications_feed(
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
    ) -> Response:
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
        selection = catalog.discovery_feed(
            query=query,
            cursor=cursor,
            limit=limit,
            revision=revision,
        )
        return atom_response(
            request,
            document=acquisition_feed_document(
                request,
                config,
                selection.page,
                cursor=selection.cursor,
                query=query,
                facet_pages=selection.facets,
            ),
            media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
        )

    def selected_query(
        *,
        q: str | None,
        require_search: bool = False,
        language: str | None,
        tag: str | None,
        tag_namespace: str | None,
        contributor: str | None,
        role: str | None,
    ) -> CatalogDiscoveryQuery:
        try:
            return discovery_query(
                search=q,
                required_search_field="q" if require_search else None,
                language=language,
                tag=tag,
                tag_namespace=tag_namespace,
                contributor=contributor,
                role=role,
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.api_route(
        "/search",
        methods=["GET", "HEAD"],
        name="opds12_search",
        response_class=Response,
    )
    def search_feed(
        request: Request,
        q: Annotated[str | None, Query(max_length=SEARCH_QUERY_MAXIMUM_BYTES)] = None,
        language: Annotated[str | None, Query(max_length=1024)] = None,
        tag: Annotated[str | None, Query(max_length=1024)] = None,
        tag_namespace: Annotated[str | None, Query(max_length=1024)] = None,
        contributor: Annotated[str | None, Query(max_length=1024)] = None,
        role: Annotated[str | None, Query(max_length=1024)] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int | None, Query(ge=1, le=128)] = None,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
        offset: Annotated[str | None, Query(include_in_schema=False)] = None,
    ) -> Response:
        if offset is not None:
            raise HTTPException(
                status_code=422,
                detail="offset pagination was removed; follow the cursor links",
            )
        query = selected_query(
            q=q,
            require_search=True,
            language=language,
            tag=tag,
            tag_namespace=tag_namespace,
            contributor=contributor,
            role=role,
        )
        selection = catalog.discovery_feed(
            query=query,
            cursor=cursor,
            limit=limit,
            revision=revision,
        )
        return atom_response(
            request,
            document=acquisition_feed_document(
                request,
                config,
                selection.page,
                cursor=selection.cursor,
                query=query,
                facet_pages=selection.facets,
            ),
            media_type=OPDS12_ACQUISITION_MEDIA_TYPE,
        )

    @router.api_route(
        "/facets/{facet}",
        methods=["GET", "HEAD"],
        name="opds12_facet_values",
        response_class=Response,
    )
    def facet_values(
        request: Request,
        facet: str,
        q: Annotated[str | None, Query(max_length=SEARCH_QUERY_MAXIMUM_BYTES)] = None,
        language: Annotated[str | None, Query(max_length=1024)] = None,
        tag: Annotated[str | None, Query(max_length=1024)] = None,
        tag_namespace: Annotated[str | None, Query(max_length=1024)] = None,
        contributor: Annotated[str | None, Query(max_length=1024)] = None,
        role: Annotated[str | None, Query(max_length=1024)] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int | None, Query(ge=1, le=128)] = None,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        try:
            facet_kind = CatalogFacetKind(facet)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Facet not found") from error
        query = selected_query(
            q=q,
            language=language,
            tag=tag,
            tag_namespace=tag_namespace,
            contributor=contributor,
            role=role,
        )
        selection = catalog.facet_page(
            facet=facet_kind,
            query=query,
            cursor=cursor,
            limit=limit,
            revision=revision,
        )
        return atom_response(
            request,
            document=facet_navigation_feed_document(
                request,
                config,
                selection.page,
                cursor=selection.cursor,
                query=query,
            ),
            media_type=OPDS12_NAVIGATION_MEDIA_TYPE,
        )

    @router.api_route(
        "/opensearch.xml",
        methods=["GET", "HEAD"],
        name="opds12_opensearch",
        response_class=Response,
    )
    def opensearch_description(
        request: Request,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        reject_parameters(request, ("cursor", "limit", "offset"))
        selected = catalog.revision(revision)
        return atom_response(
            request,
            document=opensearch_description_document(
                request,
                config,
                selected,
            ),
            media_type=OPEN_SEARCH_MEDIA_TYPE,
        )

    def recent_feed_response(
        request: Request,
        *,
        order: CatalogRecentOrder,
        revision: int | None,
        endpoint: str,
        title: str,
    ) -> Response:
        reject_parameters(request, ("cursor", "limit", "offset"))
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

    @router.api_route(
        "/publications/{publication_id}",
        methods=["GET", "HEAD"],
        name="opds12_publication",
        response_class=Response,
    )
    def publication_entry(
        request: Request,
        publication_id: str,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> Response:
        selection = catalog.publication(publication_id, revision=revision)
        return atom_response(
            request,
            document=publication_entry_document(
                request,
                config,
                selection.publication,
                selection.revision.revision,
            ),
            media_type=OPDS12_ENTRY_MEDIA_TYPE,
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
