__all__ = ["decode_artifact_cursor", "encode_artifact_cursor"]

import base64
import binascii
import json
import re

from h2hdb import CatalogArtifactCursor

_CURSOR_FORMAT = 1
_INT63_MAX = (1 << 63) - 1
_MAX_CURSOR_LENGTH = 1024
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def encode_artifact_cursor(cursor: CatalogArtifactCursor) -> str:
    payload = json.dumps(
        [
            _CURSOR_FORMAT,
            cursor.revision,
            cursor.position,
            cursor.publication_id,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_artifact_cursor(value: str) -> CatalogArtifactCursor:
    if len(value) > _MAX_CURSOR_LENGTH or _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError("cursor is not a canonical URL-safe token")
    padding = "=" * (-len(value) % 4)
    try:
        payload = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        decoded = json.loads(payload)
    except (UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as error:
        raise ValueError("cursor is not a valid encoded value") from error
    if (
        not isinstance(decoded, list)
        or len(decoded) != 4
        or decoded[0] != _CURSOR_FORMAT
    ):
        raise ValueError("cursor uses an unsupported format")
    _, revision, position, publication_id = decoded
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 1 <= revision <= _INT63_MAX
        or isinstance(position, bool)
        or not isinstance(position, int)
        or not 0 <= position <= _INT63_MAX
        or not isinstance(publication_id, str)
        or not publication_id
    ):
        raise ValueError("cursor fields are invalid")
    try:
        cursor = CatalogArtifactCursor(
            revision=revision,
            position=position,
            publication_id=publication_id,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("cursor fields are invalid") from error
    if encode_artifact_cursor(cursor) != value:
        raise ValueError("cursor is not canonically encoded")
    return cursor
