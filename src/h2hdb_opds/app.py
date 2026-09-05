__all__ = ["create_app"]

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse, Response
from h2hdb import CatalogReader, CatalogSearchQueryTooComplexError, open_database
from starlette.exceptions import HTTPException

from .auth import (
    AuthenticationRequired,
    BasicAuthenticator,
    InsecureAuthenticationTransport,
    authentication_required_response,
    basic_authentication_required_response,
)
from .catalog_service import (
    ArtifactUnavailable,
    CatalogIntegrityError,
    CatalogService,
    CursorBoundaryInvalid,
    CursorInvalid,
    MediaUnavailable,
    PageLimitExceeded,
    PublicationUnavailable,
    RevisionUnavailable,
)
from .config import OPDSConfig
from .library import LibraryIntegrityError, LibraryReadCoordinator, LibraryUnavailable
from .media import create_media_router
from .opds2 import create_opds2_router
from .opds12 import create_opds12_router
from .recovery import CatalogRefreshRequired
from .routing import CanonicalSlashRedirectMiddleware

_LOGGER = logging.getLogger("uvicorn.error")


def create_app(
    config: OPDSConfig,
    catalog: CatalogReader | None = None,
) -> FastAPI:
    package_version = version("h2hdb-opds")
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

    catalog_service = CatalogService(
        reader=current_reader,
        library_reads=library_reads,
        default_page_size=settings.default_page_size,
        maximum_page_size=settings.maximum_page_size,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        nonlocal reader
        _LOGGER.info(
            "h2hdb-opds startup: public_base_url=%s",
            settings.public_base_url,
        )
        library_reads.validate()
        if reader is None:
            reader = open_database(settings.core)
        application.state.catalog_reader = reader
        yield

    application = FastAPI(
        title=settings.title,
        version=package_version,
        lifespan=lifespan,
        redirect_slashes=False,
    )
    authenticator = BasicAuthenticator(settings)

    @application.exception_handler(AuthenticationRequired)
    async def handle_authentication_required(
        request: Request,
        _error: AuthenticationRequired,
    ) -> Response:
        if request.scope["route"].name.startswith("opds12_"):
            return basic_authentication_required_response(settings)
        return authentication_required_response(request, settings)

    @application.exception_handler(InsecureAuthenticationTransport)
    async def handle_insecure_authentication_transport(
        _request: Request,
        _error: InsecureAuthenticationTransport,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Basic authentication requires HTTPS"},
            status_code=426,
            headers={"Upgrade": "TLS/1.2, HTTP/1.1", "Cache-Control": "no-store"},
        )

    @application.exception_handler(LibraryUnavailable)
    async def handle_library_unavailable(
        _request: Request,
        error: LibraryUnavailable,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": str(error), "code": "library_activating"},
            status_code=503,
            headers={"Cache-Control": "no-store", "Retry-After": "1"},
        )

    @application.exception_handler(LibraryIntegrityError)
    async def handle_library_integrity_error(
        _request: Request,
        error: LibraryIntegrityError,
    ) -> JSONResponse:
        _LOGGER.error("library_integrity_error: %s", error)
        return JSONResponse(
            {"detail": str(error), "code": "library_integrity_error"},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(CatalogIntegrityError)
    async def handle_catalog_integrity_error(
        _request: Request,
        error: CatalogIntegrityError,
    ) -> JSONResponse:
        _LOGGER.error("catalog_integrity_error: %s", error)
        return JSONResponse(
            {"detail": str(error), "code": "catalog_integrity_error"},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, error: HTTPException) -> Response:
        response = await http_exception_handler(request, error)
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(RevisionUnavailable)
    async def handle_revision_unavailable(
        _request: Request,
        error: RevisionUnavailable,
    ) -> JSONResponse:
        description = "current" if error.revision is None else str(error.revision)
        return JSONResponse(
            {"detail": f"Catalog revision {description} not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(CatalogRefreshRequired)
    async def handle_catalog_refresh(
        _request: Request,
        error: CatalogRefreshRequired,
    ) -> RedirectResponse:
        return RedirectResponse(
            error.location,
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(CursorInvalid)
    async def handle_cursor_invalid(
        _request: Request,
        _error: CursorInvalid,
    ) -> JSONResponse:
        return JSONResponse({"detail": "cursor is invalid"}, status_code=422)

    @application.exception_handler(CursorBoundaryInvalid)
    async def handle_cursor_boundary_invalid(
        _request: Request,
        _error: CursorBoundaryInvalid,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "cursor does not identify a valid page boundary"},
            status_code=422,
        )

    @application.exception_handler(PageLimitExceeded)
    async def handle_page_limit_exceeded(
        _request: Request,
        error: PageLimitExceeded,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": f"limit must not exceed {error.maximum}"},
            status_code=422,
        )

    @application.exception_handler(CatalogSearchQueryTooComplexError)
    async def handle_search_query_too_complex(
        _request: Request,
        error: CatalogSearchQueryTooComplexError,
    ) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=422)

    @application.exception_handler(PublicationUnavailable)
    async def handle_publication_unavailable(
        _request: Request,
        _error: PublicationUnavailable,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Publication not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(ArtifactUnavailable)
    async def handle_artifact_unavailable(
        _request: Request,
        _error: ArtifactUnavailable,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Artifact not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(MediaUnavailable)
    async def handle_media_unavailable(
        _request: Request,
        _error: MediaUnavailable,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Publication media not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/health", name="health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/version", name="version")
    def service_version(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"service": "h2hdb-opds", "version": package_version}

    application.include_router(
        create_opds2_router(settings, authenticator, catalog_service)
    )
    application.include_router(
        create_opds12_router(settings, authenticator, catalog_service)
    )
    application.include_router(
        create_media_router(settings, authenticator, catalog_service)
    )
    application.add_middleware(
        CanonicalSlashRedirectMiddleware,
        router=application.router,
        public_base_url=settings.public_base_url,
    )
    return application
