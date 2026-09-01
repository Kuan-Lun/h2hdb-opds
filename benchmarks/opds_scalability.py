"""Deterministic OPDS scalability benchmark over a synthetic catalog reader.

This benchmark deliberately measures the OPDS application boundary without SQL.
The synthetic reader builds immutable catalog values and indexes before request
timing begins, then serves bounded pages through the public ``CatalogReader``
protocol.  A SQL-backed profile requires a core-owned READY database fixture and
is intentionally not approximated with OPDS-owned tables or direct SQL writes.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
import tracemalloc
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Final, Protocol, cast
from urllib.parse import urlencode

import h2hdb as h2hdb_package
from h2hdb import (
    CatalogArtifact,
    CatalogContributor,
    CatalogCursorError,
    CatalogDiscoveryBundle,
    CatalogDiscoveryCursor,
    CatalogDiscoveryPage,
    CatalogDiscoveryQuery,
    CatalogFacetCursor,
    CatalogFacetKind,
    CatalogFacetPage,
    CatalogFacetValue,
    CatalogImageResource,
    CatalogPublication,
    CatalogPublicationPresentation,
    CatalogRecentOrder,
    CatalogRecentWindow,
    CatalogRevision,
    CatalogRevisionNotFoundError,
    CatalogSubject,
    StorageObjectDescriptor,
    StorageObjectKey,
    catalog_search_field_lexemes,
)
from httpx import ASGITransport, AsyncClient

import h2hdb_opds as h2hdb_opds_package
from h2hdb_opds import OPDSConfig, create_app
from h2hdb_opds.discovery import discovery_query
from h2hdb_opds.publication import OPDS_OPEN_ACCESS_REL
from h2hdb_opds.serialization import OPDS_FEED_MEDIA_TYPE

BENCHMARK_SCHEMA_VERSION: Final = 3
DEFAULT_SEED: Final = 20_260_902
SEARCH_TEXT: Final = "benchmark needle"
_PAGE_LIMIT: Final = 128
_REVISION: Final = 1
_REVISION_PUBLISHED_AT: Final = datetime(2026, 1, 2, tzinfo=UTC)
_PUBLIC_BASE_URL: Final = "http://benchmark.invalid"
_CATALOG_TITLE: Final = "H2HDB OPDS synthetic scalability benchmark"
_ARTIFACT_PAYLOAD_SHA256: Final = sha256(b"x").hexdigest()
_EMPTY_QUERY: Final = CatalogDiscoveryQuery()
_SEARCH_QUERY: Final = discovery_query(
    search=SEARCH_TEXT,
    required_search_field="query",
    language=None,
    tag=None,
    tag_namespace=None,
    contributor=None,
    role=None,
)
_LANGUAGES: Final = ("en", "ja", "zh-Hant", "ko", "fr", "de", "es", "it")
_CONTRIBUTOR_ROLES: Final = (
    "artist",
    "author",
    "cosplayer",
    "group",
    "illustrator",
    "uploader",
)
_FACET_KINDS: Final = (
    CatalogFacetKind.LANGUAGE,
    CatalogFacetKind.SUBJECT,
    CatalogFacetKind.CONTRIBUTOR,
)
_OPERATION_ORDER: Final = (
    "discovery_first_page",
    "facet_language_first_page",
    "facet_subject_first_page",
    "facet_contributor_first_page",
    "nonempty_search_first_page",
    "discovery_cursor_page",
    "facet_subject_cursor_page",
)
_DETERMINISTIC_RESPONSE_HEADERS: Final = ("content-length", "content-type")
_SOURCE_MANIFEST_SCHEMA: Final = "h2hdb-opds-source-manifest-v2"
_SOURCE_HASH_CHUNK_BYTES: Final = 1024 * 1024

# These values are an intentional benchmark-workload contract. Updating either
# constant requires reviewing the canonical authority or serialized OPDS change.
SMOKE_EXPECTED_MANIFEST_SHA256: Final = (
    "6bfceaef01795b082a04f644a86bcff3e7e84f33b10730fef789422e38f73230"
)
SMOKE_EXPECTED_BODY_SHA256: Final[dict[str, str]] = {
    "discovery_first_page": (
        "5fc555838f9a62126f9027edf8a81605551511be61211ff615f863d3d6519662"
    ),
    "facet_language_first_page": (
        "d1d5b6093d2ebd5f8176d40f069fa95a3a36f84beea7b628f25b232085c7a993"
    ),
    "facet_subject_first_page": (
        "18d8bf157dc83eac40a33ff0089aea8ef820abb72c873e2b650ad50f5d549bbb"
    ),
    "facet_contributor_first_page": (
        "58e4152ec52364814c59a1a84e133c5ade522be9b615a0eb99c50580190795cb"
    ),
    "nonempty_search_first_page": (
        "4122d324fd5c26781fb4cd6d424d0bce211991a6419eeba0f7e3142a5825b83b"
    ),
    "discovery_cursor_page": (
        "95d96bc96118672072016ea84b01f8119ae5bf1d7122fded4c4b33e11e9711ce"
    ),
    "facet_subject_cursor_page": (
        "a520d24c02bd2c6605fbd33c6041213a17d40cb948ea0974468781bd76955499"
    ),
}


@dataclass(frozen=True, slots=True)
class BenchmarkProfile:
    """One fixed-size reproducible benchmark workload."""

    name: str
    publication_count: int
    warm_repetitions: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("profile name must not be blank")
        if self.publication_count < _PAGE_LIMIT * 2:
            raise ValueError("profile must contain at least two full discovery pages")
        if self.warm_repetitions < 1:
            raise ValueError("warm repetitions must be positive")


SMOKE_PROFILE: Final = BenchmarkProfile(
    name="smoke",
    publication_count=384,
    warm_repetitions=2,
)
TEN_THOUSAND_PROFILE: Final = BenchmarkProfile(
    name="10k",
    publication_count=10_000,
    warm_repetitions=5,
)
PROFILES: Final = {
    SMOKE_PROFILE.name: SMOKE_PROFILE,
    TEN_THOUSAND_PROFILE.name: TEN_THOUSAND_PROFILE,
}


@dataclass(frozen=True, slots=True)
class SyntheticFixture:
    """The immutable values and canonical workload receipt fed to the reader."""

    publications: tuple[CatalogPublication, ...]
    manifest_sha256: str
    canonical_receipt_bytes: int
    search_match_count: int
    facet_value_counts: tuple[tuple[CatalogFacetKind, int], ...]


@dataclass(frozen=True, slots=True)
class _FetchedResponse:
    status_code: int
    deterministic_headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class _TimedResponse:
    elapsed_ns: int
    response: _FetchedResponse


@dataclass(frozen=True, slots=True)
class _SourceFileEvidence:
    logical_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _SourceComponentEvidence:
    name: str
    located: bool
    project_version: str | None
    files: tuple[_SourceFileEvidence, ...]


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Path-independent source identity for one benchmark invocation."""

    manifest_sha256: str
    components: tuple[_SourceComponentEvidence, ...]
    git_commit: str | None
    git_dirty: bool | None


class _WritableDigest(Protocol):
    def update(self, value: bytes) -> None: ...


def _framed_update(digest: _WritableDigest, value: str) -> int:
    encoded = value.encode("utf-8")
    framed = len(encoded).to_bytes(8, "big") + encoded
    digest.update(framed)
    return len(framed)


