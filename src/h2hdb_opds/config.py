__all__ = [
    "BasicAuthConfig",
    "OPDSConfig",
    "OPDSConfigError",
    "ServerConfig",
    "load_config",
]

import json
import os
import re
from ipaddress import ip_network
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit, urlunsplit

from h2hdb import (
    CoreConfig,
    DatabaseAccessMode,
    resolve_environment_placeholders,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_INVALID_URI_CHARACTER = re.compile(r'[\x00-\x20\x7f<>"{}|\\^`]')
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class OPDSConfigError(ValueError):
    pass


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class BasicAuthConfig(ConfigModel):
    username: str | None = Field(default=None, min_length=1)
    password: SecretStr | None = None
    realm: str = Field(default="H2HDB OPDS", min_length=1)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if ":" in value:
            raise ValueError("username must not contain ':' for Basic authentication")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("username must not contain control characters")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("password must not be empty")
        return value

    @field_validator("realm")
    @classmethod
    def validate_realm(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("realm must not contain control characters")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as error:
            raise ValueError(
                "realm must contain only characters representable in HTTP headers"
            ) from error
        return value

    def model_post_init(self, __context: object) -> None:
        if (self.username is None) is not (self.password is None):
            raise ValueError("username and password must be configured together")

    @property
    def enabled(self) -> bool:
        return self.username is not None


class ServerConfig(ConfigModel):
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    tls_certificate: Path | None = None
    tls_private_key: Path | None = None
    trusted_proxy_ips: tuple[str, ...] = ()

    @field_validator("trusted_proxy_ips")
    @classmethod
    def validate_trusted_proxy_ips(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            if value == "*":
                raise ValueError("trusted_proxy_ips must not trust every address")
            try:
                network = ip_network(value, strict=False)
            except ValueError as error:
                raise ValueError(
                    f"invalid trusted proxy address or network: {value}"
                ) from error
            if network.prefixlen == 0:
                raise ValueError("trusted_proxy_ips must not trust every address")
            normalized.append(str(network))
        return tuple(normalized)

    def model_post_init(self, __context: object) -> None:
        if (self.tls_certificate is None) is not (self.tls_private_key is None):
            raise ValueError(
                "tls_certificate and tls_private_key must be configured together"
            )

    @property
    def serves_tls(self) -> bool:
        return self.tls_certificate is not None


def _read_only_core_config(config: CoreConfig | None = None) -> CoreConfig:
    core = config or CoreConfig()
    database = core.database.model_copy(
        update={"access_mode": DatabaseAccessMode.read_only}
    )
    return core.model_copy(update={"database": database})


class OPDSConfig(ConfigModel):
    library_root: Path
    coordination_root: Path
    public_base_url: str
    core: CoreConfig = Field(default_factory=_read_only_core_config)
    auth: BasicAuthConfig = Field(default_factory=BasicAuthConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    title: str = Field(default="H2HDB Catalog", min_length=1)
    default_page_size: int = Field(default=50, ge=1, le=128)
    maximum_page_size: int = Field(default=128, ge=1, le=128)

    @field_validator("library_root", "coordination_root")
    @classmethod
    def validate_filesystem_root(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("filesystem roots must be absolute paths")
        if expanded == Path(expanded.anchor):
            raise ValueError("filesystem roots must not be the filesystem root")
        return expanded

    def model_post_init(self, __context: object) -> None:
        library_root = Path(os.path.abspath(self.library_root))
        coordination_root = Path(os.path.abspath(self.coordination_root))
        if library_root == coordination_root:
            raise ValueError("library_root and coordination_root must be distinct")
        if (
            library_root in coordination_root.parents
            or coordination_root in library_root.parents
        ):
            raise ValueError("library_root and coordination_root must not overlap")
        if self.default_page_size > self.maximum_page_size:
            raise ValueError("default_page_size must not exceed maximum_page_size")
        if self.auth.enabled:
            if urlsplit(self.public_base_url).scheme != "https":
                raise ValueError(
                    "Basic authentication requires an HTTPS public_base_url"
                )
            if not self.server.serves_tls and not self.server.trusted_proxy_ips:
                raise ValueError(
                    "Basic authentication requires local TLS or an explicitly trusted "
                    "TLS-terminating proxy"
                )

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        if _INVALID_URI_CHARACTER.search(value) or _INVALID_PERCENT_ESCAPE.search(
            value
        ):
            raise ValueError("public_base_url must be a valid ASCII URI")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("public_base_url must be a valid ASCII URI") from error
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("public_base_url must be an absolute HTTP(S) URL")
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("public_base_url contains an invalid port") from error
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("public_base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("public_base_url must not contain a query or fragment")
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.casefold(), parsed.netloc, path, "", ""))

    @field_validator("core")
    @classmethod
    def force_read_only(cls, value: CoreConfig) -> CoreConfig:
        return _read_only_core_config(value)

    @classmethod
    def from_file(cls, config_path: str | Path) -> Self:
        path = Path(config_path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OPDSConfigError(
                f"Unable to load OPDS config from {path}: {error}"
            ) from error
        return cls.model_validate(resolve_environment_placeholders(raw))


def load_config(config_path: str | Path) -> OPDSConfig:
    return OPDSConfig.from_file(config_path)
