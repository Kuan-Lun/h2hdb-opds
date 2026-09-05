__all__ = ["serve_artifact", "serve_image_resource"]

import os
import re
import stat
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from h2hdb import CatalogArtifact, CatalogImageResource

from .catalog_service import CatalogIntegrityError
from .library import LibraryIntegrityError, open_directory_without_symlinks

_BYTE_RANGE_PATTERN = re.compile(r"bytes=[ \t]*(\d*)-(\d*)", re.IGNORECASE)
_CHUNK_SIZE = 64 * 1024
_DEFAULT_DOWNLOAD_NAME = "download"
_MANAGED_FILESYSTEM_CODEC = "managed-filesystem-v2"


class HeadRevalidator(Protocol):
    def __call__(self) -> None: ...


class StorageKey(Protocol):
    @property
    def codec(self) -> str: ...

    @property
    def segments(self) -> tuple[str, ...]: ...


class RangeNotSatisfiable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class SealedByteResource:
    storage_key: StorageKey
    storage_size: int
    extent_offset: int
    content_size: int
    content_sha256: str
    modified_at: datetime
    media_type: str
    download_name: str | None = None


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


def _etag(resource: SealedByteResource) -> str:
    return f'"{resource.content_sha256.casefold()}"'


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
    resource: SealedByteResource,
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
                and _normalized_datetime(resource.modified_at) > parsed
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
                and _normalized_datetime(resource.modified_at) <= parsed
            ):
                return 304
    return None


def _if_range_matches(value: str, resource: SealedByteResource, etag: str) -> bool:
    normalized = value.strip()
    if normalized[:2].casefold() == "w/":
        return False
    if normalized.startswith('"'):
        return normalized == etag
    parsed = _parse_http_date(normalized)
    return parsed is not None and _normalized_datetime(resource.modified_at) == parsed


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


def _read_file(
    source: BinaryIO,
    byte_range: ByteRange,
    *,
    extent_offset: int = 0,
) -> Iterator[bytes]:
    remaining = byte_range.length
    try:
        source.seek(extent_offset + byte_range.start)
        while remaining > 0:
            chunk = source.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                raise OSError("Resource ended before its sealed extent")
            remaining -= len(chunk)
            yield chunk
    finally:
        source.close()


def _download_name(published_name: str) -> str:
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
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _base_headers(resource: SealedByteResource) -> dict[str, str]:
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": _etag(resource),
        "Last-Modified": _http_date(resource.modified_at),
        "X-Content-Type-Options": "nosniff",
    }
    if resource.download_name is not None:
        headers["Content-Disposition"] = _content_disposition(resource.download_name)
    return headers


def _relative_storage_path(root: Path, storage_key: StorageKey) -> Path:
    if storage_key.codec != _MANAGED_FILESYSTEM_CODEC:
        raise HTTPException(status_code=404, detail="Storage codec is unavailable")
    candidate = root.joinpath(*storage_key.segments)
    normalized = Path(os.path.abspath(candidate))
    normalized_root = Path(os.path.abspath(root))
    try:
        relative = normalized.relative_to(normalized_root)
    except ValueError as error:
        raise HTTPException(
            status_code=404,
            detail="Resource is outside the configured library root",
        ) from error
    if not relative.parts:
        raise HTTPException(status_code=404, detail="Resource is unavailable")
    return relative


