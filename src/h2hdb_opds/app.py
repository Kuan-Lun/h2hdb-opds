__all__ = ["create_app"]

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from h2hdb import (
    CatalogCursorError,
    CatalogReader,
    CatalogRevision,
    CatalogRevisionNotFoundError,
    open_database,
)

from .acquisition import serve_artifact
from .auth import (
    AUTHENTICATION_MEDIA_TYPE,
    AuthenticationRequired,
    BasicAuthenticator,
    InsecureAuthenticationTransport,
    authentication_document,
    authentication_required_response,
)
from .config import OPDSConfig
from .cursor import decode_artifact_cursor
from .library import LibraryReadCoordinator, LibraryUnavailable
from .serialization import (
    OPDS_FEED_MEDIA_TYPE,
    OPDS_PUBLICATION_MEDIA_TYPE,
    navigation_document,
    publication_document,
    publications_document,
)

_INT63_MAX = (1 << 63) - 1


def create_app(
    config: OPDSConfig,
    catalog: CatalogReader | None = None,
) -> FastAPI:
    settings = config
    reader = catalog
    library_reads = LibraryReadCoordinator(
        library_root=settings.library_root,
        coordination_root=settings.coordination_root,
    )

    def current_reader() -> CatalogReader:
        if reader is None:
            raise RuntimeError(
                "Catalog reader is unavailable before application startup"
            )
        return reader

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        nonlocal reader
        library_reads.validate()
        if reader is None:
            reader = open_database(settings.core)
        application.state.catalog_reader = reader
        yield

    application = FastAPI(
        title=settings.title,
        lifespan=lifespan,
    )
    authenticator = BasicAuthenticator(settings)

    @application.exception_handler(AuthenticationRequired)
    async def handle_authentication_required(
        request: Request,
        _error: AuthenticationRequired,
    ) -> JSONResponse:
        return authentication_required_response(request, settings)

    @application.exception_handler(InsecureAuthenticationTransport)
    async def handle_insecure_authentication_transport(
        _request: Request,
        _error: InsecureAuthenticationTransport,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Basic authentication requires HTTPS"},
            status_code=426,
            headers={"Upgrade": "TLS/1.2, HTTP/1.1"},
        )

    @application.exception_handler(LibraryUnavailable)
    async def handle_library_unavailable(
        _request: Request,
        error: LibraryUnavailable,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": str(error)},
            status_code=503,
            headers={"Cache-Control": "no-store", "Retry-After": "1"},
        )

    @application.get("/health", name="health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get(
        "/opds/v2/authentication",
        name="authentication_document",
        response_class=JSONResponse,
    )
    def get_authentication_document(request: Request) -> JSONResponse:
        return JSONResponse(
            authentication_document(request, settings),
            media_type=AUTHENTICATION_MEDIA_TYPE,
        )

    protected = APIRouter(
        prefix="/opds/v2",
        dependencies=[Depends(authenticator)],
    )

    def revision_not_found(revision: int | None) -> HTTPException:
        description = "current" if revision is None else str(revision)
        return HTTPException(
            status_code=404,
            detail=f"Catalog revision {description} not found",
        )

    def resolved_revision(requested: int | None) -> CatalogRevision:
        try:
            current = current_reader().get_catalog_revision()
        except CatalogRevisionNotFoundError as error:
            raise revision_not_found(requested) from error
        if requested is not None and requested != current.revision:
            raise revision_not_found(requested)
        return current

    @contextmanager
    def pinned_revision_read(selected: CatalogRevision) -> Iterator[None]:
        try:
            yield
        except CatalogRevisionNotFoundError as error:
            # The head may advance after it was resolved but before the pinned
            # read starts.  Preserve the selected revision and fail closed;
            # retrying against the new current head would mix responses.
            raise revision_not_found(selected.revision) from error

    @protected.get("", name="navigation", response_class=JSONResponse)
    def navigation(
        request: Request,
        revision: Annotated[int | None, Query(ge=1, le=_INT63_MAX)] = None,
    ) -> JSONResponse:
        with library_reads.read():
            selected = resolved_revision(revision)
        return JSONResponse(
            navigation_document(request, settings, selected, selected.artifact_count),
            media_type=OPDS_FEED_MEDIA_TYPE,
        )

    def resolved_limit(limit: int | None) -> int:
        result = settings.default_page_size if limit is None else limit
        if result > settings.maximum_page_size:
            raise HTTPException(
                status_code=422,
                detail=f"limit must not exceed {settings.maximum_page_size}",
            )
        return result

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
        with library_reads.read():
            selected = resolved_revision(revision)
            selected_limit = resolved_limit(limit)
            try:
                decoded_cursor = (
                    None if cursor is None else decode_artifact_cursor(cursor)
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail="cursor is invalid",
                ) from error
            if (
                decoded_cursor is not None
                and decoded_cursor.revision != selected.revision
            ):
                raise revision_not_found(decoded_cursor.revision)
            try:
                with pinned_revision_read(selected):
                    page = current_reader().list_artifact_publications(
                        after=decoded_cursor,
                        limit=selected_limit,
                        revision=selected,
                    )
            except CatalogCursorError as error:
                raise HTTPException(
                    status_code=422,
                    detail="cursor does not identify a valid page boundary",
                ) from error
        return JSONResponse(
            publications_document(
                request,
                settings,
                page,
                cursor=decoded_cursor,
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
        with library_reads.read():
            selected = resolved_revision(revision)
            with pinned_revision_read(selected):
                publication = current_reader().get_publication(
                    publication_id,
                    revision=selected,
                )
        if publication is None or not publication.artifacts:
            raise HTTPException(status_code=404, detail="Publication not found")
        return JSONResponse(
            publication_document(request, settings, publication, selected.revision),
            media_type=OPDS_PUBLICATION_MEDIA_TYPE,
        )

    def artifact_response(
        request: Request,
        artifact_id: str,
        revision: int | None,
    ) -> Response:
        with library_reads.read():
            selected = resolved_revision(revision)
            with pinned_revision_read(selected):
                artifact = current_reader().get_artifact(
                    artifact_id,
                    revision=selected,
                )
                if artifact is None:
                    raise HTTPException(status_code=404, detail="Artifact not found")

                def revalidate_head() -> None:
                    confirmed = current_reader().get_catalog_revision()
                    if confirmed.revision != selected.revision:
                        raise revision_not_found(selected.revision)

                return serve_artifact(
                    request,
                    artifact,
                    library_root=settings.library_root,
                    revalidate_head=revalidate_head,
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

    application.include_router(protected)
    return application
