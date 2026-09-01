from dataclasses import replace
from pathlib import PurePosixPath
from typing import cast

import pytest
from h2hdb import (
    CatalogDiscoveryCursor,
    CatalogDiscoveryQuery,
    CatalogFacetCursor,
    CatalogFacetKind,
    CatalogReader,
)

from benchmarks.opds_scalability import (
    BENCHMARK_SCHEMA_VERSION,
    DEFAULT_SEED,
    SEARCH_TEXT,
    SMOKE_EXPECTED_BODY_SHA256,
    SMOKE_EXPECTED_MANIFEST_SHA256,
    SMOKE_PROFILE,
    TEN_THOUSAND_PROFILE,
    SyntheticCatalogReader,
    _source_manifest_sha256,
    build_synthetic_fixture,
    collect_source_provenance,
    run_benchmark,
)

_EXPECTED_OPERATION_ORDER = (
    "discovery_first_page",
    "facet_language_first_page",
    "facet_subject_first_page",
    "facet_contributor_first_page",
    "nonempty_search_first_page",
    "discovery_cursor_page",
    "facet_subject_cursor_page",
)


def _search_count(reader: SyntheticCatalogReader) -> int:
    query = CatalogDiscoveryQuery(search=SEARCH_TEXT)
    after: CatalogDiscoveryCursor | None = None
    total = 0
    while True:
        page = reader.discover_publications(
            query=query,
            after=after,
            limit=128,
        )
        total += len(page.publications)
        after = page.next_cursor
        if after is None:
            return total


def _facet_count(
    reader: SyntheticCatalogReader,
    facet: CatalogFacetKind,
) -> int:
    after: CatalogFacetCursor | None = None
    total = 0
    while True:
        page = reader.list_publication_facets(
            facet=facet,
            after=after,
            limit=128,
        )
        total += len(page.values)
        after = page.next_cursor
        if after is None:
            return total


def test_synthetic_scalability_fixture_has_fixed_complete_authority() -> None:
    first = build_synthetic_fixture(SMOKE_PROFILE)
    replay = build_synthetic_fixture(SMOKE_PROFILE)
    changed = build_synthetic_fixture(
        SMOKE_PROFILE,
        seed=DEFAULT_SEED + 1,
    )

    assert first == replay
    assert first.manifest_sha256 == SMOKE_EXPECTED_MANIFEST_SHA256
    assert first.manifest_sha256 != changed.manifest_sha256
    assert len(first.publications) == SMOKE_PROFILE.publication_count
    assert first.search_match_count == 55
    assert dict(first.facet_value_counts) == {
        CatalogFacetKind.LANGUAGE: 8,
        CatalogFacetKind.SUBJECT: 257,
        CatalogFacetKind.CONTRIBUTOR: 384,
    }
    assert first.canonical_receipt_bytes > 0
    assert all(
        artifact.storage_object.size_bytes == 1
        for publication in first.publications
        for artifact in publication.artifacts
    )

    reader = SyntheticCatalogReader(first)
    assert isinstance(reader, CatalogReader)
    assert _search_count(reader) == first.search_match_count
    assert {facet: _facet_count(reader, facet) for facet in CatalogFacetKind} == dict(
        first.facet_value_counts
    )


def test_synthetic_reader_rejects_every_unbounded_page_request() -> None:
    reader = SyntheticCatalogReader(build_synthetic_fixture(SMOKE_PROFILE))

    with pytest.raises(ValueError, match="limit must be in"):
        reader.discover_publications(limit=129)
    with pytest.raises(ValueError, match="limit must be in"):
        reader.discover_publications_with_facets(limit=129)
    with pytest.raises(ValueError, match="limit must be in"):
        reader.discover_publications_with_facets(facet_limit=129)
    with pytest.raises(ValueError, match="limit must be in"):
        reader.list_publication_facets(
            facet=CatalogFacetKind.LANGUAGE,
            limit=129,
        )


