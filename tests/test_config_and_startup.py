import json
from pathlib import Path

import pytest
from h2hdb import CoreConfig, DatabaseAccessMode
from pydantic import SecretStr

import h2hdb_opds.app as app_module
from h2hdb_opds import (
    BasicAuthConfig,
    OPDSConfig,
    ServerConfig,
    create_app,
    load_config,
)

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client


def test_loader_resolves_database_and_auth_secrets_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root = tmp_path / "current"
    coordination_root = tmp_path / "coordination"
    monkeypatch.setenv("H2HDB_OPDS_LIBRARY_ROOT", str(library_root))
    monkeypatch.setenv("H2HDB_OPDS_COORDINATION_ROOT", str(coordination_root))
    monkeypatch.setenv("H2HDB_OPDS_DATABASE_PASSWORD", "read-secret")
    monkeypatch.setenv("H2HDB_OPDS_AUTH_PASSWORD", "reader-secret")
    config_path = tmp_path / "opds.json"
    config_path.write_text(
        json.dumps(
            {
                "library_root": "${H2HDB_OPDS_LIBRARY_ROOT}",
                "coordination_root": "${H2HDB_OPDS_COORDINATION_ROOT}",
                "public_base_url": "https://books.example",
                "core": {
                    "database": {
                        "password": "${H2HDB_OPDS_DATABASE_PASSWORD}",
                    }
                },
                "auth": {
                    "username": "reader",
                    "password": "${H2HDB_OPDS_AUTH_PASSWORD}",
                },
                "server": {
                    "trusted_proxy_ips": ["127.0.0.1"],
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.library_root == library_root
    assert config.coordination_root == coordination_root
    assert config.core.database.password == "read-secret"
    assert config.auth.password is not None
    assert config.auth.password.get_secret_value() == "reader-secret"
    assert config.core.database.access_mode is DatabaseAccessMode.read_only


def test_database_access_is_read_only_by_default_and_forced(
    opds_config: OPDSConfig,
) -> None:
    default_config = opds_config
    explicitly_writable = OPDSConfig(
        library_root=opds_config.library_root,
        coordination_root=opds_config.coordination_root,
        public_base_url=opds_config.public_base_url,
        core=CoreConfig(),
    )
    raw_writable = OPDSConfig.model_validate(
        {
            "library_root": str(opds_config.library_root),
            "coordination_root": str(opds_config.coordination_root),
            "public_base_url": opds_config.public_base_url,
            "core": {"database": {"access_mode": "read-write"}},
        }
    )

    assert default_config.core.database.access_mode is DatabaseAccessMode.read_only
    assert explicitly_writable.core.database.access_mode is DatabaseAccessMode.read_only
    assert raw_writable.core.database.access_mode is DatabaseAccessMode.read_only


def test_legacy_artifact_root_is_not_a_compatibility_alias(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        OPDSConfig.model_validate(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "public_base_url": "http://catalog.example",
            }
        )


def test_catalog_page_configuration_is_bounded_by_core(
    opds_config: OPDSConfig,
) -> None:
    assert opds_config.maximum_page_size == 128
    with pytest.raises(ValueError):
        OPDSConfig(
            library_root=opds_config.library_root,
            coordination_root=opds_config.coordination_root,
            public_base_url=opds_config.public_base_url,
            maximum_page_size=129,
        )


async def test_injected_catalog_starts_without_a_legacy_compatibility_hook(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    assert not hasattr(catalog_fixture.catalog, "check_compatibility")
    async with app_client(app) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_basic_auth_requires_https_and_an_explicit_tls_boundary(
    opds_config: OPDSConfig,
) -> None:
    auth = BasicAuthConfig(username="reader", password=SecretStr("secret"))

    with pytest.raises(ValueError, match="HTTPS public_base_url"):
        OPDSConfig(
            library_root=opds_config.library_root,
            coordination_root=opds_config.coordination_root,
            public_base_url="http://books.example",
            auth=auth,
            server=ServerConfig(trusted_proxy_ips=("127.0.0.1",)),
        )
    with pytest.raises(ValueError, match="TLS-terminating proxy"):
        OPDSConfig(
            library_root=opds_config.library_root,
            coordination_root=opds_config.coordination_root,
            public_base_url="https://books.example",
            auth=auth,
        )

    proxied = OPDSConfig(
        library_root=opds_config.library_root,
        coordination_root=opds_config.coordination_root,
        public_base_url="https://books.example",
        auth=auth,
        server=ServerConfig(trusted_proxy_ips=("127.0.0.1",)),
    )
    direct = OPDSConfig(
        library_root=opds_config.library_root,
        coordination_root=opds_config.coordination_root,
        public_base_url="https://books.example",
        auth=auth,
        server=ServerConfig(
            tls_certificate=opds_config.library_root / "server.crt",
            tls_private_key=opds_config.library_root / "server.key",
        ),
    )

    assert proxied.server.trusted_proxy_ips == ("127.0.0.1/32",)
    assert direct.server.serves_tls


def test_basic_auth_rejects_unrepresentable_or_ambiguous_header_values() -> None:
    with pytest.raises(ValueError, match="must not contain ':'"):
        BasicAuthConfig(username="reader:name", password=SecretStr("secret"))
    with pytest.raises(ValueError, match="HTTP headers"):
        BasicAuthConfig(realm="私人目錄")


def test_public_url_and_trusted_proxy_configuration_fail_closed(
    opds_config: OPDSConfig,
) -> None:
    for invalid_url in (
        "https://books.example:99999",
        "https://books.example/bad path",
        "https://books.example/%invalid",
        "https://books.example/目錄",
    ):
        with pytest.raises(ValueError, match="public_base_url"):
            OPDSConfig(
                library_root=opds_config.library_root,
                coordination_root=opds_config.coordination_root,
                public_base_url=invalid_url,
            )

    for trusted_everywhere in ("*", "0.0.0.0/0", "::/0"):
        with pytest.raises(ValueError, match="must not trust every address"):
            ServerConfig(trusted_proxy_ips=(trusted_everywhere,))


async def test_protected_catalog_refuses_basic_credentials_over_http(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = OPDSConfig(
        library_root=opds_config.library_root,
        coordination_root=opds_config.coordination_root,
        public_base_url="https://books.example",
        auth=BasicAuthConfig(username="reader", password=SecretStr("secret")),
        server=ServerConfig(trusted_proxy_ips=("127.0.0.1",)),
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        response = await client.get("/opds/v2", auth=("reader", "secret"))

    assert response.status_code == 426
    assert response.headers["upgrade"].startswith("TLS/")
    assert "www-authenticate" not in response.headers


async def test_startup_rejects_a_symlink_library_root(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    linked_root = opds_config.library_root / "linked-root"
    real_root = opds_config.library_root / "real-root"
    real_root.mkdir()
    linked_root.symlink_to(real_root, target_is_directory=True)
    config = opds_config.model_copy(update={"library_root": linked_root})
    app = create_app(config, catalog_fixture.catalog)

    with pytest.raises(RuntimeError, match="real directory"):
        async with app_client(app):
            pass


async def test_startup_rejects_untrusted_coordination_contract(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    tmp_path: Path,
) -> None:
    missing_lock_root = tmp_path / "missing-lock"
    missing_lock_root.mkdir()
    missing_lock = opds_config.model_copy(
        update={"coordination_root": missing_lock_root}
    )
    linked_root = tmp_path / "linked-coordination"
    linked_root.symlink_to(
        opds_config.coordination_root,
        target_is_directory=True,
    )
    linked_coordination = opds_config.model_copy(
        update={"coordination_root": linked_root}
    )
    linked_lock_root = tmp_path / "linked-lock"
    linked_lock_root.mkdir()
    (linked_lock_root / "publication.lock").symlink_to(
        opds_config.coordination_root / "publication.lock"
    )
    linked_lock = opds_config.model_copy(update={"coordination_root": linked_lock_root})

    for config in (missing_lock, linked_coordination, linked_lock):
        with pytest.raises(RuntimeError, match="Coordination root"):
            async with app_client(create_app(config, catalog_fixture.catalog)):
                pass


async def test_production_startup_uses_read_only_open_database_once(
    catalog_fixture: CatalogFixture,
    monkeypatch: pytest.MonkeyPatch,
    opds_config: OPDSConfig,
) -> None:
    opened_access_modes: list[DatabaseAccessMode] = []

    def fake_open_database(config: CoreConfig) -> FakeCatalog:
        opened_access_modes.append(config.database.access_mode)
        return catalog_fixture.catalog

    monkeypatch.setattr(app_module, "open_database", fake_open_database)
    app = create_app(
        OPDSConfig(
            library_root=opds_config.library_root,
            coordination_root=opds_config.coordination_root,
            public_base_url=opds_config.public_base_url,
            core=CoreConfig(),
        )
    )

    async with app_client(app) as client:
        assert (await client.get("/health")).status_code == 200

    assert opened_access_modes == [DatabaseAccessMode.read_only]
