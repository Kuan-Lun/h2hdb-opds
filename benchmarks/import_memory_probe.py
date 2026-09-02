"""Isolate cold-import and warm-import memory in fresh child processes.

The regular OPDS benchmarks import ``h2hdb`` before request-level allocation
tracing begins.  This probe gives that earlier phase its own auditable samples:
one fresh isolated bytecode cache is primed with the harness, then reused for a
cold target import and a warm target import.  Every RSS value is the importing
child's ``RUSAGE_SELF`` high-water mark, never the parent or another child.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import re
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

PROBE_SCHEMA_VERSION: Final = 1
DEFAULT_MODULE: Final = "h2hdb._generated_vnext_schema"
DEFAULT_TIMEOUT_SECONDS: Final = 300
_MODULE_ENTRYPOINT: Final = "benchmarks.import_memory_probe"
_MAXIMUM_TIMEOUT_SECONDS: Final = 600
_MAXIMUM_MODULE_NAME_BYTES: Final = 256
_MODULE_NAME_PATTERN: Final = re.compile(
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*",
    flags=re.ASCII,
)


def _process_max_rss_bytes() -> int:
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


def _validated_module_name(value: str) -> str:
    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("module must be an ASCII dotted import name") from error
    if (
        not encoded
        or len(encoded) > _MAXIMUM_MODULE_NAME_BYTES
        or _MODULE_NAME_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("module must be a bounded ASCII dotted import name")
    return value


def _validated_timeout_seconds(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAXIMUM_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be in 1..{_MAXIMUM_TIMEOUT_SECONDS}")
    return value


def _new_python_sources(
    before: frozenset[str],
) -> tuple[int, int, str | None, int]:
    seen: set[Path] = set()
    sources: list[tuple[str, int]] = []
    for name in sorted(set(sys.modules).difference(before)):
        imported = sys.modules.get(name)
        raw_path = None if imported is None else getattr(imported, "__file__", None)
        if not isinstance(raw_path, str) or not raw_path.endswith((".py", ".pyw")):
            continue
        path = Path(raw_path)
        try:
            resolved = path.resolve(strict=True)
            size = resolved.stat().st_size
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        sources.append((name, size))
    if not sources:
        return 0, 0, None, 0
    largest_name, largest_size = max(sources, key=lambda item: (item[1], item[0]))
    return (
        len(sources),
        sum(size for _name, size in sources),
        largest_name,
        largest_size,
    )


def _child_sample(
    module_name: str | None,
    *,
    trace_allocations: bool,
) -> dict[str, object]:
    before_modules = frozenset(sys.modules)
    if module_name is not None and module_name in before_modules:
        raise RuntimeError("target module was imported by the probe harness")
    if trace_allocations:
        tracemalloc.start()
        traced_baseline, _baseline_peak = tracemalloc.get_traced_memory()
    else:
        traced_baseline = 0
    rss_before = _process_max_rss_bytes()
    started = time.perf_counter_ns()
    if module_name is not None:
        importlib.import_module(module_name)
    elapsed_ns = time.perf_counter_ns() - started
    rss_after = _process_max_rss_bytes()
    if trace_allocations:
        traced_current, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        traced_current_delta: int | None = max(0, traced_current - traced_baseline)
        traced_peak_delta: int | None = max(0, traced_peak - traced_baseline)
    else:
        traced_current_delta = None
        traced_peak_delta = None
    (
        source_count,
        source_bytes,
        largest_source_module,
        largest_source_bytes,
    ) = _new_python_sources(before_modules)
    return {
        "elapsed_ns": elapsed_ns,
        "process_max_rss_before_import_bytes": rss_before,
        "process_max_rss_after_import_bytes": rss_after,
        "process_max_rss_delta_bytes": max(0, rss_after - rss_before),
        "python_traced_current_delta_bytes": traced_current_delta,
        "python_traced_peak_delta_bytes": traced_peak_delta,
        "imported_module_count_delta": len(set(sys.modules).difference(before_modules)),
        "imported_python_source_file_count": source_count,
        "imported_python_source_bytes": source_bytes,
        "largest_imported_python_source_module": largest_source_module,
        "largest_imported_python_source_bytes": largest_source_bytes,
    }


def _child_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--module")
    target.add_argument("--prime", action="store_true")
    parser.add_argument("--trace-allocations", action="store_true")
    return parser


def _child_main(argv: Sequence[str]) -> int:
    arguments = _child_parser().parse_args(argv)
    module_name = None
    if arguments.module is not None:
        module_name = _validated_module_name(cast("str", arguments.module))
    result = _child_sample(
        module_name,
        trace_allocations=cast("bool", arguments.trace_allocations),
    )
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def _parse_child_result(stdout: str) -> dict[str, object]:
    parsed = json.loads(stdout)
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise RuntimeError("import probe child returned a malformed result")
    return cast("dict[str, object]", parsed)


def _invoke_child(
    *,
    module_name: str | None,
    trace_allocations: bool,
    timeout_seconds: int,
    pycache_prefix: Path,
) -> dict[str, object]:
    command = [sys.executable, "-m", _MODULE_ENTRYPOINT, "_child"]
    if module_name is None:
        command.append("--prime")
    else:
        command.extend(("--module", module_name))
    if trace_allocations:
        command.append("--trace-allocations")
    environment = os.environ.copy()
    environment["PYTHONPYCACHEPREFIX"] = os.fspath(pycache_prefix)
    environment["PYTHONHASHSEED"] = "0"
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "import probe child failed with exit code "
            f"{completed.returncode}: {completed.stderr.strip()}"
        )
    return _parse_child_result(completed.stdout)


def _pycache_evidence(root: Path) -> dict[str, int]:
    files = tuple(path for path in root.rglob("*.pyc") if path.is_file())
    return {
        "file_count": len(files),
        "size_bytes": sum(path.stat().st_size for path in files),
    }


def _evidence_delta(
    after: Mapping[str, int],
    before: Mapping[str, int],
) -> dict[str, int]:
    return {
        field: max(0, after[field] - before[field])
        for field in ("file_count", "size_bytes")
    }


def run_probe(
    module_name: str = DEFAULT_MODULE,
    *,
    trace_allocations: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Return isolated cold/warm import samples for one bounded module name."""

    selected_module = _validated_module_name(module_name)
    selected_timeout = _validated_timeout_seconds(timeout_seconds)
    if type(trace_allocations) is not bool:
        raise TypeError("trace_allocations must be bool")
    with tempfile.TemporaryDirectory(prefix="h2hdb-opds-import-probe-") as raw_root:
        pycache_prefix = Path(raw_root) / "pycache"
        _invoke_child(
            module_name=None,
            trace_allocations=False,
            timeout_seconds=selected_timeout,
            pycache_prefix=pycache_prefix,
        )
        primed = _pycache_evidence(pycache_prefix)
        cold = _invoke_child(
            module_name=selected_module,
            trace_allocations=trace_allocations,
            timeout_seconds=selected_timeout,
            pycache_prefix=pycache_prefix,
        )
        after_cold = _pycache_evidence(pycache_prefix)
        warm = _invoke_child(
            module_name=selected_module,
            trace_allocations=trace_allocations,
            timeout_seconds=selected_timeout,
            pycache_prefix=pycache_prefix,
        )
        after_warm = _pycache_evidence(pycache_prefix)
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "benchmark": "h2hdb-opds-import-memory",
        "module": selected_module,
        "mode": {
            "process_isolation": "fresh child per sample",
            "rss_authority": "resource.getrusage(resource.RUSAGE_SELF)",
            "rss_semantics": "per-child lifetime high-water mark",
            "pycache": (
                "fresh isolated PYTHONPYCACHEPREFIX primed with the harness, then "
                "reused by cold and warm target-import children"
            ),
            "trace_allocations": trace_allocations,
            "timeout_seconds_per_child": selected_timeout,
        },
        "cold": cold,
        "warm": warm,
        "isolated_pycache": {
            "after_harness_prime": primed,
            "cold_target_added": _evidence_delta(after_cold, primed),
            "warm_target_added": _evidence_delta(after_warm, after_cold),
            "after_warm": after_warm,
        },
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--module",
        default=DEFAULT_MODULE,
        help="bounded dotted module name imported in each isolated child",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-child timeout in 1..{_MAXIMUM_TIMEOUT_SECONDS}",
    )
    parser.add_argument(
        "--trace-allocations",
        action="store_true",
        help="also trace Python allocations; this materially increases cost",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of indented JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    selected_argv = sys.argv[1:] if argv is None else list(argv)
    if selected_argv and selected_argv[0] == "_child":
        return _child_main(selected_argv[1:])
    parser = _parser()
    arguments = parser.parse_args(selected_argv)
    try:
        report = run_probe(
            cast("str", arguments.module),
            trace_allocations=cast("bool", arguments.trace_allocations),
            timeout_seconds=cast("int", arguments.timeout_seconds),
        )
    except (TypeError, ValueError) as error:
        parser.error(str(error))
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
