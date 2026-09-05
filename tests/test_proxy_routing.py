from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from h2hdb_opds import BasicAuthConfig, OPDSConfig, ServerConfig, create_app
from h2hdb_opds.atom import ATOM_NAMESPACE

from .fakes import CatalogFixture

_DEPLOYMENTS = ("direct", "stripped", "root_path", "unnamed_mount", "named_mount")
_PUBLIC_ORIGIN = "https://public.example"


def _prefix(deployment: str) -> str:
    return "" if deployment == "direct" else "/library"


def _config(
    config: OPDSConfig, deployment: str, *, authenticated: bool = False
) -> OPDSConfig:
    return OPDSConfig(
        library_root=config.library_root,
        coordination_root=config.coordination_root,
        public_base_url=_PUBLIC_ORIGIN + _prefix(deployment),
        auth=(
            BasicAuthConfig(username="reader", password=SecretStr("secret"))
            if authenticated
            else BasicAuthConfig()
        ),
        server=ServerConfig(trusted_proxy_ips=("127.0.0.1",)),
    )


class _PrefixStrippingProxy:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        forwarded = dict(scope)
        if scope["type"] == "http" and scope["path"].startswith("/library/"):
            forwarded["path"] = scope["path"][len("/library") :]
            forwarded["raw_path"] = scope["raw_path"][len(b"/library") :]
        await self._app(forwarded, receive, send)


@asynccontextmanager
async def _client(
    application: FastAPI,
    deployment: str,
    *,
    base_url: str = _PUBLIC_ORIGIN,
) -> AsyncIterator[AsyncClient]:
    target: ASGIApp = application
    if deployment == "stripped":
        target = _PrefixStrippingProxy(application)
    elif deployment in {"unnamed_mount", "named_mount"}:
        outer = FastAPI()
        outer.mount(
            "/library",
            application,
            name="library" if deployment == "named_mount" else None,
        )
        target = outer
    root_path = "/library" if deployment == "root_path" else ""
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=target, root_path=root_path),
            base_url=base_url,
        ) as client:
            yield client


def _json_links(value: object) -> list[tuple[str, str]]:
    if isinstance(value, list):
        return [link for child in value for link in _json_links(child)]
    if not isinstance(value, dict):
        return []
    href = value.get("href")
    found = [(str(value.get("rel", "")), href)] if isinstance(href, str) else []
    return [*found, *(link for child in value.values() for link in _json_links(child))]


def _links(response: Response) -> list[tuple[str, str]]:
    assert response.status_code == 200
    if response.headers["content-type"].startswith("application/atom+xml"):
        root = ElementTree.fromstring(response.content)
        return [
            (link.attrib["rel"], link.attrib["href"])
            for link in root.iter(f"{{{ATOM_NAMESPACE}}}link")
        ]
    return _json_links(response.json())


def _href(response: Response, relation: str) -> str:
    return next(href for rel, href in _links(response) if rel == relation)


@pytest.mark.parametrize("deployment", _DEPLOYMENTS)
@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
async def test_canonical_links_and_revision_recovery_use_one_public_prefix(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    deployment: str,
    protocol: str,
) -> None:
    config = _config(opds_config, deployment)
    app = create_app(config, catalog_fixture.catalog)
    prefix = _prefix(deployment)
    catalog_path = f"/opds/{protocol}" + ("/catalog" if protocol == "v1.2" else "")
    async with _client(app, deployment) as client:
        root = await client.get(
            prefix + catalog_path, headers={"Host": "attacker.invalid"}
        )
        assert (
            _href(root, "self")
            == _href(root, "start")
            == config.public_base_url + catalog_path
        )
        feed = await client.get(_href(root, "subsection"))
        detail = await client.get(
            f"{prefix}/opds/{protocol}/publications/urn:h2h:gallery:1001?revision=7"
        )
        for document in (root, feed, detail):
            for _relation, href in _links(document):
                parsed = urlsplit(href)
                assert parsed.scheme == "https"
                assert parsed.netloc == "public.example"
                assert parsed.path.startswith((f"{prefix}/opds/", f"{prefix}/media/"))
        image_url = _href(detail, "http://opds-spec.org/image")
        assert parse_qs(urlsplit(image_url).query) == {"revision": ["7"]}
        image = await client.get(image_url)
        assert image.status_code == 200
        assert image.content == catalog_fixture.payload[4:16]

        catalog_fixture.catalog.revision = replace(
            catalog_fixture.catalog.revision, revision=8
        )
        stale = await client.get(
            f"{prefix}/opds/{protocol}/publications?revision=7&limit=1",
            headers={"Host": "attacker.invalid"},
        )
        assert stale.status_code == 303
        assert (
            stale.headers["location"]
            == f"{config.public_base_url}/opds/{protocol}/publications?limit=1"
        )
        assert stale.headers["cache-control"] == "no-store"
        assert (await client.get(stale.headers["location"])).status_code == 200


