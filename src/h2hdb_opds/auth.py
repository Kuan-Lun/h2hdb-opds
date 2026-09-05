__all__ = [
    "AUTHENTICATION_DOCUMENT_REL",
    "AUTHENTICATION_MEDIA_TYPE",
    "AuthenticationRequired",
    "BasicAuthenticator",
    "InsecureAuthenticationTransport",
    "authentication_document",
    "authentication_required_response",
    "basic_authentication_required_response",
]

import base64
import binascii
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .config import BasicAuthConfig, OPDSConfig
from .urls import external_url

AUTHENTICATION_MEDIA_TYPE = "application/opds-authentication+json"
AUTHENTICATION_DOCUMENT_REL = "http://opds-spec.org/auth/document"
BASIC_AUTH_TYPE = "http://opds-spec.org/auth/basic"


class AuthenticationRequired(Exception):
    pass


class InsecureAuthenticationTransport(Exception):
    pass


def authentication_document(request: Request, config: OPDSConfig) -> dict[str, object]:
    self_url = external_url(request, config, "authentication_document")
    authentication: list[dict[str, object]] = []
    if config.auth.enabled:
        authentication.append(
            {
                "type": BASIC_AUTH_TYPE,
                "labels": {
                    "login": "Username",
                    "password": "Password",
                },
            }
        )
    return {
        "id": self_url,
        "title": f"{config.title} Authentication",
        "description": "Authentication options for this OPDS catalog.",
        "links": [
            {
                "rel": "self",
                "href": self_url,
                "type": AUTHENTICATION_MEDIA_TYPE,
            }
        ],
        "authentication": authentication,
    }


def _challenge(auth: BasicAuthConfig) -> str:
    realm = auth.realm.replace("\\", "\\\\").replace('"', '\\"')
    return f'Basic realm="{realm}", charset="UTF-8"'


def authentication_required_response(
    request: Request,
    config: OPDSConfig,
) -> JSONResponse:
    authentication_url = external_url(request, config, "authentication_document")
    return JSONResponse(
        authentication_document(request, config),
        status_code=401,
        media_type=AUTHENTICATION_MEDIA_TYPE,
        headers={
            "WWW-Authenticate": _challenge(config.auth),
            "Cache-Control": "no-store",
            "Link": (
                f'<{authentication_url}>; rel="{AUTHENTICATION_DOCUMENT_REL}"; '
                f'type="{AUTHENTICATION_MEDIA_TYPE}"'
            ),
        },
    )


def basic_authentication_required_response(config: OPDSConfig) -> Response:
    """Return the protocol-neutral HTTP Basic challenge used by OPDS 1.2."""
    return Response(
        status_code=401,
        headers={
            "WWW-Authenticate": _challenge(config.auth),
            "Cache-Control": "no-store",
        },
    )


class BasicAuthenticator:
    def __init__(self, config: OPDSConfig) -> None:
        self._config = config

    def __call__(self, request: Request) -> None:
        auth = self._config.auth
        if not auth.enabled:
            return
        if request.url.scheme.casefold() != "https":
            raise InsecureAuthenticationTransport
        credentials = self._decode_credentials(request.headers.get("Authorization"))
        if credentials is None:
            raise AuthenticationRequired
        username, password = credentials
        expected_username = auth.username
        expected_password = auth.password
        if expected_username is None or expected_password is None:
            raise AuthenticationRequired
        username_matches = secrets.compare_digest(
            username.encode("utf-8"),
            expected_username.encode("utf-8"),
        )
        password_matches = secrets.compare_digest(
            password.encode("utf-8"),
            expected_password.get_secret_value().encode("utf-8"),
        )
        if not (username_matches and password_matches):
            raise AuthenticationRequired

    @staticmethod
    def _decode_credentials(header: str | None) -> tuple[str, str] | None:
        if header is None:
            return None
        scheme, separator, encoded = header.partition(" ")
        if not separator or scheme.casefold() != "basic" or not encoded:
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except binascii.Error, UnicodeDecodeError:
            return None
        username, separator, password = decoded.partition(":")
        if not separator:
            return None
        return username, password
