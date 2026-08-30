__all__ = [
    "decode_discovery_cursor",
    "decode_facet_cursor",
    "encode_discovery_cursor",
    "encode_facet_cursor",
]

import base64
import binascii
import json
import re
from typing import cast

from h2hdb import CatalogDiscoveryCursor, CatalogFacetCursor, CatalogFacetKind

_CURSOR_FORMAT = 2
_INT63_MAX = (1 << 63) - 1
_MAX_CURSOR_LENGTH = 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def _encode(fields: list[object]) -> str:
    payload = json.dumps(
        [_CURSOR_FORMAT, *fields],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode(value: str) -> list[object]:
    if len(value) > _MAX_CURSOR_LENGTH or _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError("cursor is not a canonical URL-safe token")
    padding = "=" * (-len(value) % 4)
    try:
        payload = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        decoded: object = json.loads(payload)
    except (UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as error:
        raise ValueError("cursor is not a valid encoded value") from error
    if (
        not isinstance(decoded, list)
        or len(decoded) < 2
        or decoded[0] != _CURSOR_FORMAT
    ):
        raise ValueError("cursor uses an unsupported format")
    return decoded[1:]


def _valid_position(revision: object, position: object) -> bool:
    return (
        not isinstance(revision, bool)
        and isinstance(revision, int)
        and 1 <= revision <= _INT63_MAX
        and not isinstance(position, bool)
        and isinstance(position, int)
        and 0 <= position <= _INT63_MAX
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def encode_discovery_cursor(cursor: CatalogDiscoveryCursor) -> str:
    return _encode(
        [
            "discovery",
            cursor.revision,
            cursor.query_sha256,
            cursor.position,
            cursor.publication_id,
        ]
    )


def decode_discovery_cursor(value: str) -> CatalogDiscoveryCursor:
    decoded = _decode(value)
    if len(decoded) != 5 or decoded[0] != "discovery":
        raise ValueError("cursor is not a discovery cursor")
    _, revision, query_sha256, position, publication_id = decoded
    if (
        not _valid_position(revision, position)
        or not _valid_sha256(query_sha256)
        or not isinstance(publication_id, str)
        or not publication_id
    ):
        raise ValueError("cursor fields are invalid")
    try:
        cursor = CatalogDiscoveryCursor(
            revision=cast("int", revision),
            query_sha256=cast("str", query_sha256),
            position=cast("int", position),
            publication_id=publication_id,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("cursor fields are invalid") from error
    if encode_discovery_cursor(cursor) != value:
        raise ValueError("cursor is not canonically encoded")
    return cursor


def encode_facet_cursor(cursor: CatalogFacetCursor) -> str:
    return _encode(
        [
            "facet",
            cursor.revision,
            cursor.query_sha256,
            cursor.facet.value,
            cursor.position,
            cursor.value_sha256,
        ]
    )


def decode_facet_cursor(value: str) -> CatalogFacetCursor:
    decoded = _decode(value)
    if len(decoded) != 6 or decoded[0] != "facet":
        raise ValueError("cursor is not a facet cursor")
    _, revision, query_sha256, facet_value, position, value_sha256 = decoded
    if (
        not _valid_position(revision, position)
        or not _valid_sha256(query_sha256)
        or not _valid_sha256(value_sha256)
        or not isinstance(facet_value, str)
    ):
        raise ValueError("cursor fields are invalid")
    try:
        cursor = CatalogFacetCursor(
            revision=cast("int", revision),
            query_sha256=cast("str", query_sha256),
            facet=CatalogFacetKind(facet_value),
            position=cast("int", position),
            value_sha256=cast("str", value_sha256),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("cursor fields are invalid") from error
    if encode_facet_cursor(cursor) != value:
        raise ValueError("cursor is not canonically encoded")
    return cursor
