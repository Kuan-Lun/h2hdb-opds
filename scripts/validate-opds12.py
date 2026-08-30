#!/usr/bin/env python3
"""Validate OPDS 1.2 XML with the vendored official RELAX NG closure."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lxml import etree
from opds12_validation import (
    OPDS12ValidationError,
    load_relaxng,
    parse_document_bytes,
    parse_document_path,
    validate_document,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "verification/opds/schemas/opds-1.2/opds.rng"
UPSTREAM_SCHEMA_PATH = (
    REPOSITORY_ROOT / "verification/opds/schemas/opds-1.2/opds-upstream.rng"
)


def _document(path: str) -> etree._ElementTree:
    if path == "-":
        return parse_document_bytes(sys.stdin.buffer.read())
    return parse_document_path(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-schema",
        action="store_true",
        help="compile the vendored schema without validating a document",
    )
    parser.add_argument("documents", nargs="*")
    arguments = parser.parse_args()
    if not arguments.check_schema and not arguments.documents:
        parser.error("provide --check-schema or at least one XML document")

    try:
        load_relaxng(UPSTREAM_SCHEMA_PATH)
        schema = load_relaxng(SCHEMA_PATH)
    except (OSError, etree.LxmlError) as error:
        print(f"OPDS 1.2 schema compilation failed: {error}", file=sys.stderr)
        return 2

    valid = True
    for document_path in arguments.documents:
        try:
            document = _document(document_path)
        except (OSError, etree.LxmlError) as error:
            print(f"{document_path}: XML parse failed: {error}", file=sys.stderr)
            valid = False
            continue
        try:
            validate_document(document, schema)
        except OPDS12ValidationError as error:
            print(f"{document_path}: {error}", file=sys.stderr)
            valid = False
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
