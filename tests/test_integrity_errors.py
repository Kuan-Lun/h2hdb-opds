import errno
import fcntl
import os
from dataclasses import replace
from typing import Any

import pytest
from h2hdb import (
    CatalogDiscoveryBundle,
    CatalogFacetKind,
    CatalogFacetPage,
    CatalogRecentWindow,
)

from h2hdb_opds import OPDSConfig, create_app, library

from .fakes import CatalogFixture
from .http_client import app_client


@pytest.mark.parametrize("prefix", ("/opds/v1.2", "/opds/v2"))
@pytest.mark.parametrize(
    "corruption", ("page-revision", "facet-revision", "facet-order")
)
async def test_discovery_integrity_failure_never_recovers_after_head_advance(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    prefix: str,
    corruption: str,
) -> None:
    catalog = catalog_fixture.catalog
    original = catalog.discover_publications_with_facets

    def corrupt_bundle(**kwargs: Any) -> CatalogDiscoveryBundle:
        bundle = original(**kwargs)
        wrong_revision = replace(bundle.page.revision, revision=6)
        if corruption == "page-revision":
            object.__setattr__(bundle.page, "revision", wrong_revision)
        elif corruption == "facet-revision":
            object.__setattr__(bundle.facets[0], "revision", wrong_revision)
        else:
            object.__setattr__(bundle, "facets", tuple(reversed(bundle.facets)))
        catalog.revision = replace(catalog.revision, revision=8)
        return bundle

    monkeypatch.setattr(catalog, "discover_publications_with_facets", corrupt_bundle)
    app = create_app(opds_config, catalog)
    async with app_client(app) as client:
        response = await client.get(f"{prefix}/publications", params={"revision": 7})

    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert response.headers["cache-control"] == "no-store"
    assert "retry-after" not in response.headers
    assert "location" not in response.headers
    assert catalog.revision_lookups == []
    assert (
        "uvicorn.error",
        40,
        f"catalog_integrity_error: {response.json()['detail']}",
    ) in (caplog.record_tuples)


@pytest.mark.parametrize(
    "corruption", ("facet-kind", "facet-revision", "recent-revision")
)
async def test_pinned_facet_and_recent_inconsistency_is_not_a_missing_revision(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    catalog = catalog_fixture.catalog
    wrong_revision = replace(catalog.revision, revision=6)
    if corruption == "recent-revision":
        original_recent = catalog.list_recent_publications

        def corrupt_recent(**kwargs: Any) -> CatalogRecentWindow:
            window = original_recent(**kwargs)
            object.__setattr__(window, "revision", wrong_revision)
            return window

        monkeypatch.setattr(catalog, "list_recent_publications", corrupt_recent)
        path = "/opds/v1.2/recent/uploaded"
    else:
        original_facets = catalog.list_publication_facets

        def corrupt_facets(**kwargs: Any) -> CatalogFacetPage:
            page = original_facets(**kwargs)
            if corruption == "facet-kind":
                object.__setattr__(page, "facet", CatalogFacetKind.SUBJECT)
            else:
                object.__setattr__(page, "revision", wrong_revision)
            return page

        monkeypatch.setattr(catalog, "list_publication_facets", corrupt_facets)
        path = "/opds/v2/facets/language"
    app = create_app(opds_config, catalog)
    async with app_client(app) as client:
        response = await client.get(path, params={"revision": 7})
    assert response.status_code == 500
    assert response.json()["code"] == "catalog_integrity_error"
    assert "retry-after" not in response.headers
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "fault",
    (
        "missing-lock",
        "nonregular-lock",
        "open-denied",
        "flock-denied",
        "marker-stat-denied",
    ),
)
async def test_coordination_faults_are_not_reported_as_activation(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault: str,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    lock_path = opds_config.coordination_root / "publication.lock"
    async with app_client(app) as client:
        if fault == "missing-lock":
            lock_path.unlink()
        elif fault == "nonregular-lock":
            lock_path.unlink()
            lock_path.mkdir()
        elif fault == "open-denied":

            def denied_open(_descriptor: int) -> int:
                raise PermissionError(errno.EACCES, "permission denied")

            monkeypatch.setattr(library, "_open_publication_lock", denied_open)
        elif fault == "flock-denied":

            def denied_flock(_descriptor: int, _operation: int) -> None:
                raise PermissionError(errno.EACCES, "permission denied")

            monkeypatch.setattr(fcntl, "flock", denied_flock)
        else:
            original_stat = os.stat

            def denied_marker(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
                if path == "ACTIVATING":
                    raise PermissionError(errno.EACCES, "permission denied")
                return original_stat(path, *args, **kwargs)

            monkeypatch.setattr(os, "stat", denied_marker)
        response = await client.get("/opds/v2/publications", params={"revision": 6})
        health = await client.get("/health")
    assert response.status_code == 500
    assert response.json()["code"] == "library_integrity_error"
    assert response.headers["cache-control"] == "no-store"
    assert "retry-after" not in response.headers
    assert "location" not in response.headers
    assert health.status_code == 200
    assert (
        "uvicorn.error",
        40,
        f"library_integrity_error: {response.json()['detail']}",
    ) in (caplog.record_tuples)


@pytest.mark.parametrize("activation", ("marker", "exclusive-lock"))
async def test_actual_activation_keeps_maintenance_code_and_retry_after(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    activation: str,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    descriptor = -1
    try:
        async with app_client(app) as client:
            if activation == "marker":
                (opds_config.coordination_root / "ACTIVATING").touch()
            else:
                descriptor = os.open(
                    opds_config.coordination_root / "publication.lock", os.O_RDWR
                )
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            response = await client.get("/opds/v2/publications")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    assert response.status_code == 503
    assert response.json()["code"] == "library_activating"
    assert response.headers["retry-after"] == "1"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("invalid_offset", (-1, 1000))
async def test_invalid_sealed_image_extent_is_a_catalog_integrity_failure(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    invalid_offset: int,
) -> None:
    cover = catalog_fixture.publications[0].cover
    assert cover is not None
    object.__setattr__(cover.extent, "offset", invalid_offset)
    app = create_app(opds_config, catalog_fixture.catalog)
    async with app_client(app) as client:
        response = await client.get("/media/publications/urn:h2h:gallery:1001/pages/0")
    assert response.status_code == 500
    assert response.json() == {
        "code": "catalog_integrity_error",
        "detail": "Resource extent is invalid",
    }
    assert "retry-after" not in response.headers
