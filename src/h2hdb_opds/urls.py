__all__ = ["external_url"]

from urllib.parse import quote

from fastapi import Request

from .config import OPDSConfig


def external_url(
    request: Request,
    config: OPDSConfig,
    route_name: str,
    **path_parameters: object,
) -> str:
    """Build a public URL without trusting the request's Host header."""
    encoded_parameters = {
        name: quote(str(value), safe="") for name, value in path_parameters.items()
    }
    route_url = request.url_for(route_name, **encoded_parameters)
    return f"{config.public_base_url}{route_url.path}"
