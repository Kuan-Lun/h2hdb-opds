#!/usr/bin/env python3
"""Verify the exact closed set of vendored OPDS schema snapshot bytes."""

from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPOSITORY_ROOT / "verification/opds/sources.toml"
SCHEMA_ROOT = REPOSITORY_ROOT / "verification/opds/schemas"
FORMAT = "h2hdb-opds-schema-snapshots.v1"
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)


def main() -> int:
    receipt = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    if receipt.get("schema") != FORMAT:
        raise ValueError("OPDS schema snapshot manifest has an unsupported format")
    raw_files = receipt.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise ValueError("OPDS schema snapshot manifest has no files")

    expected: dict[Path, str] = {}
    for raw_path, raw_digest in raw_files.items():
        if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
            raise TypeError("OPDS schema snapshot entries must be strings")
        if SHA256.fullmatch(raw_digest) is None:
            raise ValueError(f"OPDS schema snapshot has invalid SHA-256: {raw_path}")
        selected = (REPOSITORY_ROOT / raw_path).resolve()
        if not selected.is_relative_to(SCHEMA_ROOT.resolve()):
            raise ValueError(f"OPDS schema snapshot escapes its root: {raw_path}")
        expected[selected] = raw_digest

    actual = {path.resolve() for path in SCHEMA_ROOT.rglob("*") if path.is_file()}
    if actual != set(expected):
        missing = sorted(
            str(path.relative_to(REPOSITORY_ROOT))
            for path in expected
            if path not in actual
        )
        extra = sorted(
            str(path.relative_to(REPOSITORY_ROOT))
            for path in actual
            if path not in expected
        )
        raise ValueError(
            f"OPDS schema snapshot closure drifted; missing={missing}, extra={extra}"
        )

    for path, expected_digest in expected.items():
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            relative = path.relative_to(REPOSITORY_ROOT)
            raise ValueError(
                f"OPDS schema snapshot digest drifted: {relative}; "
                f"expected {expected_digest}, got {actual_digest}"
            )
    generation_files = receipt.get("generation_files")
    if not isinstance(generation_files, dict) or not generation_files:
        raise ValueError("OPDS schema snapshot manifest has no generation files")
    generation_root = (REPOSITORY_ROOT / "verification/opds/generation").resolve()
    expected_generation: dict[Path, str] = {}
    for raw_path, expected_digest in generation_files.items():
        if not isinstance(raw_path, str) or not isinstance(expected_digest, str):
            raise TypeError("OPDS schema generation entries must be strings")
        if SHA256.fullmatch(expected_digest) is None:
            raise ValueError(
                f"OPDS schema generation file has invalid SHA-256: {raw_path}"
            )
        path = (REPOSITORY_ROOT / raw_path).resolve()
        if not path.is_relative_to(generation_root):
            raise ValueError(
                f"OPDS schema generation file escapes its root: {raw_path}"
            )
        expected_generation[path] = expected_digest
    actual_generation = {
        path.resolve() for path in generation_root.rglob("*") if path.is_file()
    }
    if actual_generation != set(expected_generation):
        raise ValueError("OPDS schema generation file closure drifted")
    for path, expected_digest in expected_generation.items():
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            relative = path.relative_to(REPOSITORY_ROOT)
            raise ValueError(f"OPDS schema generation file digest drifted: {relative}")
    print(f"Verified {len(expected)} immutable OPDS schema snapshot files.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
