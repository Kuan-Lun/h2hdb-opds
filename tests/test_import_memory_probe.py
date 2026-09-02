from __future__ import annotations

import json
import subprocess
import sys
from typing import cast

import pytest

from benchmarks.import_memory_probe import PROBE_SCHEMA_VERSION, run_probe


def test_import_probe_isolates_cold_and_warm_child_memory() -> None:
    report = run_probe(
        "fractions",
        trace_allocations=True,
        timeout_seconds=30,
    )

    assert report["schema_version"] == PROBE_SCHEMA_VERSION
    assert report["benchmark"] == "h2hdb-opds-import-memory"
    assert report["module"] == "fractions"
    mode = cast("dict[str, object]", report["mode"])
    assert mode["process_isolation"] == "fresh child per sample"
    assert mode["rss_authority"] == "resource.getrusage(resource.RUSAGE_SELF)"
    assert mode["trace_allocations"] is True

    for sample_name in ("cold", "warm"):
        sample = cast("dict[str, object]", report[sample_name])
        before_rss = sample["process_max_rss_before_import_bytes"]
        after_rss = sample["process_max_rss_after_import_bytes"]
        rss_delta = sample["process_max_rss_delta_bytes"]
        traced_current = sample["python_traced_current_delta_bytes"]
        traced_peak = sample["python_traced_peak_delta_bytes"]
        assert isinstance(before_rss, int)
        assert isinstance(after_rss, int)
        assert isinstance(rss_delta, int)
        assert isinstance(traced_current, int)
        assert isinstance(traced_peak, int)
        assert 0 < before_rss <= after_rss
        assert rss_delta == after_rss - before_rss
        assert 0 < traced_current <= traced_peak
        for field in (
            "imported_module_count_delta",
            "imported_python_source_file_count",
            "imported_python_source_bytes",
            "largest_imported_python_source_bytes",
        ):
            value = sample[field]
            assert isinstance(value, int)
            assert value >= 1
        assert isinstance(sample["largest_imported_python_source_module"], str)

    pycache = cast("dict[str, dict[str, int]]", report["isolated_pycache"])
    assert pycache["after_harness_prime"]["file_count"] >= 1
    assert pycache["cold_target_added"]["file_count"] >= 1
    assert pycache["cold_target_added"]["size_bytes"] >= 1
    assert pycache["warm_target_added"] == {"file_count": 0, "size_bytes": 0}
    assert "h2hdb-opds-import-probe-" not in json.dumps(report, sort_keys=True)


@pytest.mark.parametrize(
    "module_name",
    ["", ".h2hdb", "h2hdb..schema", "h2hdb/schema", "模組"],
)
def test_import_probe_rejects_unbounded_or_noncanonical_module_names(
    module_name: str,
) -> None:
    with pytest.raises(ValueError, match="module"):
        run_probe(module_name, timeout_seconds=1)


def test_import_probe_rejects_unbounded_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        run_probe("fractions", timeout_seconds=601)


def test_import_probe_module_cli_emits_one_json_document() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "benchmarks.import_memory_probe",
            "--module",
            "fractions",
            "--timeout-seconds",
            "30",
            "--compact",
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=35,
    )

    assert completed.returncode == 0, completed.stderr
    document = cast("dict[str, object]", json.loads(completed.stdout))
    assert document["benchmark"] == "h2hdb-opds-import-memory"
    assert document["module"] == "fractions"
