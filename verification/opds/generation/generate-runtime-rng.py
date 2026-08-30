#!/usr/bin/env python3
"""Derive the runtime RNG from the strict Trang conversion deterministically."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_ROOT = REPOSITORY_ROOT / "verification/opds/schemas/opds-1.2"
UPSTREAM_SCHEMA = SCHEMA_ROOT / "opds-upstream.rng"
RUNTIME_SCHEMA = SCHEMA_ROOT / "opds.rng"

_UPSTREAM_URI_PATTERN = b'.*[ &lt;&gt;{}|^`"\\nrt].*'
_CORRECTED_URI_PATTERN = b'.*[ &lt;&gt;{}|^`"\\n\\r\\t].*'


def main() -> None:
    source = UPSTREAM_SCHEMA.read_bytes()
    if source.count(_UPSTREAM_URI_PATTERN) != 1:
        raise ValueError("the pinned upstream atomUri pattern is not exact")
    if _CORRECTED_URI_PATTERN in source:
        raise ValueError("the pinned upstream grammar is already corrected")
    RUNTIME_SCHEMA.write_bytes(
        source.replace(_UPSTREAM_URI_PATTERN, _CORRECTED_URI_PATTERN)
    )


if __name__ == "__main__":
    main()
