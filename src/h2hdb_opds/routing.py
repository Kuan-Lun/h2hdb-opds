__all__ = ["CanonicalSlashRedirectMiddleware"]

from urllib.parse import quote

from starlette.responses import RedirectResponse
from starlette.routing import Match, Router
from starlette.types import ASGIApp, Receive, Scope, Send


def _application_path(scope: Scope) -> str:
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and (path == root_path or path.startswith(f"{root_path}/")):
        return path[len(root_path) :]
    return path


class CanonicalSlashRedirectMiddleware:
    """Normalize matched route slashes without exposing request URL authority."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        router: Router,
        public_base_url: str,
    ) -> None:
        self._app = app
        self._router = router
        self._public_base_url = public_base_url

    def _matches(self, scope: Scope) -> bool:
        return any(
            route.matches(scope)[0] is not Match.NONE for route in self._router.routes
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or self._matches(scope)
            or _application_path(scope) == "/"
        ):
            await self._app(scope, receive, send)
            return

        redirect_scope = dict(scope)
        path: str = scope["path"]
        redirect_scope["path"] = path.rstrip("/") if path.endswith("/") else f"{path}/"
        if not self._matches(redirect_scope):
            await self._app(scope, receive, send)
            return

        # Only the application-local matched path is appended to the configured
        # public prefix. The inbound scope keeps its actual transport scheme.
        destination = self._public_base_url + quote(
            _application_path(redirect_scope), safe="/"
        )
        query: bytes = scope.get("query_string", b"")
        if query:
            destination = f"{destination}?{query.decode('latin-1')}"
        response = RedirectResponse(
            destination,
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