def _source_file_evidence(
    *,
    logical_path: str,
    path: Path,
) -> _SourceFileEvidence:
    before = path.stat()
    digest = sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(_SOURCE_HASH_CHUNK_BYTES):
            digest.update(chunk)
            size_bytes += len(chunk)
    after = path.stat()
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity or size_bytes != after.st_size:
        raise RuntimeError(f"source changed while hashing {logical_path!r}")
    return _SourceFileEvidence(
        logical_path=logical_path,
        size_bytes=size_bytes,
        sha256=digest.hexdigest(),
    )


def _pyproject_version(path: Path) -> str:
    with path.open("rb") as source:
        document = tomllib.load(source)
    project = document.get("project")
    version_value = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version_value, str) or not version_value:
        raise RuntimeError(f"source pyproject has no project.version: {path}")
    return version_value


def _opds_source_component(repository_root: Path) -> _SourceComponentEvidence:
    raw_init = getattr(h2hdb_opds_package, "__file__", None)
    if not isinstance(raw_init, str):
        return _SourceComponentEvidence(
            name="h2hdb-opds", located=False, project_version=None, files=()
        )
    try:
        package_root = Path(raw_init).resolve(strict=True).parent
    except OSError, RuntimeError:
        return _SourceComponentEvidence(
            name="h2hdb-opds", located=False, project_version=None, files=()
        )
    imported_repository_root = (
        package_root.parent.parent if package_root.parent.name == "src" else None
    )
    imported_pyproject = (
        imported_repository_root / "pyproject.toml"
        if imported_repository_root is not None
        else None
    )
    if imported_pyproject is not None and not imported_pyproject.is_file():
        imported_pyproject = None

    file_paths: dict[str, Path] = {
        "h2hdb-opds/benchmarks/"
        + path.relative_to(repository_root / "benchmarks").as_posix(): path
        for path in sorted((repository_root / "benchmarks").rglob("*.py"))
        if path.is_file()
    }
    for path in sorted(package_root.rglob("*.py")):
        if path.is_file():
            file_paths[
                "h2hdb-opds/src/h2hdb_opds/" + path.relative_to(package_root).as_posix()
            ] = path
    benchmark_pyproject = repository_root / "pyproject.toml"
    if imported_pyproject is None or imported_pyproject != benchmark_pyproject:
        file_paths["h2hdb-opds/benchmark-pyproject.toml"] = benchmark_pyproject
    if imported_pyproject is not None:
        file_paths["h2hdb-opds/pyproject.toml"] = imported_pyproject

    files = tuple(
        _source_file_evidence(logical_path=logical_path, path=path)
        for logical_path, path in sorted(file_paths.items())
    )
    return _SourceComponentEvidence(
        name="h2hdb-opds",
        located=True,
        project_version=(
            None
            if imported_pyproject is None
            else _pyproject_version(imported_pyproject)
        ),
        files=tuple(sorted(files, key=lambda item: item.logical_path)),
    )


def _h2hdb_source_component() -> _SourceComponentEvidence:
    raw_init = getattr(h2hdb_package, "__file__", None)
    if not isinstance(raw_init, str):
        return _SourceComponentEvidence(
            name="h2hdb-import", located=False, project_version=None, files=()
        )
    try:
        package_init = Path(raw_init).resolve(strict=True)
    except OSError, RuntimeError:
        return _SourceComponentEvidence(
            name="h2hdb-import", located=False, project_version=None, files=()
        )
    package_root = package_init.parent
    repository_root = (
        package_root.parent.parent if package_root.parent.name == "src" else None
    )
    pyproject_path = (
        repository_root / "pyproject.toml" if repository_root is not None else None
    )
    if pyproject_path is not None and not pyproject_path.is_file():
        pyproject_path = None
    source_paths = tuple(
        sorted(path for path in package_root.rglob("*.py") if path.is_file())
    )
    if not source_paths:
        return _SourceComponentEvidence(
            name="h2hdb-import", located=False, project_version=None, files=()
        )
    package_files = tuple(
        _source_file_evidence(
            logical_path="h2hdb/" + path.relative_to(package_root).as_posix(),
            path=path,
        )
        for path in source_paths
    )
    pyproject_files = (
        ()
        if pyproject_path is None
        else (
            _source_file_evidence(
                logical_path="h2hdb/pyproject.toml",
                path=pyproject_path,
            ),
        )
    )
    return _SourceComponentEvidence(
        name="h2hdb-import",
        located=True,
        project_version=(
            None if pyproject_path is None else _pyproject_version(pyproject_path)
        ),
        files=tuple(
            sorted(
                (*package_files, *pyproject_files), key=lambda item: item.logical_path
            )
        ),
    )


def _source_manifest_sha256(
    components: tuple[_SourceComponentEvidence, ...],
) -> str:
    digest = sha256((_SOURCE_MANIFEST_SCHEMA + "\0").encode("ascii"))
    ordered_components = tuple(sorted(components, key=lambda item: item.name))
    _framed_update(digest, str(len(ordered_components)))
    for component in ordered_components:
        _framed_update(digest, "component")
        _framed_update(digest, component.name)
        _framed_update(digest, "located" if component.located else "unlocated")
        _framed_update(digest, component.project_version or "unavailable")
        _framed_update(digest, str(len(component.files)))
        for source in component.files:
            _framed_update(digest, "file")
            _framed_update(digest, source.logical_path)
            _framed_update(digest, str(source.size_bytes))
            _framed_update(digest, source.sha256)
    return digest.hexdigest()


def _git_output(repository_root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(repository_root), *arguments),
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=2,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip("\n")


