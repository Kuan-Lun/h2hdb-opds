"""OPDS 2.0 HTTP service for H2HDB catalogs."""

from .app import CompatibleCatalogReader, create_app
from .config import (
    BasicAuthConfig,
    OPDSConfig,
    OPDSConfigError,
    ServerConfig,
    load_config,
)

__all__ = [
    "BasicAuthConfig",
    "CompatibleCatalogReader",
    "OPDSConfig",
    "OPDSConfigError",
    "ServerConfig",
    "create_app",
    "load_config",
]