@pytest.mark.parametrize("deployment", _DEPLOYMENTS)
@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
@pytest.mark.parametrize(
    ("method", "final_status"), (("GET", 200), ("POST", 405), ("HEAD", 200))
)
async def test_slash_redirects_preserve_method_and_query_under_canonical_origin(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    deployment: str,
    protocol: str,
    method: str,
    final_status: int,
) -> None:
    config = _config(opds_config, deployment)
    app = create_app(config, catalog_fixture.catalog)
    path = f"/opds/{protocol}/publications"
    query = "revision=7&language=en&x=%2f%FF+%20&x=second"
    async with _client(app, deployment, base_url="http://internal:8000") as client:
        response = await client.request(
            method,
            f"{_prefix(deployment)}{path}/?{query}",
            headers={"Host": "attacker.invalid"},
        )
        assert response.status_code == 307
        assert response.headers["location"] == f"{config.public_base_url}{path}?{query}"
        assert response.headers["cache-control"] == "no-store"
        followed = await client.request(method, response.headers["location"])
        expected = 405 if method == "HEAD" and protocol == "v2" else final_status
        assert followed.status_code == expected


@pytest.mark.parametrize("deployment", _DEPLOYMENTS)
async def test_media_slash_redirect_encodes_path_parameters_once(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    deployment: str,
) -> None:
    config = _config(opds_config, deployment)
    app = create_app(config, catalog_fixture.catalog)
    path = "/media/publications/urn%3Ah2h%3Agallery%3A1001/pages/0"
    async with _client(app, deployment) as client:
        response = await client.get(f"{_prefix(deployment)}{path}/?revision=7")
        assert response.status_code == 307
        assert (
            response.headers["location"] == f"{config.public_base_url}{path}?revision=7"
        )
        image = await client.get(response.headers["location"])
    assert image.status_code == 200
    assert image.content == catalog_fixture.payload[4:16]


@pytest.mark.parametrize("deployment", _DEPLOYMENTS)
async def test_slash_redirect_does_not_recover_invalid_queries_or_unknown_routes(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    deployment: str,
) -> None:
    config = _config(opds_config, deployment)
    app = create_app(config, catalog_fixture.catalog)
    prefix = _prefix(deployment)
    async with _client(app, deployment) as client:
        invalid = await client.get(
            f"{prefix}/opds/v2/publications/?revision=7&limit=bad"
        )
        assert invalid.status_code == 307
        assert (
            invalid.headers["location"]
            == f"{config.public_base_url}/opds/v2/publications?revision=7&limit=bad"
        )
        assert (await client.get(invalid.headers["location"])).status_code == 422
        unknown = await client.get(f"{prefix}/opds/missing/?revision=7")
        assert unknown.status_code == 404
        assert "location" not in unknown.headers
        method = await client.post(f"{prefix}/opds/v2/publications?revision=7")
        assert method.status_code == 405
        assert "location" not in method.headers


@pytest.mark.parametrize("deployment", _DEPLOYMENTS)
@pytest.mark.parametrize("protocol", ("v1.2", "v2"))
async def test_authentication_remains_protocol_correct_after_prefixed_redirect(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    deployment: str,
    protocol: str,
) -> None:
    config = _config(opds_config, deployment, authenticated=True)
    app = create_app(config, catalog_fixture.catalog)
    path = f"{_prefix(deployment)}/opds/{protocol}/publications"
    async with _client(app, deployment) as client:
        slash = await client.get(f"{path}/?revision=6")
        assert slash.status_code == 307
        unauthorized = await client.get(slash.headers["location"])
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"].startswith("Basic ")
        if protocol == "v1.2":
            assert unauthorized.content == b""
            assert unauthorized.headers["cache-control"] == "no-store"
        else:
            assert (
                unauthorized.headers["content-type"]
                == "application/opds-authentication+json"
            )
            assert (
                unauthorized.json()["links"][0]["href"]
                == f"{config.public_base_url}/opds/v2/authentication"
            )
        recovered = await client.get(
            slash.headers["location"], auth=("reader", "secret"), follow_redirects=True
        )
        assert recovered.status_code == 200
        assert [response.status_code for response in recovered.history] == [303]

    async with _client(app, deployment, base_url="http://internal:8000") as client:
        insecure = await client.get(
            path,
            auth=("reader", "secret"),
            headers={"X-Forwarded-Proto": "https"},
        )
    assert insecure.status_code == 426
