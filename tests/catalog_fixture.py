"""Explicit, receipt-verified SQLite smoke inputs shared by integration tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import h2hdb
import pytest

from benchmarks.opds_sqlite_scalability import load_core_fixture


def _prepare_core_smoke_fixture(root: Path) -> tuple[Path, Path]:
    database = root / "catalog.sqlite3"
    receipt = root / "core-receipt.json"
    provided_database = os.environ.get("H2HDB_SQLITE_FIXTURE_DATABASE")
    provided_receipt = os.environ.get("H2HDB_SQLITE_FIXTURE_RECEIPT")
    if provided_database is not None or provided_receipt is not None:
        if not provided_database or not provided_database.strip():
            pytest.fail("H2HDB_SQLITE_FIXTURE_DATABASE must name the smoke database")
        if not provided_receipt or not provided_receipt.strip():
            pytest.fail("H2HDB_SQLITE_FIXTURE_RECEIPT must name its core receipt")
        shutil.copyfile(Path(provided_database).expanduser(), database)
        shutil.copyfile(Path(provided_receipt).expanduser(), receipt)
        authority = load_core_fixture(database, receipt)
        if authority.profile != "smoke":
            pytest.fail(
                "automatic SQLite integration requires the bounded smoke fixture"
            )
        return database, receipt

    configured_root = os.environ.get("H2HDB_CORE_REPOSITORY")
    if configured_root is not None:
        if not configured_root.strip():
            pytest.fail("H2HDB_CORE_REPOSITORY must name a core checkout")
        core_root = Path(configured_root).expanduser().resolve(strict=True)
    else:
        raw_init = h2hdb.__file__
        if raw_init is None:
            pytest.fail(
                "SQLite integration requires H2HDB_CORE_REPOSITORY or both "
                "H2HDB_SQLITE_FIXTURE_DATABASE and H2HDB_SQLITE_FIXTURE_RECEIPT"
            )
        core_root = Path(raw_init).resolve(strict=True).parents[2]
    generator = core_root / "benchmarks" / "sqlite_catalog_scalability.py"
    if not generator.is_file():
        if configured_root is not None:
            pytest.fail(
                f"H2HDB_CORE_REPOSITORY lacks the fixture generator: {generator}"
            )
        pytest.fail(
            "The installed h2hdb wheel lacks its smoke fixture generator; set "
            "H2HDB_CORE_REPOSITORY to an explicit core source checkout or provide "
            "both H2HDB_SQLITE_FIXTURE_DATABASE and H2HDB_SQLITE_FIXTURE_RECEIPT. "
            "Required SQLite integration cannot be skipped."
        )
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