def test_source_provenance_is_path_independent_and_content_bound() -> None:
    first = collect_source_provenance()
    replay = collect_source_provenance()

    assert first.manifest_sha256 == replay.manifest_sha256
    assert len(first.manifest_sha256) == 64
    assert {component.name for component in first.components} == {
        "h2hdb-import",
        "h2hdb-opds",
    }
    opds = next(
        component for component in first.components if component.name == "h2hdb-opds"
    )
    core = next(
        component for component in first.components if component.name == "h2hdb-import"
    )
    assert opds.located is True
    assert core.located is True
    logical_paths = {
        source.logical_path
        for component in first.components
        for source in component.files
    }
    assert {
        "h2hdb-opds/pyproject.toml",
        "h2hdb-opds/benchmarks/opds_scalability.py",
        "h2hdb-opds/src/h2hdb_opds/__init__.py",
        "h2hdb/__init__.py",
    } <= logical_paths
    assert opds.project_version is not None
    # A source checkout binds its matching pyproject/version.  An installed
    # wheel still binds every imported Python byte but has no trustworthy
    # adjacent project manifest; distribution metadata remains diagnostic.
    assert ("h2hdb/pyproject.toml" in logical_paths) is (
        core.project_version is not None
    )
    assert all(not PurePosixPath(path).is_absolute() for path in logical_paths)
    assert all("/Users/" not in path and "\\" not in path for path in logical_paths)
    assert all(
        source.size_bytes >= 0 and len(source.sha256) == 64
        for component in first.components
        for source in component.files
    )

    original = opds.files[0]
    changed_file = replace(original, sha256="0" * 64)
    changed_opds = replace(opds, files=(changed_file, *opds.files[1:]))
    changed_components = tuple(
        changed_opds if component.name == opds.name else component
        for component in first.components
    )
    assert _source_manifest_sha256(changed_components) != first.manifest_sha256
    assert first.git_commit is None or len(first.git_commit) >= 40
    assert first.git_dirty is None or isinstance(first.git_dirty, bool)


def test_ten_thousand_profile_has_exact_expected_cardinalities() -> None:
    fixture = build_synthetic_fixture(TEN_THOUSAND_PROFILE)

    assert TEN_THOUSAND_PROFILE.publication_count == 10_000
    assert TEN_THOUSAND_PROFILE.warm_repetitions == 5
    assert len(fixture.publications) == 10_000
    assert fixture.search_match_count == 1_428
    assert dict(fixture.facet_value_counts) == {
        CatalogFacetKind.LANGUAGE: 8,
        CatalogFacetKind.SUBJECT: 257,
        CatalogFacetKind.CONTRIBUTOR: 1_158,
    }