def collect_source_provenance() -> SourceProvenance:
    """Collect the exact local Python sources without hashing host paths."""

    repository_root = Path(__file__).resolve(strict=True).parents[1]
    components = tuple(
        sorted(
            (
                _opds_source_component(repository_root),
                _h2hdb_source_component(),
            ),
            key=lambda item: item.name,
        )
    )
    commit = _git_output(repository_root, "rev-parse", "--verify", "HEAD")
    status = _git_output(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    return SourceProvenance(
        manifest_sha256=_source_manifest_sha256(components),
        components=components,
        git_commit=commit or None,
        git_dirty=None if status is None else bool(status),
    )


def _source_provenance_document(provenance: SourceProvenance) -> dict[str, object]:
    return {
        "manifest_schema": _SOURCE_MANIFEST_SCHEMA,
        "manifest_sha256": provenance.manifest_sha256,
        "canonical_paths_are_relative": True,
        "components": [
            {
                "name": component.name,
                "located": component.located,
                "project_version": component.project_version,
                "file_count": len(component.files),
                "files": [
                    {
                        "logical_path": source.logical_path,
                        "size_bytes": source.size_bytes,
                        "sha256": source.sha256,
                    }
                    for source in component.files
                ],
            }
            for component in provenance.components
        ],
        "git": {
            "commit": provenance.git_commit,
            "dirty": provenance.git_dirty,
            "canonical": False,
        },
    }


class _CanonicalReceipt:
    """Unambiguous typed framing for fixture and workload authority."""

    def __init__(self) -> None:
        self._digest = sha256(b"h2hdb-opds-synthetic-catalog-v2\0")
        self.byte_count = 0

    def _field(self, label: str, kind: str, value: str) -> None:
        self.byte_count += _framed_update(self._digest, label)
        self.byte_count += _framed_update(self._digest, kind)
        self.byte_count += _framed_update(self._digest, value)

    def text(self, label: str, value: str) -> None:
        self._field(label, "text", value)

    def optional_text(self, label: str, value: str | None) -> None:
        if value is None:
            self._field(label, "none", "")
        else:
            self.text(label, value)

    def integer(self, label: str, value: int) -> None:
        self._field(label, "integer", str(value))

    def boolean(self, label: str, value: bool) -> None:
        self._field(label, "boolean", "true" if value else "false")

    def timestamp(self, label: str, value: datetime) -> None:
        canonical = value.astimezone(UTC).isoformat(timespec="microseconds")
        self._field(label, "utc-datetime", canonical)

    def binary(self, label: str, value: bytes) -> None:
        self._field(label, "bytes-hex", value.hex())

    def digest(self) -> str:
        return self._digest.hexdigest()


def _update_query_receipt(
    receipt: _CanonicalReceipt,
    prefix: str,
    query: CatalogDiscoveryQuery,
) -> None:
    receipt.optional_text(f"{prefix}.search", query.search)
    receipt.optional_text(f"{prefix}.language", query.language)
    receipt.optional_text(
        f"{prefix}.subject.namespace",
        None if query.subject is None else query.subject.namespace,
    )
    receipt.optional_text(
        f"{prefix}.subject.value",
        None if query.subject is None else query.subject.value,
    )
    receipt.optional_text(
        f"{prefix}.contributor.name",
        None if query.contributor is None else query.contributor.name,
    )
    receipt.optional_text(
        f"{prefix}.contributor.role",
        None if query.contributor is None else query.contributor.role,
    )
    receipt.integer(f"{prefix}.search_lexemes.count", len(query.search_lexemes))
    for position, lexeme in enumerate(query.search_lexemes):
        receipt.binary(f"{prefix}.search_lexemes.{position}", lexeme)


def _update_storage_object_receipt(
    receipt: _CanonicalReceipt,
    prefix: str,
    descriptor: StorageObjectDescriptor,
) -> None:
    receipt.text(f"{prefix}.key.codec", descriptor.key.codec)
    receipt.integer(f"{prefix}.key.segments.count", len(descriptor.key.segments))
    for position, segment in enumerate(descriptor.key.segments):
        receipt.text(f"{prefix}.key.segments.{position}", segment)
    receipt.integer(f"{prefix}.size_bytes", descriptor.size_bytes)
    receipt.text(f"{prefix}.sha256", descriptor.sha256)
    receipt.timestamp(f"{prefix}.modified_at", descriptor.modified_at)


def _update_image_receipt(
    receipt: _CanonicalReceipt,
    prefix: str,
    image: CatalogImageResource | None,
) -> None:
    receipt.boolean(f"{prefix}.present", image is not None)
    if image is None:
        return
    _update_storage_object_receipt(
        receipt, f"{prefix}.storage_object", image.storage_object
    )
    receipt.integer(f"{prefix}.extent.offset", image.extent.offset)
    receipt.integer(f"{prefix}.extent.length", image.extent.length)
    receipt.text(f"{prefix}.media_type", image.media_type)
    receipt.text(f"{prefix}.sha256", image.sha256)
    receipt.integer(f"{prefix}.width", image.width)
    receipt.integer(f"{prefix}.height", image.height)


def _update_publication_receipt(
    receipt: _CanonicalReceipt,
    position: int,
    publication: CatalogPublication,
) -> None:
    prefix = f"publications.{position}"
    receipt.text(f"{prefix}.publication_id", publication.publication_id)
    receipt.integer(f"{prefix}.gid", publication.gid)
    receipt.text(f"{prefix}.title", publication.title)
    receipt.text(f"{prefix}.source_title", publication.source_title)
    receipt.text(f"{prefix}.sort_title", publication.sort_title)
    receipt.text(f"{prefix}.summary", publication.summary)
    receipt.text(f"{prefix}.language", publication.language)
    receipt.timestamp(f"{prefix}.published_at", publication.published_at)
    receipt.timestamp(f"{prefix}.modified_at", publication.modified_at)
    receipt.timestamp(f"{prefix}.downloaded_at", publication.downloaded_at)
    receipt.text(f"{prefix}.source_gallery_name", publication.source_gallery_name)
    receipt.integer(f"{prefix}.page_count", publication.page_count)
    _update_image_receipt(receipt, f"{prefix}.cover", publication.cover)
    _update_image_receipt(receipt, f"{prefix}.thumbnail", publication.thumbnail)
    receipt.boolean(f"{prefix}.redownload_required", publication.redownload_required)
    receipt.optional_text(f"{prefix}.content_sha256", publication.content_sha256)

    receipt.integer(f"{prefix}.contributors.count", len(publication.contributors))
    for index, contributor in enumerate(publication.contributors):
        receipt.text(f"{prefix}.contributors.{index}.name", contributor.name)
        receipt.text(f"{prefix}.contributors.{index}.role", contributor.role)

    receipt.integer(f"{prefix}.subjects.count", len(publication.subjects))
    for index, subject in enumerate(publication.subjects):
        receipt.text(f"{prefix}.subjects.{index}.name", subject.name)
        receipt.optional_text(f"{prefix}.subjects.{index}.scheme", subject.scheme)
        receipt.optional_text(f"{prefix}.subjects.{index}.code", subject.code)

    receipt.integer(f"{prefix}.artifacts.count", len(publication.artifacts))
    for index, artifact in enumerate(publication.artifacts):
        artifact_prefix = f"{prefix}.artifacts.{index}"
        receipt.text(f"{artifact_prefix}.artifact_id", artifact.artifact_id)
        receipt.text(f"{artifact_prefix}.name", artifact.name)
        receipt.text(f"{artifact_prefix}.media_type", artifact.media_type)
        _update_storage_object_receipt(
            receipt,
            f"{artifact_prefix}.storage_object",
            artifact.storage_object,
        )

    searchable_fields = (
        publication.title,
        publication.source_title,
        *(item.name for item in publication.contributors),
        *(item.name for item in publication.subjects),
    )
    receipt.integer(f"{prefix}.search_fields.count", len(searchable_fields))
    for field_position, field in enumerate(searchable_fields):
        lexemes = catalog_search_field_lexemes(field)
        receipt.integer(
            f"{prefix}.search_fields.{field_position}.lexemes.count",
            len(lexemes),
        )
        for lexeme_position, lexeme in enumerate(lexemes):
            receipt.binary(
                f"{prefix}.search_fields.{field_position}.lexemes.{lexeme_position}",
                lexeme,
            )


def _workload_receipt(
    *,
    profile: BenchmarkProfile,
    seed: int,
) -> _CanonicalReceipt:
    receipt = _CanonicalReceipt()
    receipt.integer("benchmark_schema_version", BENCHMARK_SCHEMA_VERSION)
    receipt.text("profile.name", profile.name)
    receipt.integer("profile.warm_repetitions", profile.warm_repetitions)
    receipt.integer("seed", seed)
    receipt.integer("publication_count", profile.publication_count)
    receipt.integer("page_limit", _PAGE_LIMIT)
    receipt.integer("revision.revision", _REVISION)
    receipt.timestamp("revision.published_at", _REVISION_PUBLISHED_AT)
    receipt.integer("revision.publication_count", profile.publication_count)
    receipt.integer("revision.artifact_count", profile.publication_count)
    receipt.text("config.public_base_url", _PUBLIC_BASE_URL)
    receipt.text("config.title", _CATALOG_TITLE)
    receipt.integer("config.default_page_size", _PAGE_LIMIT)
    receipt.integer("config.maximum_page_size", _PAGE_LIMIT)
    receipt.boolean("config.authentication_enabled", False)
    receipt.text(
        "config.acquisition_relation",
        OPDS_OPEN_ACCESS_REL,
    )
    receipt.text("response.media_type", OPDS_FEED_MEDIA_TYPE)
    receipt.text(
        "reader.discovery_api",
        "CatalogReader.discover_publications_with_facets-v1",
    )
    receipt.text("cursor.query_digest_algorithm", "repr-utf8-sha256-v1")
    receipt.text(
        "facet.order_algorithm",
        "publication-count-desc,value-role-namespace-asc-v1",
    )
    receipt.text("discovery.order_algorithm", "fixture-position-asc-v1")
    receipt.integer("operation_order.count", len(_OPERATION_ORDER))
    for position, name in enumerate(_OPERATION_ORDER):
        receipt.text(f"operation_order.{position}", name)
    receipt.integer("facet_kinds.count", len(_FACET_KINDS))
    for position, facet in enumerate(_FACET_KINDS):
        receipt.text(f"facet_kinds.{position}", facet.value)
    receipt.integer(
        "deterministic_response_headers.count",
        len(_DETERMINISTIC_RESPONSE_HEADERS),
    )
    for position, header in enumerate(_DETERMINISTIC_RESPONSE_HEADERS):
        receipt.text(f"deterministic_response_headers.{position}", header)
    receipt.text("request.discovery", f"/opds/v2/publications?limit={_PAGE_LIMIT}")
    receipt.text("request.facet", f"/opds/v2/facets/{{facet}}?limit={_PAGE_LIMIT}")
    receipt.text(
        "request.search",
        f"/opds/v2/search?{urlencode({'query': SEARCH_TEXT, 'limit': _PAGE_LIMIT})}",
    )
    receipt.text("request.discovery_cursor", "follow:discovery_first_page.next")
    receipt.text("request.subject_cursor", "follow:facet_subject_first_page.next")
    _update_query_receipt(receipt, "query.empty", _EMPTY_QUERY)
    _update_query_receipt(receipt, "query.search", _SEARCH_QUERY)
    receipt.text(
        "query.empty.cursor_sha256",
        sha256(repr(_EMPTY_QUERY).encode("utf-8")).hexdigest(),
    )
    receipt.text(
        "query.search.cursor_sha256",
        sha256(repr(_SEARCH_QUERY).encode("utf-8")).hexdigest(),
    )
    return receipt


def build_synthetic_fixture(
    profile: BenchmarkProfile,
    *,
    seed: int = DEFAULT_SEED,
) -> SyntheticFixture:
    """Build deterministic legal catalog values without creating CBZ bytes."""

    if seed < 0:
        raise ValueError("seed must be nonnegative")
    publication_count = profile.publication_count
    base_timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    seed_digest = sha256(str(seed).encode("ascii")).digest()
    subject_offset = int.from_bytes(seed_digest[:2], "big") % 257
    contributor_offset = int.from_bytes(seed_digest[2:4], "big") % 193
    language_offset = seed_digest[4] % len(_LANGUAGES)
    role_offset = seed_digest[5] % len(_CONTRIBUTOR_ROLES)
    search_offset = int.from_bytes(seed_digest[6:8], "big") % 7
    receipt = _workload_receipt(profile=profile, seed=seed)
    search_match_count = 0
    facet_values: dict[
        CatalogFacetKind,
        set[tuple[str, str | None, str | None]],
    ] = {facet: set() for facet in _FACET_KINDS}
    publications: list[CatalogPublication] = []
    for position in range(publication_count):
        gid = 1_000_000 + position
        is_search_match = (position + search_offset) % 7 == 0
        search_match_count += int(is_search_match)
        subject_index = (position + subject_offset) % 257
        contributor_index = (position + contributor_offset) % 193
        language = _LANGUAGES[(position + language_offset) % len(_LANGUAGES)]
        role = _CONTRIBUTOR_ROLES[(position + role_offset) % len(_CONTRIBUTOR_ROLES)]
        title_prefix = "Benchmark Needle" if is_search_match else "Synthetic"
        title = f"{title_prefix} Publication {position:05d}"
        source_title = f"Synthetic Source {position:05d}"
        contributor = CatalogContributor(
            name=f"Contributor {contributor_index:03d}",
            role=role,
        )
        subject = CatalogSubject(
            name=f"Subject {subject_index:03d}",
            scheme="urn:h2h:benchmark:subject",
            code=f"subject-{subject_index:03d}",
        )
        published_at = base_timestamp + timedelta(seconds=position)
        artifact_id = (
            f"urn:h2h:artifact:acquisition:{gid}:sha256:{_ARTIFACT_PAYLOAD_SHA256}"
        )
        artifact = CatalogArtifact(
            artifact_id=artifact_id,
            name=f"synthetic-{gid}.cbz",
            storage_object=StorageObjectDescriptor(
                key=StorageObjectKey(
                    codec="managed-filesystem-v2",
                    segments=("acquisitions", "synthetic", f"{gid}.cbz"),
                ),
                size_bytes=1,
                sha256=_ARTIFACT_PAYLOAD_SHA256,
                modified_at=published_at,
            ),
            media_type="application/vnd.comicbook+zip",
        )
        publication = CatalogPublication(
            publication_id=f"urn:h2h:gallery:{gid}",
            gid=gid,
            source_gallery_name=f"Synthetic Gallery {position:05d} [{gid}]",
            title=title,
            source_title=source_title,
            sort_title=f"synthetic publication {position:05d}",
            summary=f"Deterministic benchmark record {position:05d}",
            language=language,
            published_at=published_at,
            downloaded_at=published_at + timedelta(hours=1),
            modified_at=published_at,
            page_count=0,
            cover=None,
            thumbnail=None,
            contributors=(contributor,),
            subjects=(subject,),
            artifacts=(artifact,),
        )
        publications.append(publication)
        facet_values[CatalogFacetKind.LANGUAGE].add((language, None, None))
        facet_values[CatalogFacetKind.SUBJECT].add((subject.name, None, subject.code))
        facet_values[CatalogFacetKind.CONTRIBUTOR].add(
            (contributor.name, contributor.role, None)
        )
        _update_publication_receipt(receipt, position, publication)
    facet_value_counts = tuple(
        (facet, len(facet_values[facet])) for facet in _FACET_KINDS
    )
    receipt.integer("search_match_count", search_match_count)
    for facet, count in facet_value_counts:
        receipt.integer(f"facet_value_counts.{facet.value}", count)
    return SyntheticFixture(
        publications=tuple(publications),
        manifest_sha256=receipt.digest(),
        canonical_receipt_bytes=receipt.byte_count,
        search_match_count=search_match_count,
        facet_value_counts=facet_value_counts,
    )


class SyntheticCatalogReader:
    """A preindexed, in-memory implementation of the public reader protocol."""

    def __init__(self, fixture: SyntheticFixture) -> None:
        self._publications = fixture.publications
        self._revision = CatalogRevision(
            revision=_REVISION,
            published_at=_REVISION_PUBLISHED_AT,
            publication_count=len(self._publications),
            artifact_count=len(self._publications),
        )
        self._publication_by_id = {
            publication.publication_id: publication
            for publication in self._publications
        }
        self._artifact_by_id = {
            artifact.artifact_id: artifact
            for publication in self._publications
            for artifact in publication.artifacts
        }
        self._publication_by_artifact_name = {
            artifact.name: publication
            for publication in self._publications
            for artifact in publication.artifacts
        }
        self._searchable_lexemes = tuple(
            frozenset(
                lexeme
                for field in (
                    publication.title,
                    publication.source_title,
                    *(item.name for item in publication.contributors),
                    *(item.name for item in publication.subjects),
                )
                for lexeme in catalog_search_field_lexemes(field)
            )
            for publication in self._publications
        )
        self._selections: dict[
            str,
            tuple[tuple[int, CatalogPublication], ...],
        ] = {}
        self._selection_offsets: dict[str, dict[int, int]] = {}
        self._facets: dict[
            tuple[str, CatalogFacetKind], tuple[CatalogFacetValue, ...]
        ] = {}
        self._discovery_limits: list[int] = []
        self._bundle_limits: list[tuple[int, int]] = []
        self._facet_limits: list[int] = []
        for query in (_EMPTY_QUERY, _SEARCH_QUERY):
            self._selection(query)
            for facet in _FACET_KINDS:
                self._facet_values(query, facet)
        observed_search_count = len(self._selection(_SEARCH_QUERY))
        if observed_search_count != fixture.search_match_count:
            raise RuntimeError(
                "synthetic fixture search count disagrees with its preindexed reader"
            )
        expected_facets = dict(fixture.facet_value_counts)
        for facet in _FACET_KINDS:
            if len(self._facet_values(_EMPTY_QUERY, facet)) != expected_facets[facet]:
                raise RuntimeError(
                    "synthetic fixture facet count disagrees with its preindexed reader"
                )

    @staticmethod
    def _bounded_limit(limit: int) -> int:
        if isinstance(limit, bool) or not 1 <= limit <= _PAGE_LIMIT:
            raise ValueError(f"synthetic reader limit must be in 1..{_PAGE_LIMIT}")
        return limit

    def observations(self) -> dict[str, object]:
        bundle_limits = tuple(limit for pair in self._bundle_limits for limit in pair)
        limits = (*self._discovery_limits, *bundle_limits, *self._facet_limits)
        return {
            "discovery_call_count": len(self._discovery_limits),
            "discovery_bundle_call_count": len(self._bundle_limits),
            "facet_call_count": len(self._facet_limits),
            "maximum_requested_limit": max(limits, default=0),
            "all_requested_limits": list(limits),
        }

    @staticmethod
    def _query_key(query: CatalogDiscoveryQuery) -> str:
        return repr(query)

    @classmethod
    def _query_sha256(cls, query: CatalogDiscoveryQuery) -> str:
        return sha256(cls._query_key(query).encode("utf-8")).hexdigest()

    def _revision_at(
        self,
        revision: CatalogRevision | int | None,
    ) -> CatalogRevision:
        selected = (
            self._revision.revision
            if isinstance(revision, CatalogRevision)
            else revision
        )
        if selected is not None and selected != self._revision.revision:
            raise CatalogRevisionNotFoundError(selected)
        return self._revision

    def _matches(self, position: int, query: CatalogDiscoveryQuery) -> bool:
        publication = self._publications[position]
        if query.search is not None and not set(query.search_lexemes).issubset(
            self._searchable_lexemes[position]
        ):
            return False
        if query.language is not None and publication.language != query.language:
            return False
        if query.subject is not None and not any(
            item.name == query.subject.value and item.code == query.subject.namespace
            for item in publication.subjects
        ):
            return False
        return query.contributor is None or any(
            item.name == query.contributor.name and item.role == query.contributor.role
            for item in publication.contributors
        )

    def _selection(
        self,
        query: CatalogDiscoveryQuery,
    ) -> tuple[tuple[int, CatalogPublication], ...]:
        key = self._query_key(query)
        cached = self._selections.get(key)
        if cached is not None:
            return cached
        selected = tuple(
            (position, publication)
            for position, publication in enumerate(self._publications)
            if self._matches(position, query)
        )
        self._selections[key] = selected
        self._selection_offsets[key] = {
            position: offset for offset, (position, _publication) in enumerate(selected)
        }
        return selected

    @staticmethod
    def _without_facet(
        query: CatalogDiscoveryQuery,
        facet: CatalogFacetKind,
    ) -> CatalogDiscoveryQuery:
        if facet is CatalogFacetKind.LANGUAGE:
            return replace(query, language=None)
        if facet is CatalogFacetKind.SUBJECT:
            return replace(query, subject=None)
        return replace(query, contributor=None)

    def _facet_values(
        self,
        query: CatalogDiscoveryQuery,
        facet: CatalogFacetKind,
    ) -> tuple[CatalogFacetValue, ...]:
        cache_key = (self._query_key(query), facet)
        cached = self._facets.get(cache_key)
        if cached is not None:
            return cached
        counts: defaultdict[tuple[str, str | None, str | None], int] = defaultdict(int)
        effective = self._without_facet(query, facet)
        for _position, publication in self._selection(effective):
            values: tuple[tuple[str, str | None, str | None], ...]
            if facet is CatalogFacetKind.LANGUAGE:
                values = ((publication.language, None, None),)
            elif facet is CatalogFacetKind.SUBJECT:
                values = tuple(
                    (item.name, None, item.code)
                    for item in publication.subjects
                    if item.name and item.code
                )
            else:
                values = tuple(
                    (item.name, item.role, None)
                    for item in publication.contributors
                    if item.name and item.role
                )
            for value in values:
                counts[value] += 1
        result = tuple(
            CatalogFacetValue(
                value=value,
                label=value,
                publication_count=count,
                role=role,
                namespace=namespace,
            )
            for (value, role, namespace), count in sorted(
                counts.items(),
                key=lambda item: (
                    -item[1],
                    item[0][0],
                    item[0][1] or "",
                    item[0][2] or "",
                ),
            )
        )
        self._facets[cache_key] = result
        return result

    def get_catalog_revision(self, revision: int | None = None) -> CatalogRevision:
        return self._revision_at(revision)

    def discover_publications(
        self,
        *,
        query: CatalogDiscoveryQuery = _EMPTY_QUERY,
        after: CatalogDiscoveryCursor | None = None,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogDiscoveryPage:
        limit = self._bounded_limit(limit)
        self._discovery_limits.append(limit)
        selected_revision = self._revision_at(revision)
        return self._discovery_page(
            query=query,
            after=after,
            limit=limit,
            selected_revision=selected_revision,
        )

    def _discovery_page(
        self,
        *,
        query: CatalogDiscoveryQuery,
        after: CatalogDiscoveryCursor | None,
        limit: int,
        selected_revision: CatalogRevision,
    ) -> CatalogDiscoveryPage:
        selected = self._selection(query)
        start = 0
        if after is not None:
            key = self._query_key(query)
            if (
                after.revision != selected_revision.revision
                or after.query_sha256 != self._query_sha256(query)
            ):
                raise CatalogCursorError("discovery cursor authority changed")
            offset = self._selection_offsets[key].get(after.position)
            if (
                offset is None
                or selected[offset][1].publication_id != after.publication_id
            ):
                raise CatalogCursorError("discovery cursor boundary changed")
            start = offset + 1
        visible = selected[start : start + limit]
        next_cursor = None
        if start + limit < len(selected) and visible:
            position, publication = visible[-1]
            next_cursor = CatalogDiscoveryCursor(
                revision=selected_revision.revision,
                query_sha256=self._query_sha256(query),
                position=position,
                publication_id=publication.publication_id,
            )
        return CatalogDiscoveryPage(
            revision=selected_revision,
            publications=tuple(publication for _position, publication in visible),
            next_cursor=next_cursor,
            limit=limit,
            total=len(selected) if query == _EMPTY_QUERY else None,
        )

    def discover_publications_with_facets(
        self,
        *,
        query: CatalogDiscoveryQuery = _EMPTY_QUERY,
        after: CatalogDiscoveryCursor | None = None,
        limit: int = 50,
        facet_limit: int = 128,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogDiscoveryBundle:
        limit = self._bounded_limit(limit)
        facet_limit = self._bounded_limit(facet_limit)
        self._bundle_limits.append((limit, facet_limit))
        selected_revision = self._revision_at(revision)
        page = self._discovery_page(
            query=query,
            after=after,
            limit=limit,
            selected_revision=selected_revision,
        )
        facets = tuple(
            self._facet_page(
                facet=facet,
                query=query,
                after=None,
                limit=facet_limit,
                selected_revision=selected_revision,
            )
            for facet in _FACET_KINDS
        )
        return CatalogDiscoveryBundle(page=page, facets=facets)

    def list_publication_facets(
        self,
        *,
        facet: CatalogFacetKind,
        query: CatalogDiscoveryQuery = _EMPTY_QUERY,
        after: CatalogFacetCursor | None = None,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogFacetPage:
        limit = self._bounded_limit(limit)
        self._facet_limits.append(limit)
        selected_revision = self._revision_at(revision)
        return self._facet_page(
            facet=facet,
            query=query,
            after=after,
            limit=limit,
            selected_revision=selected_revision,
        )

    def _facet_page(
        self,
        *,
        facet: CatalogFacetKind,
        query: CatalogDiscoveryQuery,
        after: CatalogFacetCursor | None,
        limit: int,
        selected_revision: CatalogRevision,
    ) -> CatalogFacetPage:
        values = self._facet_values(query, facet)
        start = 0
        if after is not None:
            if (
                after.revision != selected_revision.revision
                or after.query_sha256 != self._query_sha256(query)
                or after.facet is not facet
                or after.position >= len(values)
            ):
                raise CatalogCursorError("facet cursor authority changed")
            boundary = values[after.position]
            if sha256(boundary.value.encode("utf-8")).hexdigest() != (
                after.value_sha256
            ):
                raise CatalogCursorError("facet cursor boundary changed")
            start = after.position + 1
        visible = values[start : start + limit]
        next_cursor = None
        if start + limit < len(values) and visible:
            position = start + len(visible) - 1
            next_cursor = CatalogFacetCursor(
                revision=selected_revision.revision,
                query_sha256=self._query_sha256(query),
                facet=facet,
                position=position,
                value_sha256=sha256(visible[-1].value.encode("utf-8")).hexdigest(),
            )
        return CatalogFacetPage(
            revision=selected_revision,
            facet=facet,
            values=visible,
            next_cursor=next_cursor,
            limit=limit,
        )

    def list_recent_publications(
        self,
        *,
        order: CatalogRecentOrder,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogRecentWindow:
        selected_revision = self._revision_at(revision)
        key = (
            (lambda publication: (publication.published_at, publication.gid))
            if order is CatalogRecentOrder.UPLOADED
            else (lambda publication: (publication.downloaded_at, publication.gid))
        )
        return CatalogRecentWindow(
            revision=selected_revision,
            order=order,
            publications=tuple(sorted(self._publications, key=key, reverse=True)[:128]),
        )

    def get_publication(
        self,
        publication_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogPublication | None:
        self._revision_at(revision)
        return self._publication_by_id.get(publication_id)

    def get_publication_presentation(
        self,
        publication_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogPublicationPresentation | None:
        publication = self.get_publication(publication_id, revision=revision)
        if publication is None:
            return None
        return CatalogPublicationPresentation(
            publication_id=publication.publication_id,
            page_count=0,
            cover=None,
            thumbnail=None,
        )

    def get_publication_page(
        self,
        publication_id: str,
        page_index: int,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogImageResource | None:
        del page_index
        self._revision_at(revision)
        if publication_id not in self._publication_by_id:
            return None
        return None

    def get_publications_by_artifact_names(
        self,
        names: Sequence[str],
        *,
        revision: CatalogRevision | int | None = None,
    ) -> Mapping[str, CatalogPublication]:
        self._revision_at(revision)
        return {
            name: publication
            for name in names
            if (publication := self._publication_by_artifact_name.get(name)) is not None
        }

    def get_artifact(
        self,
        artifact_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogArtifact | None:
        self._revision_at(revision)
        return self._artifact_by_id.get(artifact_id)


def _latency_summary(samples_ns: list[int]) -> dict[str, object]:
    ordered = sorted(samples_ns)
    return {
        "samples_ns": samples_ns,
        "minimum_ns": ordered[0],
        "median_ns": int(statistics.median(ordered)),
        "mean_ns": int(statistics.fmean(ordered)),
        "maximum_ns": ordered[-1],
    }


def _json_object(body: bytes) -> dict[str, object]:
    value: object = json.loads(body)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RuntimeError("benchmark response is not a JSON object")
    return cast("dict[str, object]", value)


def _collection_length(document: dict[str, object], field: str) -> int:
    value = document.get(field)
    if value is None:
        return 0
    if not isinstance(value, list):
        raise RuntimeError(f"benchmark response field {field!r} is not an array")
    return len(value)


def _next_link(document: dict[str, object]) -> str:
    links = document.get("links")
    if not isinstance(links, list):
        raise RuntimeError("benchmark response has no link array")
    for raw_link in links:
        if not isinstance(raw_link, dict) or raw_link.get("rel") != "next":
            continue
        href = raw_link.get("href")
        if not isinstance(href, str) or not href:
            break
        return href
    raise RuntimeError("benchmark response has no next link")


def _response_shape(document: dict[str, object]) -> dict[str, object]:
    links = document.get("links")
    has_next = isinstance(links, list) and any(
        isinstance(item, dict) and item.get("rel") == "next" for item in links
    )
    return {
        "publication_count": _collection_length(document, "publications"),
        "facet_group_count": _collection_length(document, "facets"),
        "navigation_count": _collection_length(document, "navigation"),
        "has_next": has_next,
    }


async def _get_response(client: AsyncClient, url: str) -> _FetchedResponse:
    response = await client.get(url)
    body = response.content
    if response.status_code != 200:
        raise RuntimeError(
            f"benchmark request {url!r} returned HTTP {response.status_code}: "
            f"{body[:256]!r}"
        )
    deterministic_headers: list[tuple[str, str]] = []
    for name in _DETERMINISTIC_RESPONSE_HEADERS:
        value = response.headers.get(name)
        if value is None:
            raise RuntimeError(
                f"benchmark response omitted deterministic header {name!r}"
            )
        deterministic_headers.append((name, value))
    header_map = dict(deterministic_headers)
    if header_map["content-type"] != OPDS_FEED_MEDIA_TYPE:
        raise RuntimeError("benchmark response is not an OPDS 2 feed document")
    if header_map["content-length"] != str(len(body)):
        raise RuntimeError("benchmark response Content-Length disagrees with its body")
    return _FetchedResponse(
        status_code=response.status_code,
        deterministic_headers=tuple(deterministic_headers),
        body=body,
    )


async def _timed_get(client: AsyncClient, url: str) -> _TimedResponse:
    started = time.perf_counter_ns()
    response = await _get_response(client, url)
    return _TimedResponse(time.perf_counter_ns() - started, response)


def _same_response(left: _FetchedResponse, right: _FetchedResponse) -> bool:
    return (
        left.status_code == right.status_code
        and left.deterministic_headers == right.deterministic_headers
        and left.body == right.body
    )


async def _measure_timing_operation(
    client: AsyncClient,
    *,
    url: str,
    warm_repetitions: int,
) -> tuple[dict[str, object], _FetchedResponse]:
    gc.collect()
    first = await _timed_get(client, url)
    expected_sha256 = sha256(first.response.body).hexdigest()
    warm_samples: list[int] = []
    for _iteration in range(warm_repetitions):
        warmed = await _timed_get(client, url)
        if not _same_response(warmed.response, first.response):
            raise RuntimeError("warm benchmark response changed observable HTTP fields")
        warm_samples.append(warmed.elapsed_ns)
    document = _json_object(first.response.body)
    return (
        {
            "request": url,
            "status_code": first.response.status_code,
            "deterministic_headers": dict(first.response.deterministic_headers),
            "body_bytes": len(first.response.body),
            "body_sha256": expected_sha256,
            "first_sample_ns": first.elapsed_ns,
            "warm": _latency_summary(warm_samples),
            "response_shape": _response_shape(document),
        },
        first.response,
    )


_OperationMeasurer = Callable[
    [str, str],
    Awaitable[tuple[dict[str, object], _FetchedResponse]],
]


async def _run_operation_suite(
    measure: _OperationMeasurer,
    *,
    retain_responses: bool,
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, _FetchedResponse],
    tuple[str, ...],
]:
    operations: dict[str, dict[str, object]] = {}
    responses: dict[str, _FetchedResponse] = {}
    observed_order: list[str] = []

    async def record(name: str, url: str) -> _FetchedResponse:
        operation, response = await measure(name, url)
        operations[name] = operation
        if retain_responses:
            responses[name] = response
        observed_order.append(name)
        return response

    discovery = await record(
        "discovery_first_page",
        f"/opds/v2/publications?limit={_PAGE_LIMIT}",
    )
    discovery_next = _next_link(_json_object(discovery.body))
    del discovery

    subject_next: str | None = None
    for facet in _FACET_KINDS:
        response = await record(
            f"facet_{facet.value}_first_page",
            f"/opds/v2/facets/{facet.value}?limit={_PAGE_LIMIT}",
        )
        if facet is CatalogFacetKind.SUBJECT:
            subject_next = _next_link(_json_object(response.body))
    del response

    search_parameters = urlencode({"query": SEARCH_TEXT, "limit": _PAGE_LIMIT})
    await record(
        "nonempty_search_first_page",
        f"/opds/v2/search?{search_parameters}",
    )
    await record("discovery_cursor_page", discovery_next)

    if subject_next is None:
        raise RuntimeError("subject facet did not provide a cursor page")
    await record("facet_subject_cursor_page", subject_next)

    exact_order = tuple(observed_order)
    if exact_order != _OPERATION_ORDER:
        raise RuntimeError("benchmark operation order drifted from its receipt")
    return operations, responses, exact_order


@asynccontextmanager
async def _benchmark_client(
    reader: SyntheticCatalogReader,
) -> AsyncIterator[AsyncClient]:
    with tempfile.TemporaryDirectory(prefix="h2hdb-opds-scalability-") as root:
        benchmark_root = Path(root).resolve(strict=True)
        library_root = benchmark_root / "current"
        coordination_root = benchmark_root / "coordination"
        library_root.mkdir()
        coordination_root.mkdir()
        (coordination_root / "publication.lock").touch()
        config = OPDSConfig(
            library_root=library_root,
            coordination_root=coordination_root,
            public_base_url=_PUBLIC_BASE_URL,
            title=_CATALOG_TITLE,
            default_page_size=_PAGE_LIMIT,
            maximum_page_size=_PAGE_LIMIT,
        )
        application = create_app(config, reader)
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application),
                base_url=_PUBLIC_BASE_URL,
            ) as client:
                yield client


@dataclass(frozen=True, slots=True)
class _FixtureEvidence:
    manifest_sha256: str
    canonical_receipt_bytes: int
    search_match_count: int
    facet_value_counts: tuple[tuple[CatalogFacetKind, int], ...]


def _fixture_evidence(fixture: SyntheticFixture) -> _FixtureEvidence:
    return _FixtureEvidence(
        manifest_sha256=fixture.manifest_sha256,
        canonical_receipt_bytes=fixture.canonical_receipt_bytes,
        search_match_count=fixture.search_match_count,
        facet_value_counts=fixture.facet_value_counts,
    )


@dataclass(frozen=True, slots=True)
class _TimingPassResult:
    fixture: _FixtureEvidence
    fixture_and_index_build_ns: int
    operations: dict[str, dict[str, object]]
    responses: dict[str, _FetchedResponse]
    operation_order: tuple[str, ...]
    reader_observations: dict[str, object]


async def _run_timing_pass(
    profile: BenchmarkProfile,
    *,
    seed: int,
) -> _TimingPassResult:
    if tracemalloc.is_tracing():
        raise RuntimeError("latency pass requires tracemalloc to be disabled")
    gc.collect()
    fixture_started = time.perf_counter_ns()
    fixture = build_synthetic_fixture(profile, seed=seed)
    reader = SyntheticCatalogReader(fixture)
    fixture_build_ns = time.perf_counter_ns() - fixture_started
    evidence = _fixture_evidence(fixture)
    async with _benchmark_client(reader) as client:

        async def measure(
            _name: str,
            url: str,
        ) -> tuple[dict[str, object], _FetchedResponse]:
            return await _measure_timing_operation(
                client,
                url=url,
                warm_repetitions=profile.warm_repetitions,
            )

        operations, responses, operation_order = await _run_operation_suite(
            measure,
            retain_responses=True,
        )
    return _TimingPassResult(
        fixture=evidence,
        fixture_and_index_build_ns=fixture_build_ns,
        operations=operations,
        responses=responses,
        operation_order=operation_order,
        reader_observations=reader.observations(),
    )


def _run_fixture_memory_pass(
    profile: BenchmarkProfile,
    *,
    seed: int,
) -> tuple[dict[str, int], _FixtureEvidence]:
    gc.collect()
    tracemalloc.start()
    try:
        baseline, _baseline_peak = tracemalloc.get_traced_memory()
        fixture = build_synthetic_fixture(profile, seed=seed)
        reader = SyntheticCatalogReader(fixture)
        current, peak = tracemalloc.get_traced_memory()
        evidence = _fixture_evidence(fixture)
        # Retain both objects through the peak observation; they are discarded
        # before the independent request-memory pass is constructed.
        _ = reader
        return (
            {
                "python_traced_baseline_bytes": baseline,
                "python_traced_current_delta_bytes": max(0, current - baseline),
                "python_traced_peak_delta_bytes": max(0, peak - baseline),
            },
            evidence,
        )
    finally:
        tracemalloc.stop()


async def _run_request_memory_pass(
    profile: BenchmarkProfile,
    *,
    seed: int,
    expected_fixture: _FixtureEvidence,
    expected_responses: Mapping[str, _FetchedResponse],
) -> tuple[dict[str, object], tuple[str, ...], dict[str, object]]:
    fixture = build_synthetic_fixture(profile, seed=seed)
    observed_fixture = _fixture_evidence(fixture)
    if observed_fixture != expected_fixture:
        raise RuntimeError("fresh request-memory fixture changed canonical authority")
    reader = SyntheticCatalogReader(fixture)
    async with _benchmark_client(reader) as client:
        gc.collect()
        tracemalloc.start()
        suite_peak_delta = 0
        try:
            suite_baseline, _baseline_peak = tracemalloc.get_traced_memory()

            async def measure(
                name: str,
                url: str,
            ) -> tuple[dict[str, object], _FetchedResponse]:
                nonlocal suite_peak_delta
                gc.collect()
                operation_baseline, _operation_peak = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
                response = await _get_response(client, url)
                _current, peak = tracemalloc.get_traced_memory()
                suite_peak_delta = max(
                    suite_peak_delta,
                    max(0, peak - suite_baseline),
                )
                expected = expected_responses.get(name)
                if expected is None or not _same_response(response, expected):
                    raise RuntimeError(
                        "fresh request-memory pass changed observable HTTP fields"
                    )
                return (
                    {
                        "request": url,
                        "python_traced_peak_delta_bytes": max(
                            0,
                            peak - operation_baseline,
                        ),
                    },
                    response,
                )

            operation_memory, _responses, operation_order = await _run_operation_suite(
                measure, retain_responses=False
            )
            current, _peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    return (
        {
            "python_traced_baseline_bytes": suite_baseline,
            "python_traced_current_delta_bytes": max(0, current - suite_baseline),
            "python_traced_peak_delta_bytes": suite_peak_delta,
            "operations": operation_memory,
        },
        operation_order,
        reader.observations(),
    )


def _process_max_rss_bytes() -> int:
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


async def run_benchmark(
    profile: BenchmarkProfile,
    *,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    """Run one profile and return a machine-readable result object."""
    if tracemalloc.is_tracing():
        raise RuntimeError("benchmark requires tracemalloc to be disabled on entry")
    provenance_started = time.perf_counter_ns()
    source_provenance = collect_source_provenance()
    provenance_build_ns = time.perf_counter_ns() - provenance_started
    timing = await _run_timing_pass(profile, seed=seed)
    fixture_memory, memory_fixture = _run_fixture_memory_pass(profile, seed=seed)
    if memory_fixture != timing.fixture:
        raise RuntimeError("fixture-memory pass changed canonical authority")
    request_memory, memory_order, memory_reader = await _run_request_memory_pass(
        profile,
        seed=seed,
        expected_fixture=timing.fixture,
        expected_responses=timing.responses,
    )
    if memory_order != timing.operation_order:
        raise RuntimeError("fresh memory pass changed benchmark operation order")

    body_sha256 = {
        name: cast("str", operation["body_sha256"])
        for name, operation in timing.operations.items()
    }
    smoke_contract_applies = profile == SMOKE_PROFILE and seed == DEFAULT_SEED
    if smoke_contract_applies:
        if timing.fixture.manifest_sha256 != SMOKE_EXPECTED_MANIFEST_SHA256:
            raise RuntimeError("smoke fixture/workload manifest changed")
        if body_sha256 != SMOKE_EXPECTED_BODY_SHA256:
            raise RuntimeError("smoke OPDS response-body digests changed")

    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark": "h2hdb-opds-synthetic-scalability",
        "profile": {
            "name": profile.name,
            "publication_count": profile.publication_count,
            "page_limit": _PAGE_LIMIT,
            "warm_repetitions": profile.warm_repetitions,
            "seed": seed,
        },
        "mode": {
            "name": "serialization-only",
            "sql_backed": False,
            "protocol": "OPDS 2.0",
            "reader": "preindexed-synthetic-catalog-reader",
            "first_sample_definition": (
                "first invocation of each operation in one fresh timing-pass "
                "application lifespan, in operation_order; later operations may "
                "observe process and ASGI infrastructure warmed by earlier ones"
            ),
            "warm_definition": (
                "immediate byte-identical repetitions after that operation's "
                "first invocation"
            ),
            "included": [
                "ASGI routing and in-process HTTP transport",
                "publication coordination read lock",
                "CatalogService revision, cursor, and publication validation",
                "OPDS 2 document construction and JSON serialization",
                "bounded synthetic reader page slicing",
            ],
            "excluded": [
                "SQL, database connections, and schema startup audit",
                "network transport",
                "CBZ creation, storage, acquisition, and media bytes",
                "synthetic fixture and index construction",
                "Python allocation tracing",
            ],
            "timing_instrumentation": "perf_counter_ns with tracemalloc disabled",
        },
        "operation_order": list(timing.operation_order),
        "source_provenance": _source_provenance_document(source_provenance),
        "sql_backed_follow_up": {
            "implemented": True,
            "owner": "h2hdb core",
            "fixture": (
                "manifest-bound READY epoch-3 database generated by a core-owned "
                "fixture tool, with 10,000 published neutral acquisition descriptors"
            ),
            "opds_integration": (
                "consume the fixture read-only through open_database; do not add "
                "OPDS-owned schema, SQL, or connector access"
            ),
            "module": "benchmarks.opds_sqlite_scalability",
        },
        "fixture": {
            "manifest_sha256": timing.fixture.manifest_sha256,
            "canonical_receipt_bytes": timing.fixture.canonical_receipt_bytes,
            "search_query": SEARCH_TEXT,
            "search_match_count": timing.fixture.search_match_count,
            "facet_value_counts": {
                facet.value: count for facet, count in timing.fixture.facet_value_counts
            },
            "artifact_payload_files_created": 0,
            "artifact_descriptor_size_bytes": 1,
        },
        "setup": {
            "source_manifest_build_ns": provenance_build_ns,
            "fixture_and_index_build_ns": timing.fixture_and_index_build_ns,
            "timing_tracemalloc_enabled": False,
        },
        "memory": {
            "fixture_and_index_fresh_pass": fixture_memory,
            "request_fresh_app_reader_pass": request_memory,
            "process_lifetime_max_rss_bytes": _process_max_rss_bytes(),
            "process_lifetime_max_rss_scope": (
                "lifetime high-water mark for the benchmark process across timing, "
                "fixture/index-memory, and request-memory passes"
            ),
        },
        "determinism": {
            "fresh_request_pass_matches_timing_pass": True,
            "validated_http_fields": [
                "status_code",
                "response_body",
                *_DETERMINISTIC_RESPONSE_HEADERS,
            ],
            "smoke_expected_contract_applied": smoke_contract_applies,
        },
        "reader_observations": {
            "timing_pass": timing.reader_observations,
            "request_memory_pass": memory_reader,
        },
        "operations": timing.operations,
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "h2hdb": version("h2hdb"),
            "h2hdb_opds": version("h2hdb-opds"),
            "timer": "perf_counter_ns",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure OPDS serialization over a deterministic preindexed synthetic "
            "CatalogReader and emit JSON on stdout."
        )
    )
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default=SMOKE_PROFILE.name,
        help="smoke runs in tests; 10k is the manual scalability profile",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="nonnegative deterministic fixture seed",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of indented JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.seed < 0:
        raise SystemExit("--seed must be nonnegative")
    report = asyncio.run(
        run_benchmark(
            PROFILES[arguments.profile],
            seed=arguments.seed,
        )
    )
    json.dump(
        report,
        sys.stdout,
        sort_keys=True,
        indent=None if arguments.compact else 2,
        separators=(",", ":") if arguments.compact else None,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
