__all__ = ["serve_artifact", "validate_artifact_root"]

import hashlib
import os
import re
import secrets
import stat
import tempfile
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from h2hdb import CatalogArtifact

_BYTE_RANGE_PATTERN = re.compile(r"bytes=[ \t]*(\d*)-(\d*)", re.IGNORECASE)
_CHUNK_SIZE = 64 * 1024
_DEFAULT_DOWNLOAD_NAME = "download"
_SPOOL_MEMORY_LIMIT = 8 * 1024 * 1024


class RangeNotSatisfiable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def _normalized_datetime(value: datetime) -> datetime:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return normalized.astimezone(UTC).replace(microsecond=0)


def _http_date(value: datetime) -> str:
    return format_datetime(_normalized_datetime(value), usegmt=True)


def _parse_http_date(value: str) -> datetime | None:
    if value.count(",") > 1:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def _etag(artifact: CatalogArtifact) -> str:
    return f'"{artifact.sha256.casefold()}"'


def _entity_tags(value: str) -> Iterator[str]:
    yield from (candidate.strip() for candidate in value.split(","))


def _if_match_matches(value: str, etag: str) -> bool:
    for candidate in _entity_tags(value):
        if candidate == "*":
            return True
        if candidate[:2].casefold() != "w/" and candidate == etag:
            return True
    return False


def _if_none_match_matches(value: str, etag: str) -> bool:
    for candidate in _entity_tags(value):
        if candidate == "*":
            return True
        if candidate[:2].casefold() == "w/":
            candidate = candidate[2:].lstrip()
        if candidate == etag:
            return True
    return False


def _precondition_status(
    request: Request,
    artifact: CatalogArtifact,
    etag: str,
) -> int | None:
    if_match = request.headers.get("If-Match")
    if if_match is not None:
        if not _if_match_matches(if_match, etag):
            return 412
    else:
        if_unmodified_since = request.headers.get("If-Unmodified-Since")
        if if_unmodified_since is not None:
            parsed = _parse_http_date(if_unmodified_since)
            if (
                parsed is not None
                and _normalized_datetime(artifact.modified_at) > parsed
            ):
                return 412

    if_none_match = request.headers.get("If-None-Match")
    if if_none_match is not None:
        if _if_none_match_matches(if_none_match, etag):
            return 304
    else:
        if_modified_since = request.headers.get("If-Modified-Since")
        if if_modified_since is not None:
            parsed = _parse_http_date(if_modified_since)
            if (
                parsed is not None
                and _normalized_datetime(artifact.modified_at) <= parsed
            ):
                return 304
    return None


def _if_range_matches(value: str, artifact: CatalogArtifact, etag: str) -> bool:
    normalized = value.strip()
    if normalized[:2].casefold() == "w/":
        return False
    if normalized.startswith('"'):
        return normalized == etag
    parsed = _parse_http_date(normalized)
    return parsed is not None and _normalized_datetime(artifact.modified_at) == parsed


def _parse_range(value: str, size: int) -> ByteRange:
    if "," in value or size == 0:
        raise RangeNotSatisfiable
    match = _BYTE_RANGE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise RangeNotSatisfiable
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise RangeNotSatisfiable
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise RangeNotSatisfiable
        return ByteRange(start=max(0, size - suffix_length), end=size - 1)

    start = int(start_text)
    if start >= size:
        raise RangeNotSatisfiable
    if not end_text:
        return ByteRange(start=start, end=size - 1)
    end = int(end_text)
    if end < start:
        raise RangeNotSatisfiable
    return ByteRange(start=start, end=min(end, size - 1))


def _read_file(artifact_file: BinaryIO, byte_range: ByteRange) -> Iterator[bytes]:
    remaining = byte_range.length
    try:
        artifact_file.seek(byte_range.start)
        while remaining > 0:
            chunk = artifact_file.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        artifact_file.close()


def _download_name(published_name: str) -> str:
    """Return a safe leaf name without consulting the storage path."""
    normalized = unicodedata.normalize("NFC", published_name).replace("\\", "/")
    leaf = normalized.rsplit("/", maxsplit=1)[-1]
    leaf = "".join(
        character for character in leaf if character >= " " and character != "\x7f"
    )
    leaf = leaf.strip(" .")
    if leaf in {"", ".", ".."}:
        return _DEFAULT_DOWNLOAD_NAME
    return leaf


def _ascii_download_name(download_name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", download_name)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    ascii_name = "".join(
        character
        for character in ascii_name
        if " " <= character <= "~" and character not in {'"', "\\", "/"}
    ).strip()
    if not ascii_name:
        return _DEFAULT_DOWNLOAD_NAME
    if ascii_name.startswith("."):
        return f"{_DEFAULT_DOWNLOAD_NAME}{ascii_name}"
    return ascii_name