def _open_without_symlinks(root: Path, storage_key: StorageKey) -> BinaryIO:
    relative = _relative_storage_path(root, storage_key)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = open_directory_without_symlinks(root)
        for component in relative.parts[:-1]:
            next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(relative.name, file_flags, dir_fd=descriptor)
    except OSError as error:
        raise HTTPException(
            status_code=404, detail="Resource is unavailable"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        return os.fdopen(file_descriptor, "rb")
    except BaseException:
        os.close(file_descriptor)
        raise


def _open_sealed_resource(root: Path, resource: SealedByteResource) -> BinaryIO:
    source = _open_without_symlinks(root, resource.storage_key)
    try:
        source_stat = os.fstat(source.fileno())
        if not stat.S_ISREG(source_stat.st_mode):
            raise HTTPException(status_code=404, detail="Resource is unavailable")
    except BaseException:
        source.close()
        raise
    # The activation protocol makes every object beneath ``current`` immutable
    # for the lifetime of its catalog revision. Size is rechecked here to bind
    # the opened inode to the sealed descriptor; hashing on every request would
    # defeat direct streaming. The shared publication lock and head recheck
    # below are the authority that prevents an unsealed replacement.
    if source_stat.st_size != resource.storage_size:
        source.close()
        raise LibraryIntegrityError(
            "Resource no longer matches its published catalog metadata"
        )
    extent_end = resource.extent_offset + resource.content_size
    if resource.extent_offset < 0 or extent_end > resource.storage_size:
        source.close()
        raise CatalogIntegrityError("Resource extent is invalid")
    return source


def _range_unit(value: str) -> str | None:
    unit, separator, _specification = value.partition("=")
    if not separator:
        return None
    return unit.strip().casefold()


def _serve(
    request: Request,
    resource: SealedByteResource,
    *,
    library_root: Path,
    revalidate_head: HeadRevalidator,
) -> Response:
    source = _open_sealed_resource(library_root, resource)
    try:
        revalidate_head()
    except BaseException:
        source.close()
        raise
    headers = _base_headers(resource)
    etag = headers["ETag"]

    precondition_status = _precondition_status(request, resource, etag)
    if precondition_status is not None:
        source.close()
        return Response(status_code=precondition_status, headers=headers)

    selected_range: ByteRange | None = None
    range_header = request.headers.get("Range") if request.method == "GET" else None
    if range_header is not None and _range_unit(range_header) in {None, "bytes"}:
        if_range = request.headers.get("If-Range")
        if if_range is None or _if_range_matches(if_range, resource, etag):
            try:
                selected_range = _parse_range(range_header, resource.content_size)
            except ValueError:
                source.close()
                headers["Content-Range"] = f"bytes */{resource.content_size}"
                return Response(status_code=416, headers=headers)

    if selected_range is None:
        selected_range = ByteRange(0, resource.content_size - 1)
        status_code = 200
    else:
        status_code = 206
        headers["Content-Range"] = (
            f"bytes {selected_range.start}-{selected_range.end}/{resource.content_size}"
        )
    headers["Content-Length"] = str(
        selected_range.length if resource.content_size else 0
    )

    if request.method == "HEAD" or resource.content_size == 0:
        source.close()
        return Response(
            status_code=status_code,
            media_type=resource.media_type,
            headers=headers,
        )
    return StreamingResponse(
        _read_file(source, selected_range, extent_offset=resource.extent_offset),
        status_code=status_code,
        media_type=resource.media_type,
        headers=headers,
    )


def serve_artifact(
    request: Request,
    artifact: CatalogArtifact,
    *,
    library_root: Path,
    revalidate_head: HeadRevalidator,
) -> Response:
    return _serve(
        request,
        SealedByteResource(
            storage_key=artifact.storage_object.key,
            storage_size=artifact.storage_object.size_bytes,
            extent_offset=0,
            content_size=artifact.storage_object.size_bytes,
            content_sha256=artifact.storage_object.sha256,
            modified_at=artifact.storage_object.modified_at,
            media_type=artifact.media_type,
            download_name=artifact.name,
        ),
        library_root=library_root,
        revalidate_head=revalidate_head,
    )


def serve_image_resource(
    request: Request,
    image: CatalogImageResource,
    *,
    library_root: Path,
    revalidate_head: HeadRevalidator,
) -> Response:
    return _serve(
        request,
        SealedByteResource(
            storage_key=image.storage_object.key,
            storage_size=image.storage_object.size_bytes,
            extent_offset=image.extent.offset,
            content_size=image.extent.length,
            content_sha256=image.sha256,
            modified_at=image.storage_object.modified_at,
            media_type=image.media_type,
        ),
        library_root=library_root,
        revalidate_head=revalidate_head,
    )