async def test_serialization_scalability_smoke_profile_is_bounded() -> None:
    report = await run_benchmark(SMOKE_PROFILE)
    profile = cast("dict[str, object]", report["profile"])
    mode = cast("dict[str, object]", report["mode"])
    sql_backed = cast("dict[str, object]", report["sql_backed_follow_up"])
    fixture = cast("dict[str, object]", report["fixture"])
    operations = cast("dict[str, dict[str, object]]", report["operations"])
    memory = cast("dict[str, object]", report["memory"])
    determinism = cast("dict[str, object]", report["determinism"])
    provenance = cast("dict[str, object]", report["source_provenance"])
    observations = cast("dict[str, dict[str, object]]", report["reader_observations"])

    assert report["schema_version"] == BENCHMARK_SCHEMA_VERSION
    assert profile == {
        "name": "smoke",
        "publication_count": 384,
        "page_limit": 128,
        "warm_repetitions": 2,
        "seed": DEFAULT_SEED,
    }
    assert mode["name"] == "serialization-only"
    assert mode["sql_backed"] is False
    assert mode["timing_instrumentation"] == (
        "perf_counter_ns with tracemalloc disabled"
    )
    assert report["operation_order"] == list(_EXPECTED_OPERATION_ORDER)
    assert provenance["manifest_schema"] == "h2hdb-opds-source-manifest-v2"
    assert provenance["manifest_sha256"] == collect_source_provenance().manifest_sha256
    assert provenance["canonical_paths_are_relative"] is True
    git = cast("dict[str, object]", provenance["git"])
    assert git["canonical"] is False
    assert sql_backed["implemented"] is True
    assert sql_backed["module"] == "benchmarks.opds_sqlite_scalability"
    assert fixture["artifact_payload_files_created"] == 0
    assert fixture["manifest_sha256"] == SMOKE_EXPECTED_MANIFEST_SHA256
    assert fixture["search_match_count"] == 55
    assert fixture["facet_value_counts"] == {
        "language": 8,
        "subject": 257,
        "contributor": 384,
    }
    assert determinism == {
        "fresh_request_pass_matches_timing_pass": True,
        "validated_http_fields": [
            "status_code",
            "response_body",
            "content-length",
            "content-type",
        ],
        "smoke_expected_contract_applied": True,
    }
    setup = cast("dict[str, object]", report["setup"])
    assert isinstance(setup["source_manifest_build_ns"], int)
    assert setup["source_manifest_build_ns"] > 0

    assert tuple(operations) == _EXPECTED_OPERATION_ORDER
    assert {
        name: measurement["body_sha256"] for name, measurement in operations.items()
    } == SMOKE_EXPECTED_BODY_SHA256
    for measurement in operations.values():
        warm = cast("dict[str, object]", measurement["warm"])
        headers = cast("dict[str, str]", measurement["deterministic_headers"])
        body_bytes = measurement["body_bytes"]
        first_sample_ns = measurement["first_sample_ns"]
        assert isinstance(body_bytes, int)
        assert isinstance(first_sample_ns, int)
        assert measurement["status_code"] == 200
        assert body_bytes > 0
        assert first_sample_ns > 0
        assert headers == {
            "content-length": str(body_bytes),
            "content-type": "application/opds+json",
        }
        assert "cold_ns" not in measurement
        assert "peak_python_allocation_delta_bytes" not in measurement
        samples = cast("list[int]", warm["samples_ns"])
        assert len(samples) == 2
        assert all(sample > 0 for sample in samples)

    first_shape = cast(
        "dict[str, object]",
        operations["discovery_first_page"]["response_shape"],
    )
    cursor_shape = cast(
        "dict[str, object]",
        operations["discovery_cursor_page"]["response_shape"],
    )
    search_shape = cast(
        "dict[str, object]",
        operations["nonempty_search_first_page"]["response_shape"],
    )
    assert first_shape == {
        "publication_count": 128,
        "facet_group_count": 3,
        "navigation_count": 0,
        "has_next": True,
    }
    assert cursor_shape == first_shape
    assert search_shape == {
        "publication_count": 55,
        "facet_group_count": 3,
        "navigation_count": 0,
        "has_next": False,
    }

    fixture_memory = cast(
        "dict[str, int]",
        memory["fixture_and_index_fresh_pass"],
    )
    request_memory = cast(
        "dict[str, object]",
        memory["request_fresh_app_reader_pass"],
    )
    for field in (
        "python_traced_baseline_bytes",
        "python_traced_current_delta_bytes",
        "python_traced_peak_delta_bytes",
    ):
        assert fixture_memory[field] >= 0
        value = request_memory[field]
        assert isinstance(value, int)
        assert value >= 0
    process_rss = memory["process_lifetime_max_rss_bytes"]
    assert isinstance(process_rss, int)
    assert process_rss > 0
    request_operation_memory = cast(
        "dict[str, dict[str, object]]",
        request_memory["operations"],
    )
    assert tuple(request_operation_memory) == _EXPECTED_OPERATION_ORDER
    assert all(
        isinstance(item["python_traced_peak_delta_bytes"], int)
        and item["python_traced_peak_delta_bytes"] >= 0
        for item in request_operation_memory.values()
    )

    timing_reader = observations["timing_pass"]
    request_reader = observations["request_memory_pass"]
    assert timing_reader["discovery_call_count"] == 0
    assert timing_reader["discovery_bundle_call_count"] == 9
    assert timing_reader["facet_call_count"] == 12
    assert request_reader["discovery_call_count"] == 0
    assert request_reader["discovery_bundle_call_count"] == 3
    assert request_reader["facet_call_count"] == 4
    for reader in (timing_reader, request_reader):
        limits = cast("list[int]", reader["all_requested_limits"])
        assert limits
        assert max(limits) == 128
        assert all(1 <= limit <= 128 for limit in limits)
