from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast


def test_repository_benchmarks_are_classified_as_dev_only() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    namespace = run_path(
        str(repository_root / "scripts" / "check-version.py"),
        run_name="h2hdb_opds_check_version_test",
    )
    ignored_paths = cast("tuple[str, ...]", namespace["_IGNORED_PATHS"])
    matches = cast(
        "Callable[[str, tuple[str, ...]], bool]",
        namespace["_matches"],
    )

    assert matches("benchmarks/__init__.py", ignored_paths)
    assert matches("benchmarks/opds_scalability.py", ignored_paths)
    assert not matches("src/h2hdb_opds/app.py", ignored_paths)
