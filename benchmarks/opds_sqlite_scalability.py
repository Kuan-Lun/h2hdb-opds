"""Measure the public OPDS HTTP surface over a core-owned READY SQLite fixture.

The input database and receipt must be emitted together by
``h2hdb/benchmarks/sqlite_catalog_scalability.py``.  This tool never writes SQL,
imports a core implementation module, or injects a catalog reader.  It opens the
database through the normal OPDS application lifespan, which in turn calls the
public ``h2hdb.open_database(..., read_only=True)`` boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import platform
import stat
import sys
import tempfile
import time
import tracemalloc
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI
from h2hdb import CoreConfig, DatabaseAccessMode, DatabaseConfig
from httpx import ASGITransport, AsyncClient

from benchmarks.opds_scalability import (
    _FetchedResponse,
    _get_response,
    _json_object,
    _latency_summary,
    _next_link,
    _process_max_rss_bytes,
    _response_shape,
    _same_response,
    _source_provenance_document,
    collect_source_provenance,
)
from h2hdb_opds import OPDSConfig, create_app

BENCHMARK_SCHEMA_VERSION: Final = 1
SUPPORTED_CORE_RECEIPT_FORMAT: Final = "h2hdb-sqlite-catalog-scalability-v1"
SUPPORTED_CORE_RECEIPT_SCHEMA_VERSION: Final = 1
CORE_FIXTURE_MODE: Final = "manifest-bound-sql"
_MAX_FIXTURE_RECEIPT_BYTES: Final = 4 * 1024 * 1024
_HASH_CHUNK_BYTES: Final = 1024 * 1024
_PAGE_LIMIT: Final = 32
_FACET_LIMIT: Final = 128
_PUBLIC_BASE_URL: Final = "http://benchmark.invalid"
_CATALOG_TITLE: Final = "H2HDB OPDS SQL scalability benchmark"
_FACET_FAMILIES: Final = ("language", "subject", "contributor")
_FACET_TITLES: Final = {
    "language": "Language",
    "subject": "Tag",
    "contributor": "Contributor",
}
_OPERATION_ORDER: Final = (
    "discovery_first_page",
    "discovery_cursor_page",
    "nonempty_search_first_page",
    "nonempty_search_cursor_page",
    "facet_language_first_page",
    "facet_subject_first_page",
    "facet_contributor_first_page",
)
_REQUIRED_DATABASE_COUNTS: Final = (
    "publication_count",
    "artifact_count",
    "artifact_blob_count",
    "acquisition_descriptor_count",
    "search_document_count",
    "search_posting_count",
)


class FixtureReceiptError(ValueError):
    """The core fixture receipt or its database binding is malformed."""


@dataclass(frozen=True, slots=True)
class CoreFixtureAuthority:
    """Validated, path-free authority consumed by the HTTP benchmark."""

    receipt_file_sha256: str
    receipt_file_size_bytes: int
    receipt_format: str
    receipt_schema_version: int
    fixture_contract_sha256: str
    fixture_contract_includes_receipt_schema_version: bool
    fixture_mode: str
    core_version: str
    core_source_manifest_sha256: str
    seed: int
    profile: str
    schema_epoch: int
    schema_version: int
    schema_manifest_sha256: str
    publication_count: int
    artifact_count: int
    acquisition_descriptor_count: int
    search_query: str
    search_publication_count: int
    search_first_page_gids: tuple[int, ...]
    search_cursor_page_gids: tuple[int, ...]
    search_facet_counts: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    database_sha256: str
    database_size_bytes: int

    def facet_counts(self) -> dict[str, dict[str, int]]:
        return {family: dict(values) for family, values in self.search_facet_counts}


@dataclass(frozen=True, slots=True)
class _FileDigest:
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _TimingPass:
    startup_ready_audit_ns: int
    operations: dict[str, dict[str, object]]
    responses: dict[str, _FetchedResponse]
    operation_order: tuple[str, ...]


def _exact_int(value: object, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise FixtureReceiptError(f"{field} must be an integer >= {minimum}")
    return value


def _nonblank_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureReceiptError(f"{field} must be a nonblank string")
    return value


def _sha256_text(value: object, *, field: str) -> str:
    candidate = _nonblank_text(value, field=field)
    if len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise FixtureReceiptError(f"{field} must be a lowercase SHA-256 digest")
    return candidate


def _object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FixtureReceiptError(f"{field} must be a JSON object")
    return cast("dict[str, object]", value)


def _array(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list):
        raise FixtureReceiptError(f"{field} must be a JSON array")
    return cast("list[object]", value)


def _required(document: Mapping[str, object], key: str, *, field: str) -> object:
    try:
        return document[key]
    except KeyError as error:
        raise FixtureReceiptError(f"{field}.{key} is required") from error


def _parse_unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FixtureReceiptError(f"fixture receipt repeats JSON key {key!r}")
        result[key] = value
    return result


def _read_receipt(path: Path) -> tuple[dict[str, object], _FileDigest]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    if not stat.S_ISREG(before.st_mode):
        raise FixtureReceiptError("fixture receipt must be a regular file")
    if before.st_size > _MAX_FIXTURE_RECEIPT_BYTES:
        raise FixtureReceiptError(
            "fixture receipt exceeds the 4 MiB benchmark input limit"
        )
    payload = resolved.read_bytes()
    after = resolved.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or len(payload) != after.st_size:
        raise FixtureReceiptError("fixture receipt changed while it was read")
    try:
        decoded: object = json.loads(payload, object_pairs_hook=_parse_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixtureReceiptError("fixture receipt is not valid UTF-8 JSON") from error
    return (
        _object(decoded, field="fixture receipt"),
        _FileDigest(
            sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
        ),
    )


def _digest_regular_file(path: Path, *, field: str) -> _FileDigest:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    if not stat.S_ISREG(before.st_mode):
        raise FixtureReceiptError(f"{field} must be a regular file")
    digest = hashlib.sha256()
    size_bytes = 0
    with resolved.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            size_bytes += len(chunk)
    after = resolved.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or size_bytes != after.st_size:
        raise FixtureReceiptError(f"{field} changed while it was hashed")
    return _FileDigest(sha256=digest.hexdigest(), size_bytes=size_bytes)


def _canonical_json_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _int_list(value: object, *, field: str) -> tuple[int, ...]:
    items = _array(value, field=field)
    return tuple(
        _exact_int(item, field=f"{field}[{position}]", minimum=1)
        for position, item in enumerate(items)
    )


def _facet_count_map(value: object, *, field: str) -> dict[str, int]:
    document = _object(value, field=field)
    result: dict[str, int] = {}
    for name, raw_count in document.items():
        if not name:
            raise FixtureReceiptError(f"{field} contains a blank facet value")
        result[name] = _exact_int(raw_count, field=f"{field}.{name}")
    if not result:
        raise FixtureReceiptError(f"{field} must not be empty")
    return result


def _string_list(value: object, *, field: str) -> tuple[str, ...]:
    items = _array(value, field=field)
    result = tuple(
        _nonblank_text(item, field=f"{field}[{position}]")
        for position, item in enumerate(items)
    )
    if len(result) != len(set(result)):
        raise FixtureReceiptError(f"{field} must not contain duplicates")
    return result


def load_core_fixture(
    database_path: Path,
    fixture_receipt_path: Path,
) -> CoreFixtureAuthority:
    """Validate the core receipt, its path-free contract, and exact DB bytes."""

    receipt, receipt_file = _read_receipt(fixture_receipt_path)
    receipt_format = _nonblank_text(
        _required(receipt, "format", field="receipt"), field="receipt.format"
    )
    if receipt_format != SUPPORTED_CORE_RECEIPT_FORMAT:
        raise FixtureReceiptError(
            f"unsupported core fixture receipt format: {receipt_format!r}"
        )
    declared_receipt_schema = receipt.get(
        "receipt_schema_version", SUPPORTED_CORE_RECEIPT_SCHEMA_VERSION
    )
    receipt_schema_version = _exact_int(
        declared_receipt_schema,
        field="receipt.receipt_schema_version",
        minimum=1,
    )
    if receipt_schema_version != SUPPORTED_CORE_RECEIPT_SCHEMA_VERSION:
        raise FixtureReceiptError(
            f"unsupported core fixture receipt schema version: {receipt_schema_version}"
        )
    fixture_mode = _nonblank_text(
        _required(receipt, "fixture_mode", field="receipt"),
        field="receipt.fixture_mode",
    )
    if fixture_mode != CORE_FIXTURE_MODE:
        raise FixtureReceiptError("core fixture is not manifest-bound SQL")
    if receipt.get("fixture_refines_complete_ingest_state_machine") is not False:
        raise FixtureReceiptError(
            "core fixture must explicitly disclaim complete ingest refinement"
        )

    core_version = _nonblank_text(
        _required(receipt, "core_version", field="receipt"),
        field="receipt.core_version",
    )
    source = _object(
        _required(receipt, "source_provenance", field="receipt"),
        field="receipt.source_provenance",
    )
    if source.get("project_version") != core_version:
        raise FixtureReceiptError(
            "core source provenance version disagrees with receipt.core_version"
        )
    core_source_manifest_sha256 = _sha256_text(
        _required(source, "source_manifest_sha256", field="source_provenance"),
        field="receipt.source_provenance.source_manifest_sha256",
    )
    _nonblank_text(
        _required(source, "source_manifest_algorithm", field="source_provenance"),
        field="receipt.source_provenance.source_manifest_algorithm",
    )
    _exact_int(
        _required(source, "source_manifest_file_count", field="source_provenance"),
        field="receipt.source_provenance.source_manifest_file_count",
        minimum=1,
    )

    seed = _exact_int(_required(receipt, "seed", field="receipt"), field="receipt.seed")
    profile = _nonblank_text(
        _required(receipt, "profile", field="receipt"), field="receipt.profile"
    )
    schema = _object(
        _required(receipt, "schema", field="receipt"), field="receipt.schema"
    )
    schema_epoch = _exact_int(
        _required(schema, "epoch", field="schema"),
        field="receipt.schema.epoch",
        minimum=1,
    )
    schema_version = _exact_int(
        _required(schema, "schema_version", field="schema"),
        field="receipt.schema.schema_version",
        minimum=1,
    )
    if schema_epoch != 3 or schema_version != 3:
        raise FixtureReceiptError("core fixture must use schema epoch 3/version 3")
    if (
        schema.get("state") != "READY"
        or schema.get("full_ready_audit_passed") is not True
    ):
        raise FixtureReceiptError("core fixture must carry a successful READY audit")
    schema_manifest_sha256 = _sha256_text(
        _required(schema, "manifest_sha256", field="schema"),
        field="receipt.schema.manifest_sha256",
    )

    fixture = _object(
        _required(receipt, "fixture", field="receipt"), field="receipt.fixture"
    )
    publication_count = _exact_int(
        _required(fixture, "publication_count", field="fixture"),
        field="receipt.fixture.publication_count",
        minimum=1,
    )
    if fixture.get("creates_cbz_or_artwork_bytes") is not False:
        raise FixtureReceiptError(
            "core fixture must explicitly create no CBZ or artwork bytes"
        )
    if not _string_list(
        _required(fixture, "production_family_bindings", field="fixture"),
        field="receipt.fixture.production_family_bindings",
    ):
        raise FixtureReceiptError("core fixture has no production family bindings")
    manifest_bound_tables = _string_list(
        _required(fixture, "manifest_bound_tables", field="fixture"),
        field="receipt.fixture.manifest_bound_tables",
    )
    if not manifest_bound_tables or manifest_bound_tables != tuple(
        sorted(manifest_bound_tables)
    ):
        raise FixtureReceiptError(
            "core fixture manifest-bound table evidence must be nonempty and sorted"
        )

    expected = _object(
        _required(receipt, "expected", field="receipt"), field="receipt.expected"
    )
    expected_revision = _exact_int(
        _required(expected, "revision", field="expected"),
        field="receipt.expected.revision",
        minimum=1,
    )
    expected_counts = {
        name: _exact_int(
            _required(expected, name, field="expected"),
            field=f"receipt.expected.{name}",
            minimum=1,
        )
        for name in _REQUIRED_DATABASE_COUNTS
    }
    if expected_counts["publication_count"] != publication_count:
        raise FixtureReceiptError("fixture and expected publication counts disagree")
    if not (
        expected_counts["artifact_count"]
        == expected_counts["acquisition_descriptor_count"]
        == publication_count
    ):
        raise FixtureReceiptError(
            "fixture must have one artifact and acquisition descriptor per publication"
        )
    if expected_counts["search_document_count"] != publication_count:
        raise FixtureReceiptError(
            "fixture must have one search document per publication"
        )

    search = _object(
        _required(expected, "search", field="expected"), field="receipt.expected.search"
    )
    search_query = _nonblank_text(
        _required(search, "query", field="search"),
        field="receipt.expected.search.query",
    )
    search_publication_count = _exact_int(
        _required(search, "publication_count", field="search"),
        field="receipt.expected.search.publication_count",
        minimum=1,
    )
    first_gids = _int_list(
        _required(search, "first_page_gids", field="search"),
        field="receipt.expected.search.first_page_gids",
    )
    cursor_gids = _int_list(
        _required(search, "cursor_page_gids", field="search"),
        field="receipt.expected.search.cursor_page_gids",
    )
    if len(first_gids) != _PAGE_LIMIT or not cursor_gids:
        raise FixtureReceiptError(
            "core fixture must provide one full search page and a nonempty cursor page"
        )
    if len(cursor_gids) > _PAGE_LIMIT or len({*first_gids, *cursor_gids}) != (
        len(first_gids) + len(cursor_gids)
    ):
        raise FixtureReceiptError("search page GIDs are duplicated or unbounded")
    if first_gids != tuple(sorted(first_gids)) or cursor_gids != tuple(
        sorted(cursor_gids)
    ):
        raise FixtureReceiptError("search page GIDs must use canonical ascending order")
    if search_publication_count < len(first_gids) + len(cursor_gids):
        raise FixtureReceiptError("search count is smaller than its receipt pages")

    facets = _object(
        _required(expected, "facets", field="expected"), field="receipt.expected.facets"
    )
    if set(facets) != set(_FACET_FAMILIES):
        raise FixtureReceiptError("expected facet families are not canonical")
    facet_maps = {
        family: _facet_count_map(
            _required(facets, family, field="facets"),
            field=f"receipt.expected.facets.{family}",
        )
        for family in _FACET_FAMILIES
    }
    for family, counts in facet_maps.items():
        if sum(counts.values()) != search_publication_count:
            raise FixtureReceiptError(
                f"{family} facet counts do not partition the search result"
            )
        if sum(count > 0 for count in counts.values()) > _FACET_LIMIT:
            raise FixtureReceiptError(
                f"{family} fixture exceeds the benchmark facet cap"
            )

    actual_counts = _object(
        _required(receipt, "actual_database_counts", field="receipt"),
        field="receipt.actual_database_counts",
    )
    for name in _REQUIRED_DATABASE_COUNTS:
        actual = _exact_int(
            _required(actual_counts, name, field="actual_database_counts"),
            field=f"receipt.actual_database_counts.{name}",
            minimum=1,
        )
        if actual != expected_counts[name]:
            raise FixtureReceiptError(
                f"actual database count {name!r} disagrees with expected authority"
            )

    database = _object(
        _required(receipt, "database", field="receipt"), field="receipt.database"
    )
    expected_database_sha256 = _sha256_text(
        _required(database, "sha256", field="database"),
        field="receipt.database.sha256",
    )
    expected_database_size = _exact_int(
        _required(database, "size_bytes", field="database"),
        field="receipt.database.size_bytes",
        minimum=1,
    )
    observed_database = _digest_regular_file(database_path, field="SQLite database")
    if observed_database != _FileDigest(
        sha256=expected_database_sha256,
        size_bytes=expected_database_size,
    ):
        raise FixtureReceiptError(
            "SQLite database bytes disagree with the core fixture receipt"
        )

    fixture_contract_sha256 = _sha256_text(
        _required(receipt, "fixture_contract_sha256", field="receipt"),
        field="receipt.fixture_contract_sha256",
    )
    contract: dict[str, object] = {
        "format": receipt_format,
        "fixture_mode": fixture_mode,
        "core_version": core_version,
        "source_manifest_sha256": core_source_manifest_sha256,
        "seed": seed,
        "schema_epoch": schema_epoch,
        "schema_version": schema_version,
        "schema_manifest_sha256": schema_manifest_sha256,
        "expected": expected,
    }
    legacy_contract_sha256 = _canonical_json_sha256(contract)
    explicit_contract = {
        **contract,
        "receipt_schema_version": receipt_schema_version,
    }
    explicit_contract_sha256 = _canonical_json_sha256(explicit_contract)
    contract_includes_receipt_schema_version = (
        fixture_contract_sha256 == explicit_contract_sha256
    )
    if fixture_contract_sha256 not in {
        legacy_contract_sha256,
        explicit_contract_sha256,
    }:
        raise FixtureReceiptError(
            "core fixture contract digest does not match its canonical fields"
        )
    if expected_revision != 1:
        raise FixtureReceiptError("core benchmark fixture revision must be 1")

    return CoreFixtureAuthority(
        receipt_file_sha256=receipt_file.sha256,
        receipt_file_size_bytes=receipt_file.size_bytes,
        receipt_format=receipt_format,
        receipt_schema_version=receipt_schema_version,
        fixture_contract_sha256=fixture_contract_sha256,
        fixture_contract_includes_receipt_schema_version=(
            contract_includes_receipt_schema_version
        ),
        fixture_mode=fixture_mode,
        core_version=core_version,
        core_source_manifest_sha256=core_source_manifest_sha256,
        seed=seed,
        profile=profile,
        schema_epoch=schema_epoch,
        schema_version=schema_version,
        schema_manifest_sha256=schema_manifest_sha256,
        publication_count=publication_count,
        artifact_count=expected_counts["artifact_count"],
        acquisition_descriptor_count=expected_counts["acquisition_descriptor_count"],
        search_query=search_query,
        search_publication_count=search_publication_count,
        search_first_page_gids=first_gids,
        search_cursor_page_gids=cursor_gids,
        search_facet_counts=tuple(
            (family, tuple(sorted(counts.items())))
            for family, counts in facet_maps.items()
        ),
        database_sha256=observed_database.sha256,
        database_size_bytes=observed_database.size_bytes,
    )


@asynccontextmanager
async def _benchmark_client(
    database_path: Path,
) -> AsyncIterator[tuple[AsyncClient, int]]:
    with tempfile.TemporaryDirectory(prefix="h2hdb-opds-sqlite-scalability-") as root:
        benchmark_root = Path(root).resolve(strict=True)
        library_root = benchmark_root / "empty-library"
        coordination_root = benchmark_root / "coordination"
        library_root.mkdir()
        coordination_root.mkdir()
        (coordination_root / "publication.lock").touch()
        core = CoreConfig(
            database=DatabaseConfig(
                sql_type="sqlite",
                database=str(database_path.resolve(strict=True)),
                access_mode=DatabaseAccessMode.read_only,
            )
        )
        config = OPDSConfig(
            library_root=library_root,
            coordination_root=coordination_root,
            public_base_url=_PUBLIC_BASE_URL,
            core=core,
            title=_CATALOG_TITLE,
            default_page_size=_PAGE_LIMIT,
            maximum_page_size=_FACET_LIMIT,
        )
        application: FastAPI = create_app(config)
        lifespan_started = time.perf_counter_ns()
        async with application.router.lifespan_context(application):
            startup_ready_audit_ns = time.perf_counter_ns() - lifespan_started
            async with AsyncClient(
                transport=ASGITransport(app=application),
                base_url=_PUBLIC_BASE_URL,
            ) as client:
                yield client, startup_ready_audit_ns


_OperationMeasurer = Callable[
    [str, str], Awaitable[tuple[dict[str, object], _FetchedResponse]]
]


async def _run_operation_suite(
    measure: _OperationMeasurer,
    *,
    authority: CoreFixtureAuthority,
    retain_responses: bool,
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, _FetchedResponse],
    tuple[str, ...],
]:
    operations: dict[str, dict[str, object]] = {}
    responses: dict[str, _FetchedResponse] = {}
    order: list[str] = []

    async def record(name: str, url: str) -> _FetchedResponse:
        operation, response = await measure(name, url)
        operations[name] = operation
        if retain_responses:
            responses[name] = response
        order.append(name)
        return response

    discovery = await record(
        "discovery_first_page", f"/opds/v2/publications?limit={_PAGE_LIMIT}"
    )
    discovery_next = _next_link(_json_object(discovery.body))
    await record("discovery_cursor_page", discovery_next)

    search_parameters = urlencode(
        {"query": authority.search_query, "limit": _PAGE_LIMIT}
    )
    search = await record(
        "nonempty_search_first_page", f"/opds/v2/search?{search_parameters}"
    )
    search_next = _next_link(_json_object(search.body))
    await record("nonempty_search_cursor_page", search_next)

    facet_parameters = urlencode(
        {"query": authority.search_query, "limit": _FACET_LIMIT}
    )
    for family in _FACET_FAMILIES:
        await record(
            f"facet_{family}_first_page",
            f"/opds/v2/facets/{family}?{facet_parameters}",
        )

    exact_order = tuple(order)
    if exact_order != _OPERATION_ORDER:
        raise RuntimeError("SQL benchmark operation order drifted")
    return operations, responses, exact_order


async def _measure_timing_operation(
    client: AsyncClient,
    *,
    url: str,
    warm_repetitions: int,
) -> tuple[dict[str, object], _FetchedResponse]:
    gc.collect()
    started = time.perf_counter_ns()
    first = await _get_response(client, url)
    first_elapsed_ns = time.perf_counter_ns() - started
    warm_samples: list[int] = []
    for _iteration in range(warm_repetitions):
        started = time.perf_counter_ns()
        warmed = await _get_response(client, url)
        elapsed_ns = time.perf_counter_ns() - started
        if not _same_response(first, warmed):
            raise RuntimeError("warm SQL benchmark response changed exact HTTP fields")
        warm_samples.append(elapsed_ns)
    document = _json_object(first.body)
    return (
        {
            "request": url,
            "status_code": first.status_code,
            "deterministic_headers": dict(first.deterministic_headers),
            "body_bytes": len(first.body),
            "body_sha256": hashlib.sha256(first.body).hexdigest(),
            "first_sample_ns": first_elapsed_ns,
            "warm": _latency_summary(warm_samples),
            "response_shape": _response_shape(document),
        },
        first,
    )


async def _run_timing_pass(
    database_path: Path,
    authority: CoreFixtureAuthority,
    *,
    warm_repetitions: int,
) -> _TimingPass:
    if tracemalloc.is_tracing():
        raise RuntimeError("latency pass requires tracemalloc to be disabled")
    async with _benchmark_client(database_path) as (client, startup_ns):

        async def measure(
            _name: str, url: str
        ) -> tuple[dict[str, object], _FetchedResponse]:
            return await _measure_timing_operation(
                client, url=url, warm_repetitions=warm_repetitions
            )

        operations, responses, order = await _run_operation_suite(
            measure, authority=authority, retain_responses=True
        )
    return _TimingPass(
        startup_ready_audit_ns=startup_ns,
        operations=operations,
        responses=responses,
        operation_order=order,
    )


async def _run_memory_pass(
    database_path: Path,
    authority: CoreFixtureAuthority,
    expected_responses: Mapping[str, _FetchedResponse],
) -> tuple[dict[str, object], int, tuple[str, ...]]:
    async with _benchmark_client(database_path) as (client, startup_ns):
        gc.collect()
        tracemalloc.start()
        suite_peak_delta = 0
        try:
            suite_baseline, _baseline_peak = tracemalloc.get_traced_memory()

            async def measure(
                name: str, url: str
            ) -> tuple[dict[str, object], _FetchedResponse]:
                nonlocal suite_peak_delta
                gc.collect()
                operation_baseline, _operation_peak = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
                response = await _get_response(client, url)
                _current, peak = tracemalloc.get_traced_memory()
                suite_peak_delta = max(suite_peak_delta, max(0, peak - suite_baseline))
                expected = expected_responses.get(name)
                if expected is None or not _same_response(response, expected):
                    raise RuntimeError(
                        "memory-pass SQL response changed exact HTTP fields"
                    )
                return (
                    {
                        "request": url,
                        "python_traced_peak_delta_bytes": max(
                            0, peak - operation_baseline
                        ),
                    },
                    response,
                )

            operations, _responses, order = await _run_operation_suite(
                measure, authority=authority, retain_responses=False
            )
            current, _peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    return (
        {
            "python_traced_baseline_bytes": suite_baseline,
            "python_traced_current_delta_bytes": max(0, current - suite_baseline),
            "python_traced_peak_delta_bytes": suite_peak_delta,
            "operations": operations,
        },
        startup_ns,
        order,
    )


def _metadata(document: Mapping[str, object], *, operation: str) -> dict[str, object]:
    return _object(document.get("metadata"), field=f"{operation}.metadata")


def _publication_gids(
    document: Mapping[str, object], *, operation: str
) -> tuple[int, ...]:
    publications = _array(
        document.get("publications"), field=f"{operation}.publications"
    )
    gids: list[int] = []
    for position, raw_publication in enumerate(publications):
        publication = _object(
            raw_publication, field=f"{operation}.publications[{position}]"
        )
        metadata = _object(
            publication.get("metadata"),
            field=f"{operation}.publications[{position}].metadata",
        )
        identifier = _nonblank_text(
            metadata.get("identifier"),
            field=f"{operation}.publications[{position}].metadata.identifier",
        )
        prefix = "urn:h2h:gallery:"
        if not identifier.startswith(prefix):
            raise RuntimeError(f"{operation} emitted a non-gallery identifier")
        try:
            gid = int(identifier.removeprefix(prefix))
        except ValueError as error:
            raise RuntimeError(
                f"{operation} emitted a malformed gallery GID"
            ) from error
        if gid < 1:
            raise RuntimeError(f"{operation} emitted a nonpositive gallery GID")
        gids.append(gid)
    return tuple(gids)


def _facet_navigation_counts(
    document: Mapping[str, object], *, operation: str
) -> dict[str, int]:
    navigation = _array(document.get("navigation"), field=f"{operation}.navigation")
    if not navigation:
        raise RuntimeError(f"{operation} omitted the canonical clear-facet entry")
    counts: dict[str, int] = {}
    for position, raw_entry in enumerate(navigation):
        entry = _object(raw_entry, field=f"{operation}.navigation[{position}]")
        properties = entry.get("properties")
        if position == 0:
            if properties is not None:
                raise RuntimeError(f"{operation} clear-facet entry has a count")
            continue
        title = _nonblank_text(
            entry.get("title"), field=f"{operation}.navigation[{position}].title"
        )
        property_document = _object(
            properties, field=f"{operation}.navigation[{position}].properties"
        )
        count = _exact_int(
            property_document.get("numberOfItems"),
            field=f"{operation}.navigation[{position}].properties.numberOfItems",
            minimum=1,
        )
        if title in counts:
            raise RuntimeError(f"{operation} repeats facet label {title!r}")
        counts[title] = count
    return counts


def _embedded_facet_counts(
    document: Mapping[str, object], *, operation: str
) -> dict[str, dict[str, int]]:
    facets = _array(document.get("facets"), field=f"{operation}.facets")
    by_title: dict[str, dict[str, int]] = {}
    for position, raw_facet in enumerate(facets):
        facet = _object(raw_facet, field=f"{operation}.facets[{position}]")
        metadata = _object(
            facet.get("metadata"), field=f"{operation}.facets[{position}].metadata"
        )
        title = _nonblank_text(
            metadata.get("title"),
            field=f"{operation}.facets[{position}].metadata.title",
        )
        links = _array(
            facet.get("links"), field=f"{operation}.facets[{position}].links"
        )
        counts: dict[str, int] = {}
        for link_position, raw_link in enumerate(links):
            link = _object(
                raw_link,
                field=f"{operation}.facets[{position}].links[{link_position}]",
            )
            properties = link.get("properties")
            if properties is None:
                continue
            value = _nonblank_text(
                link.get("title"),
                field=f"{operation}.facets[{position}].links[{link_position}].title",
            )
            property_document = _object(
                properties,
                field=(
                    f"{operation}.facets[{position}].links[{link_position}].properties"
                ),
            )
            count = _exact_int(
                property_document.get("numberOfItems"),
                field=(
                    f"{operation}.facets[{position}].links[{link_position}]"
                    ".properties.numberOfItems"
                ),
                minimum=1,
            )
            if value in counts:
                raise RuntimeError(f"{operation} repeats facet label {value!r}")
            counts[value] = count
        if title in by_title:
            raise RuntimeError(f"{operation} repeats facet group {title!r}")
        by_title[title] = counts
    return {
        family: by_title.get(_FACET_TITLES[family], {}) for family in _FACET_FAMILIES
    }


def _validate_metadata_count(
    document: Mapping[str, object],
    *,
    operation: str,
    expected_total: int,
    total_required: bool = True,
) -> None:
    metadata = _metadata(document, operation=operation)
    observed_total = metadata.get("numberOfItems")
    if (total_required and observed_total != expected_total) or (
        observed_total is not None and observed_total != expected_total
    ):
        raise RuntimeError(f"{operation} total cardinality disagrees with receipt")
    if metadata.get("itemsPerPage") != _PAGE_LIMIT:
        raise RuntimeError(f"{operation} page limit is not {_PAGE_LIMIT}")


def _validate_revision_links(
    document: Mapping[str, object], *, operation: str, revision: int
) -> None:
    links = _array(document.get("links"), field=f"{operation}.links")
    if not links:
        raise RuntimeError(f"{operation} has no links")
    revision_pinned_count = 0
    for position, raw_link in enumerate(links):
        link = _object(raw_link, field=f"{operation}.links[{position}]")
        href = _nonblank_text(
            link.get("href"), field=f"{operation}.links[{position}].href"
        )
        parsed = urlsplit(href)
        if parsed.scheme != "http" or parsed.netloc != "benchmark.invalid":
            raise RuntimeError(f"{operation} link escaped the benchmark origin")
        if link.get("rel") == "http://opds-spec.org/auth/document":
            continue
        if f"revision={revision}" not in parsed.query:
            raise RuntimeError(f"{operation} link is not revision pinned")
        revision_pinned_count += 1
    if revision_pinned_count < 2:
        raise RuntimeError(f"{operation} lacks canonical revision-pinned links")


def _validate_http_oracle(
    responses: Mapping[str, _FetchedResponse], authority: CoreFixtureAuthority
) -> dict[str, object]:
    documents = {
        name: _json_object(response.body) for name, response in responses.items()
    }
    if tuple(documents) != _OPERATION_ORDER:
        raise RuntimeError("timing responses do not cover the exact operation order")

    discovery_first = documents["discovery_first_page"]
    discovery_cursor = documents["discovery_cursor_page"]
    _validate_metadata_count(
        discovery_first,
        operation="discovery_first_page",
        expected_total=authority.publication_count,
    )
    _validate_metadata_count(
        discovery_cursor,
        operation="discovery_cursor_page",
        expected_total=authority.publication_count,
    )
    discovery_first_gids = _publication_gids(
        discovery_first, operation="discovery_first_page"
    )
    discovery_cursor_gids = _publication_gids(
        discovery_cursor, operation="discovery_cursor_page"
    )
    if (
        len(discovery_first_gids) != _PAGE_LIMIT
        or len(discovery_cursor_gids) != _PAGE_LIMIT
    ):
        raise RuntimeError("discovery fixture did not produce two full bounded pages")
    if set(discovery_first_gids) & set(discovery_cursor_gids):
        raise RuntimeError("discovery cursor page overlaps its first page")

    search_first = documents["nonempty_search_first_page"]
    search_cursor = documents["nonempty_search_cursor_page"]
    _validate_metadata_count(
        search_first,
        operation="nonempty_search_first_page",
        expected_total=authority.search_publication_count,
        total_required=False,
    )
    _validate_metadata_count(
        search_cursor,
        operation="nonempty_search_cursor_page",
        expected_total=authority.search_publication_count,
        total_required=False,
    )
    if (
        _publication_gids(search_first, operation="nonempty_search_first_page")
        != authority.search_first_page_gids
    ):
        raise RuntimeError("search first-page GIDs disagree with the core receipt")
    if (
        _publication_gids(search_cursor, operation="nonempty_search_cursor_page")
        != authority.search_cursor_page_gids
    ):
        raise RuntimeError("search cursor-page GIDs disagree with the core receipt")

    expected_facets = {
        family: {name: count for name, count in counts.items() if count > 0}
        for family, counts in authority.facet_counts().items()
    }
    embedded = _embedded_facet_counts(
        search_first, operation="nonempty_search_first_page"
    )
    if embedded != expected_facets:
        raise RuntimeError("embedded search facets disagree with the core receipt")
    for family in _FACET_FAMILIES:
        operation = f"facet_{family}_first_page"
        document = documents[operation]
        metadata = _metadata(document, operation=operation)
        if metadata.get("itemsPerPage") != _FACET_LIMIT:
            raise RuntimeError(f"{operation} facet limit is not {_FACET_LIMIT}")
        if (
            _facet_navigation_counts(document, operation=operation)
            != expected_facets[family]
        ):
            raise RuntimeError(f"{operation} counts disagree with the core receipt")

    for operation, document in documents.items():
        _validate_revision_links(document, operation=operation, revision=1)
    return {
        "revision": 1,
        "publication_count": authority.publication_count,
        "search_query": authority.search_query,
        "search_publication_count": authority.search_publication_count,
        "search_first_page_gids": list(authority.search_first_page_gids),
        "search_cursor_page_gids": list(authority.search_cursor_page_gids),
        "search_facet_counts": expected_facets,
        "discovery_first_page_gids": list(discovery_first_gids),
        "discovery_cursor_page_gids": list(discovery_cursor_gids),
    }


def _authority_document(authority: CoreFixtureAuthority) -> dict[str, object]:
    return {
        "receipt": {
            "format": authority.receipt_format,
            "schema_version": authority.receipt_schema_version,
            "file_sha256": authority.receipt_file_sha256,
            "file_size_bytes": authority.receipt_file_size_bytes,
            "fixture_contract_sha256": authority.fixture_contract_sha256,
            "fixture_contract_includes_receipt_schema_version": (
                authority.fixture_contract_includes_receipt_schema_version
            ),
        },
        "fixture_mode": authority.fixture_mode,
        "core_version": authority.core_version,
        "core_source_manifest_sha256": authority.core_source_manifest_sha256,
        "seed": authority.seed,
        "profile": authority.profile,
        "schema": {
            "epoch": authority.schema_epoch,
            "schema_version": authority.schema_version,
            "state": "READY",
            "manifest_sha256": authority.schema_manifest_sha256,
            "full_ready_audit_passed_by_core_fixture": True,
            "full_ready_audit_passed_by_opds_startup": True,
        },
        "database": {
            "sha256": authority.database_sha256,
            "size_bytes": authority.database_size_bytes,
            "unchanged_after_requests": True,
        },
        "cardinalities": {
            "publication_count": authority.publication_count,
            "artifact_count": authority.artifact_count,
            "acquisition_descriptor_count": authority.acquisition_descriptor_count,
            "search_publication_count": authority.search_publication_count,
        },
        "creates_or_reads_cbz_or_artwork_bytes": False,
    }


async def run_sqlite_benchmark(
    database_path: Path,
    fixture_receipt_path: Path,
    *,
    warm_repetitions: int = 5,
) -> dict[str, object]:
    """Run the SQL-backed HTTP benchmark and return its auditable receipt."""

    benchmark_entry_max_rss_bytes = _process_max_rss_bytes()
    if type(warm_repetitions) is not int or not 1 <= warm_repetitions <= 20:
        raise ValueError("warm_repetitions must be in 1..20")
    if tracemalloc.is_tracing():
        raise RuntimeError("benchmark requires tracemalloc to be disabled on entry")

    input_validation_started = time.perf_counter_ns()
    authority = load_core_fixture(database_path, fixture_receipt_path)
    input_validation_ns = time.perf_counter_ns() - input_validation_started
    runtime_core_version = version("h2hdb")

    provenance_started = time.perf_counter_ns()
    source_provenance = collect_source_provenance()
    source_provenance_ns = time.perf_counter_ns() - provenance_started
    source_versions = {
        component.name: component.project_version
        for component in source_provenance.components
    }
    imported_core_source_version = source_versions.get("h2hdb-import")
    if (
        imported_core_source_version is not None
        and imported_core_source_version != authority.core_version
    ):
        raise FixtureReceiptError(
            "imported h2hdb source pyproject version disagrees with the fixture "
            f"receipt: {imported_core_source_version!r} != {authority.core_version!r}"
        )

    timing = await _run_timing_pass(
        database_path,
        authority,
        warm_repetitions=warm_repetitions,
    )
    oracle = _validate_http_oracle(timing.responses, authority)
    memory, memory_startup_ns, memory_order = await _run_memory_pass(
        database_path, authority, timing.responses
    )
    if memory_order != timing.operation_order:
        raise RuntimeError("memory pass changed SQL benchmark operation order")

    observed_after = _digest_regular_file(database_path, field="SQLite database")
    if observed_after != _FileDigest(
        sha256=authority.database_sha256,
        size_bytes=authority.database_size_bytes,
    ):
        raise RuntimeError("SQLite database changed during the read-only benchmark")
    receipt_after, receipt_file_after = _read_receipt(fixture_receipt_path)
    del receipt_after
    if receipt_file_after != _FileDigest(
        sha256=authority.receipt_file_sha256,
        size_bytes=authority.receipt_file_size_bytes,
    ):
        raise RuntimeError("core fixture receipt changed during the benchmark")

    fixture_document = _authority_document(authority)
    exact_results: dict[str, object] = {
        name: {
            "request": operation["request"],
            "status_code": operation["status_code"],
            "deterministic_headers": operation["deterministic_headers"],
            "body_bytes": operation["body_bytes"],
            "body_sha256": operation["body_sha256"],
            "response_shape": operation["response_shape"],
        }
        for name, operation in timing.operations.items()
    }
    result_manifest_input: dict[str, object] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "fixture_contract_sha256": authority.fixture_contract_sha256,
        "database_sha256": authority.database_sha256,
        "schema_manifest_sha256": authority.schema_manifest_sha256,
        "source_manifest_sha256": source_provenance.manifest_sha256,
        "operation_order": list(timing.operation_order),
        "exact_http_results": exact_results,
        "validated_oracle": oracle,
    }
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark": "h2hdb-opds-sqlite-scalability",
        "mode": {
            "name": "sql-backed-public-http",
            "sql_backed": True,
            "protocol": "OPDS 2.0",
            "catalog_open_boundary": "h2hdb.open_database(read_only=True)",
            "reader_injected": False,
            "core_internal_api_used": False,
            "direct_sql_used": False,
            "network_transport_included": False,
            "cbz_or_artwork_bytes_read": False,
            "request_timing_includes": [
                "in-process ASGI routing",
                "publication coordination shared lock",
                "public CatalogService and h2hdb CatalogReader SQL discovery",
                "OPDS 2 document construction and JSON serialization",
            ],
            "request_timing_excludes": [
                "application startup and full READY audit",
                "fixture receipt and database hashing",
                "source provenance hashing",
                "Python allocation tracing",
                "network transport",
                "CBZ, artwork, acquisition, and media byte reads",
            ],
            "timing_instrumentation": "perf_counter_ns with tracemalloc disabled",
        },
        "profile": {
            "name": authority.profile,
            "publication_count": authority.publication_count,
            "page_limit": _PAGE_LIMIT,
            "facet_limit": _FACET_LIMIT,
            "warm_repetitions": warm_repetitions,
            "seed": authority.seed,
        },
        "operation_order": list(timing.operation_order),
        "source_provenance": _source_provenance_document(source_provenance),
        "fixture": fixture_document,
        "setup": {
            "input_receipt_and_database_validation_ns": input_validation_ns,
            "source_manifest_build_ns": source_provenance_ns,
            "timing_pass_startup_and_full_ready_audit_ns": (
                timing.startup_ready_audit_ns
            ),
            "memory_pass_startup_and_full_ready_audit_ns": memory_startup_ns,
            "startup_timing_included_in_request_samples": False,
            "timing_tracemalloc_enabled": False,
        },
        "validated_oracle": oracle,
        "operations": timing.operations,
        "memory": {
            "request_fresh_app_pass": memory,
            "process_lifetime_max_rss_at_benchmark_entry_bytes": (
                benchmark_entry_max_rss_bytes
            ),
            "process_lifetime_max_rss_bytes": _process_max_rss_bytes(),
            "process_lifetime_max_rss_scope": (
                "RUSAGE_SELF lifetime high-water mark; the entry sample includes "
                "module import, while the final sample also includes input "
                "validation, two startup audits, timing requests, and the "
                "request-memory pass"
            ),
        },
        "determinism": {
            "fresh_memory_pass_matches_timing_pass": True,
            "database_unchanged_after_requests": True,
            "fixture_receipt_unchanged_after_requests": True,
            "validated_http_fields": [
                "status_code",
                "response_body",
                "content-length",
                "content-type",
            ],
            "result_manifest_sha256": _canonical_json_sha256(result_manifest_input),
            "result_manifest_contains_absolute_paths": False,
        },
        "comparability": {
            "imported_core_source_project_version": imported_core_source_version,
            "imported_core_source_version_matches_fixture_receipt": (
                imported_core_source_version is None
                or imported_core_source_version == authority.core_version
            ),
            "distribution_metadata_is_diagnostic_only": True,
            "diagnostic_core_distribution_version_matches_fixture_receipt": (
                runtime_core_version == authority.core_version
            ),
            "schema_compatibility_authority": (
                "successful public open_database full READY audit against the exact "
                "receipt-bound database and schema manifest"
            ),
        },
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "h2hdb_distribution_metadata_version_diagnostic": runtime_core_version,
            "h2hdb_opds_distribution_metadata_version_diagnostic": version(
                "h2hdb-opds"
            ),
            "timer": "perf_counter_ns",
        },
    }


def _write_new_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    target = path.resolve()
    if not target.parent.is_dir():
        raise FileNotFoundError(
            f"output receipt parent does not exist: {target.parent}"
        )
    payload = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with target.open("x", encoding="utf-8") as destination:
        destination.write(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, required=True, help="existing core READY SQLite DB"
    )
    parser.add_argument(
        "--fixture-receipt",
        type=Path,
        required=True,
        help="matching core scalability fixture receipt",
    )
    parser.add_argument(
        "--output-receipt",
        type=Path,
        required=True,
        help="new OPDS SQL benchmark receipt path",
    )
    parser.add_argument(
        "--warm-repetitions", type=int, default=5, help="bounded repetitions in 1..20"
    )
    parser.add_argument(
        "--compact", action="store_true", help="emit compact JSON on stdout"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    output_receipt = arguments.output_receipt.resolve()
    database = arguments.database.resolve(strict=True)
    fixture_receipt = arguments.fixture_receipt.resolve(strict=True)
    if output_receipt in {database, fixture_receipt}:
        raise SystemExit("output receipt must differ from both inputs")
    if output_receipt.exists() or output_receipt.is_symlink():
        raise FileExistsError(f"output receipt already exists: {output_receipt}")
    receipt = asyncio.run(
        run_sqlite_benchmark(
            database,
            fixture_receipt,
            warm_repetitions=arguments.warm_repetitions,
        )
    )
    _write_new_receipt(output_receipt, receipt)
    json.dump(
        receipt,
        sys.stdout,
        sort_keys=True,
        indent=None if arguments.compact else 2,
        separators=(",", ":") if arguments.compact else None,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
