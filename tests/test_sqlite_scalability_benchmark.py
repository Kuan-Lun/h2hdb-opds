from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import cast

import h2hdb
import pytest

from benchmarks.opds_sqlite_scalability import (
    BENCHMARK_SCHEMA_VERSION,
    FixtureReceiptError,
    load_core_fixture,
    run_sqlite_benchmark,
)

_SMOKE_PUBLICATION_COUNT = 165
_EXPECTED_OPERATION_ORDER = (
    "discovery_first_page",
    "discovery_cursor_page",
    "nonempty_search_first_page",
    "nonempty_search_cursor_page",
    "facet_language_first_page",
    "facet_subject_first_page",
    "facet_contributor_first_page",
)
_SMOKE_EXPECTED_BODY_SHA256 = {
    "discovery_first_page": (
        "11a65e2df97ddc503703e094fda7e9b212907348fd5214d646a86cbdf273a62e"
    ),
    "discovery_cursor_page": (
        "b37765d2d67f9c499a126d5e4bba5e03f0b48c371ac8fc2729acb7c0a67cbd9d"
    ),
    "nonempty_search_first_page": (
        "aaa356f5aab73382d2758bd8bb19c8f0cf92afa5cf1bafc8b389e506b274fd37"
    ),
    "nonempty_search_cursor_page": (
        "bc5351ac38d1b5ad3091c03924ec0d0a443ba152e5484efe4e77f714f4cf7cdb"
    ),
    "facet_language_first_page": (
        "3f69c649aee0cc2bf447ce135f69abcc921a10662b9037036f9a32e919ea9e15"
    ),
    "facet_subject_first_page": (
        "1b474850976186734e1528f0aebe8ae17def55befd2e373bb9aaa7b1152358e1"
    ),
    "facet_contributor_first_page": (
        "012bc792262c357f2e800ccbdbe5946952d0041f9a576cc39cf779cec0850ebc"
    ),
}