def _content_disposition(published_name: str) -> str:
    download_name = _download_name(published_name)
    fallback = _ascii_download_name(download_name)
    encoded = quote(download_name, safe="")
    return f'attachment; filename="{fallback}"; ' f"filename*=UTF-8''{encoded}"


def _base_headers(artifact: CatalogArtifact) -> dict[str, str]:
    return {
        "Accept-Ranges": "bytes",
        "ETag": _etag(artifact),
        "Last-Modified": _http_date(artifact.modified_at),
        "Content-Disposition": _content_disposition(artifact.name),
    }


def _open_directory_without_symlinks(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    normalized = Path(os.path.abspath(path))
    descriptor = os.open(normalized.anchor, flags)
    try:
        for component in normalized.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def validate_artifact_root(root: Path) -> None:
    try:
        descriptor = _open_directory_without_symlinks(root)
    except OSError as error:
        raise RuntimeError(f"Artifact root must be a real directory: {root}") from error
    os.close(descriptor)


def _relative_artifact_path(root: Path, location: Path) -> Path:
    candidate = location if location.is_absolute() else root / location
    normalized = Path(os.path.abspath(candidate))
    normalized_root = Path(os.path.abspath(root))
    try:
        relative = normalized.relative_to(normalized_root)
    except ValueError as error:
        raise HTTPException(
            status_code=404,
            detail="Artifact is outside the configured artifact root",
        ) from error
    if not relative.parts:
        raise HTTPException(status_code=404, detail="Artifact is unavailable")
    return relative


def _open_without_symlinks(root: Path, location: Path) -> BinaryIO:
    relative = _relative_artifact_path(root, location)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = _open_directory_without_symlinks(root)
        for component in relative.parts[:-1]:
            next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(relative.name, file_flags, dir_fd=descriptor)
    except OSError as error:
        raise HTTPException(
            status_code=404,
            detail="Artifact is unavailable",
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return os.fdopen(file_descriptor, "rb")


def _verified_snapshot(root: Path, artifact: CatalogArtifact) -> BinaryIO:
    source = _open_without_symlinks(root, artifact.location)
    snapshot = cast(
        BinaryIO,
        tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_LIMIT),
    )
    digest = hashlib.sha256()
    size = 0
    try:
        source_stat = os.fstat(source.fileno())
        if not stat.S_ISREG(source_stat.st_mode):
            raise HTTPException(status_code=404, detail="Artifact is unavailable")
        while chunk := source.read(_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
            snapshot.write(chunk)
    except BaseException:
        snapshot.close()
        raise
    finally:
        source.close()

    if size != artifact.size_bytes or not secrets.compare_digest(
        digest.hexdigest().casefold(), artifact.sha256.casefold()
    ):
        snapshot.close()
        raise HTTPException(
            status_code=409,
            detail="Artifact no longer matches its published catalog metadata",
        )
    snapshot.seek(0)
    return snapshot


def _range_unit(value: str) -> str | None:
    unit, separator, _specification = value.partition("=")
    if not separator:
        return None
    return unit.strip().casefold()


def serve_artifact(
    request: Request,
    artifact: CatalogArtifact,
    *,
    artifact_root: Path,
) -> Response:
    artifact_file = _verified_snapshot(artifact_root, artifact)
    headers = _base_headers(artifact)
    etag = headers["ETag"]

    precondition_status = _precondition_status(request, artifact, etag)
    if precondition_status is not None:
        artifact_file.close()
        return Response(status_code=precondition_status, headers=headers)

    selected_range: ByteRange | None = None
    range_header = request.headers.get("Range") if request.method == "GET" else None
    if range_header is not None and _range_unit(range_header) in {None, "bytes"}:
        if_range = request.headers.get("If-Range")
        if if_range is None or _if_range_matches(if_range, artifact, etag):
            try:
                selected_range = _parse_range(range_header, artifact.size_bytes)
            except ValueError:
                artifact_file.close()
                headers["Content-Range"] = f"bytes */{artifact.size_bytes}"
                return Response(status_code=416, headers=headers)

    if selected_range is None:
        selected_range = ByteRange(0, artifact.size_bytes - 1)
        status_code = 200
    else:
        status_code = 206
        headers["Content-Range"] = (
            f"bytes {selected_range.start}-{selected_range.end}/{artifact.size_bytes}"
        )
    headers["Content-Length"] = str(selected_range.length if artifact.size_bytes else 0)

    if request.method == "HEAD" or artifact.size_bytes == 0:
        artifact_file.close()
        return Response(
            status_code=status_code,
            media_type=artifact.media_type,
            headers=headers,
        )
    return StreamingResponse(
        _read_file(artifact_file, selected_range),
        status_code=status_code,
        media_type=artifact.media_type,
        headers=headers,
    )