@pytest.fixture(scope="module")
def core_smoke_fixture(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    raw_init = h2hdb.__file__
    if raw_init is None:
        pytest.skip("the imported h2hdb package has no locatable source tree")
    core_root = Path(raw_init).resolve(strict=True).parents[2]
    generator = core_root / "benchmarks" / "sqlite_catalog_scalability.py"
    if not generator.is_file():
        pytest.skip("the installed h2hdb wheel does not include its fixture generator")
    root = tmp_path_factory.mktemp("core-sqlite-scalability")
    database = root / "catalog.sqlite3"
    receipt = root / "core-receipt.json"
    completed = subprocess.run(
        (
            sys.executable,
            str(generator),
            "--profile",
            "smoke",
            "--database",
            str(database),
            "--receipt",
            str(receipt),
        ),
        cwd=core_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return database, receipt


def test_core_fixture_receipt_is_exactly_bound_to_ready_database(
    core_smoke_fixture: tuple[Path, Path],
) -> None:
    database, receipt = core_smoke_fixture

    authority = load_core_fixture(database, receipt)

    assert authority.receipt_format == "h2hdb-sqlite-catalog-scalability-v1"
    assert authority.receipt_schema_version == 1
    assert authority.fixture_contract_includes_receipt_schema_version is False
    assert authority.fixture_mode == "manifest-bound-sql"
    assert authority.profile == "smoke"
    assert authority.schema_epoch == 3
    assert authority.schema_version == 2
    assert authority.publication_count == _SMOKE_PUBLICATION_COUNT
    assert authority.artifact_count == _SMOKE_PUBLICATION_COUNT
    assert authority.acquisition_descriptor_count == _SMOKE_PUBLICATION_COUNT
    assert authority.search_query == "needle"
    assert authority.search_publication_count == 33
    assert len(authority.search_first_page_gids) == 32
    assert authority.search_cursor_page_gids
    assert database.stat().st_size == authority.database_size_bytes
    assert hashlib.sha256(database.read_bytes()).hexdigest() == (
        authority.database_sha256
    )


def test_core_fixture_validation_supports_explicit_v1_and_fails_closed(
    core_smoke_fixture: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    database, receipt = core_smoke_fixture
    document = cast(
        "dict[str, object]", json.loads(receipt.read_text(encoding="utf-8"))
    )

    source = cast("dict[str, object]", document["source_provenance"])
    schema = cast("dict[str, object]", document["schema"])
    explicit_contract = {
        "format": document["format"],
        "fixture_mode": document["fixture_mode"],
        "core_version": document["core_version"],
        "source_manifest_sha256": source["source_manifest_sha256"],
        "seed": document["seed"],
        "schema_epoch": schema["epoch"],
        "schema_version": schema["schema_version"],
        "schema_manifest_sha256": schema["manifest_sha256"],
        "expected": document["expected"],
        "receipt_schema_version": 1,
    }
    explicit_digest = hashlib.sha256(
        json.dumps(
            explicit_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    explicit = tmp_path / "explicit-v1-receipt.json"
    explicit.write_text(
        json.dumps(
            {
                **document,
                "receipt_schema_version": 1,
                "fixture_contract_sha256": explicit_digest,
            }
        ),
        encoding="utf-8",
    )
    assert (
        load_core_fixture(
            database, explicit
        ).fixture_contract_includes_receipt_schema_version
        is True
    )

    unsupported = tmp_path / "unsupported-receipt.json"
    unsupported_document = {**document, "receipt_schema_version": 2}
    unsupported.write_text(json.dumps(unsupported_document), encoding="utf-8")
    with pytest.raises(FixtureReceiptError, match=r"unsupported.*schema version"):
        load_core_fixture(database, unsupported)

    broken_contract = tmp_path / "broken-contract.json"
    broken_contract.write_text(
        json.dumps({**document, "fixture_contract_sha256": "0" * 64}),
        encoding="utf-8",
    )
    with pytest.raises(FixtureReceiptError, match="contract digest"):
        load_core_fixture(database, broken_contract)

    changed_database = tmp_path / "changed.sqlite3"
    shutil.copyfile(database, changed_database)
    with changed_database.open("ab") as destination:
        destination.write(b"not-the-receipted-database")
    with pytest.raises(FixtureReceiptError, match="database bytes disagree"):
        load_core_fixture(changed_database, receipt)


async def test_sqlite_scalability_smoke_uses_public_app_and_exact_http_oracle(
    core_smoke_fixture: tuple[Path, Path],
) -> None:
    database, receipt = core_smoke_fixture
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    report = await run_sqlite_benchmark(
        database,
        receipt,
        warm_repetitions=1,
    )

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert report["schema_version"] == BENCHMARK_SCHEMA_VERSION
    assert report["benchmark"] == "h2hdb-opds-sqlite-scalability"
    mode = cast("dict[str, object]", report["mode"])
    assert mode["name"] == "sql-backed-public-http"
    assert mode["sql_backed"] is True
    assert mode["catalog_open_boundary"] == "h2hdb.open_database(read_only=True)"
    assert mode["reader_injected"] is False
    assert mode["core_internal_api_used"] is False
    assert mode["direct_sql_used"] is False
    assert mode["cbz_or_artwork_bytes_read"] is False
    assert report["operation_order"] == list(_EXPECTED_OPERATION_ORDER)

    fixture = cast("dict[str, object]", report["fixture"])
    database_evidence = cast("dict[str, object]", fixture["database"])
    assert database_evidence["unchanged_after_requests"] is True
    assert fixture["creates_or_reads_cbz_or_artwork_bytes"] is False
    schema = cast("dict[str, object]", fixture["schema"])
    assert schema["state"] == "READY"
    assert schema["full_ready_audit_passed_by_core_fixture"] is True
    assert schema["full_ready_audit_passed_by_opds_startup"] is True

    setup = cast("dict[str, object]", report["setup"])
    assert setup["startup_timing_included_in_request_samples"] is False
    assert setup["timing_tracemalloc_enabled"] is False
    for field in (
        "input_receipt_and_database_validation_ns",
        "source_manifest_build_ns",
        "timing_pass_startup_and_full_ready_audit_ns",
        "memory_pass_startup_and_full_ready_audit_ns",
    ):
        value = setup[field]
        assert isinstance(value, int)
        assert value > 0

    operations = cast("dict[str, dict[str, object]]", report["operations"])
    assert tuple(operations) == _EXPECTED_OPERATION_ORDER
    assert {
        name: operation["body_sha256"] for name, operation in operations.items()
    } == _SMOKE_EXPECTED_BODY_SHA256
    for operation in operations.values():
        assert operation["status_code"] == 200
        assert isinstance(operation["first_sample_ns"], int)
        assert operation["first_sample_ns"] > 0
        body_bytes = operation["body_bytes"]
        assert isinstance(body_bytes, int)
        assert body_bytes > 0
        headers = cast("dict[str, str]", operation["deterministic_headers"])
        assert headers == {
            "content-length": str(body_bytes),
            "content-type": "application/opds+json",
        }
        warm = cast("dict[str, object]", operation["warm"])
        samples = cast("list[int]", warm["samples_ns"])
        assert len(samples) == 1
        assert samples[0] > 0

    oracle = cast("dict[str, object]", report["validated_oracle"])
    assert oracle["publication_count"] == _SMOKE_PUBLICATION_COUNT
    assert oracle["search_query"] == "needle"
    assert oracle["search_publication_count"] == 33
    assert len(cast("list[int]", oracle["search_first_page_gids"])) == 32
    assert cast("list[int]", oracle["search_cursor_page_gids"])

    provenance = cast("dict[str, object]", report["source_provenance"])
    assert provenance["manifest_schema"] == "h2hdb-opds-source-manifest-v2"
    assert provenance["canonical_paths_are_relative"] is True
    components = cast("list[dict[str, object]]", provenance["components"])
    assert all(component["project_version"] for component in components)
    logical_paths = {
        cast("str", source["logical_path"])
        for component in components
        for source in cast("list[dict[str, object]]", component["files"])
    }
    assert "h2hdb-opds/benchmarks/opds_sqlite_scalability.py" in logical_paths
    assert "h2hdb/pyproject.toml" in logical_paths
    assert all(not PurePosixPath(path).is_absolute() for path in logical_paths)

    determinism = cast("dict[str, object]", report["determinism"])
    assert determinism["fresh_memory_pass_matches_timing_pass"] is True
    assert determinism["database_unchanged_after_requests"] is True
    assert determinism["fixture_receipt_unchanged_after_requests"] is True
    assert determinism["result_manifest_contains_absolute_paths"] is False
    result_digest = determinism["result_manifest_sha256"]
    assert isinstance(result_digest, str)
    assert len(result_digest) == 64

    memory = cast("dict[str, object]", report["memory"])
    request_memory = cast("dict[str, object]", memory["request_fresh_app_pass"])
    process_rss = memory["process_lifetime_max_rss_bytes"]
    entry_process_rss = memory["process_lifetime_max_rss_at_benchmark_entry_bytes"]
    assert isinstance(process_rss, int)
    assert isinstance(entry_process_rss, int)
    assert 0 < entry_process_rss <= process_rss
    assert isinstance(request_memory["python_traced_peak_delta_bytes"], int)
    assert request_memory["python_traced_peak_delta_bytes"] > 0
    operation_memory = cast(
        "dict[str, dict[str, object]]", request_memory["operations"]
    )
    assert tuple(operation_memory) == _EXPECTED_OPERATION_ORDER
    assert all(
        isinstance(item["python_traced_peak_delta_bytes"], int)
        and item["python_traced_peak_delta_bytes"] > 0
        for item in operation_memory.values()
    )
