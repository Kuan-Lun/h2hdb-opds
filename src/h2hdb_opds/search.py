__all__ = [
    "SEARCH_QUERY_MAXIMUM_BYTES",
    "has_search_query",
    "parse_search_query",
    "render_search_query",
]

import json
import re
from datetime import UTC, date, datetime, time, timedelta

from h2hdb import (
    CatalogDiscoveryQuery,
    CatalogPageCountRange,
    CatalogSubjectFilter,
    CatalogTimestampRange,
)

_FIELD_PREFIX = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):")
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_SCALAR_FIELDS = frozenset({"gid", "uploaded", "downloaded", "pages"})
# Six bytes per JSON-escaped byte covers 16 x (128-byte namespace + 1024-byte
# value), two 1024-byte text fields, scalar fields and all DSL delimiters.
SEARCH_QUERY_MAXIMUM_BYTES = 128 * 1024
_MAXIMUM_CLAUSES = 32


def _validate_size(value: str) -> None:
    if len(value.encode("utf-8", errors="strict")) > SEARCH_QUERY_MAXIMUM_BYTES:
        raise ValueError("search query exceeds 128 KiB of UTF-8 transport bytes")


def _tokens(value: str) -> list[str]:
    """Split outside JSON-style quotes without discarding literal provenance."""
    tokens: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for position, character in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif not quoted and character.isspace():
            if position > start:
                tokens.append(value[start:position])
            start = position + 1
    if quoted:
        raise ValueError("search query has an unterminated quoted value")
    if start < len(value):
        tokens.append(value[start:])
    if len(tokens) > _MAXIMUM_CLAUSES:
        raise ValueError("search query exceeds 32 clauses")
    return tokens


def _value(raw: str, *, allow_whitespace: bool = False) -> str:
    if not raw:
        raise ValueError("search field is missing its value")
    if raw.startswith('"'):
        try:
            decoded: object = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("search query has an invalid quoted value") from error
        if not isinstance(decoded, str):
            raise ValueError("quoted search value must be a string")
        selected = decoded
    else:
        if '"' in raw:
            raise ValueError("quotes must enclose the complete search value")
        selected = raw
    if not selected or (not allow_whitespace and not selected.strip()):
        raise ValueError("search value must not be blank")
    return selected


def _tag(raw: str) -> CatalogSubjectFilter:
    quoted = False
    escaped = False
    for position, character in enumerate(raw):
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == ":" and not quoted:
            return CatalogSubjectFilter(
                namespace=_value(raw[:position], allow_whitespace=True),
                value=_value(raw[position + 1 :], allow_whitespace=True),
            )
    raise ValueError("tag requires a namespace and value: tag:namespace:value")


def _integer(value: str, *, field: str) -> int:
    if _INTEGER.fullmatch(value) is None:
        raise ValueError(f"{field} requires a canonical nonnegative integer")
    return int(value)


def _range_values(value: str, *, field: str) -> tuple[str, str]:
    if ".." not in value:
        return value, value
    parts = value.split("..")
    if len(parts) != 2 or not any(parts):
        raise ValueError(f"{field} requires a value or a nonempty lower..upper range")
    return parts[0], parts[1]


def _calendar_date(value: str, *, field: str) -> date:
    if _DATE.fullmatch(value) is None:
        raise ValueError(f"{field} dates must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} contains an invalid calendar date") from error


def _timestamp_range(value: str | None, *, field: str) -> CatalogTimestampRange | None:
    if value is None:
        return None
    lower, upper = _range_values(value, field=field)
    start = (
        None
        if not lower
        else datetime.combine(_calendar_date(lower, field=field), time(), UTC)
    )
    try:
        end = (
            None
            if not upper
            else datetime.combine(
                _calendar_date(upper, field=field) + timedelta(days=1), time(), UTC
            )
        )
    except OverflowError as error:
        raise ValueError(
            f"{field} upper date has no representable following day"
        ) from error
    return CatalogTimestampRange(start=start, end=end)


def _page_range(value: str | None) -> CatalogPageCountRange | None:
    if value is None:
        return None
    lower, upper = _range_values(value, field="pages")
    return CatalogPageCountRange(
        minimum=None if not lower else _integer(lower, field="pages"),
        maximum=None if not upper else _integer(upper, field="pages"),
    )


def parse_search_query(value: str) -> CatalogDiscoveryQuery:
    _validate_size(value)
    words: list[str] = []
    titles: list[str] = []
    subjects: list[CatalogSubjectFilter] = []
    scalars: dict[str, str] = {}
    for token in _tokens(value):
        prefix = _FIELD_PREFIX.match(token)
        if prefix is None:
            words.append(_value(token))
            continue
        field = prefix[1]
        raw = token[prefix.end() :]
        if field == "title":
            titles.append(_value(raw))
        elif field == "tag":
            subjects.append(_tag(raw))
        elif field in _SCALAR_FIELDS:
            if field in scalars:
                raise ValueError(f"{field} may only appear once")
            scalars[field] = _value(raw)
        else:
            raise ValueError(f"unknown search field: {field}")
    query = CatalogDiscoveryQuery(
        search=" ".join(" ".join(words).split()) or None,
        title=" ".join(" ".join(titles).split()) or None,
        gid=None if "gid" not in scalars else _integer(scalars["gid"], field="gid"),
        subjects=tuple(dict.fromkeys(subjects)),
        uploaded=_timestamp_range(scalars.get("uploaded"), field="uploaded"),
        downloaded=_timestamp_range(scalars.get("downloaded"), field="downloaded"),
        pages=_page_range(scalars.get("pages")),
    )
    if not has_search_query(query):
        raise ValueError("search query must contain text or a field condition")
    render_search_query(query)
    return query


def has_search_query(query: CatalogDiscoveryQuery) -> bool:
    return any(
        (
            query.search is not None,
            query.title is not None,
            query.gid is not None,
            bool(query.subjects),
            query.uploaded is not None,
            query.downloaded is not None,
            query.pages is not None,
        )
    )


def _quoted(value: str) -> str:
    if (
        value
        and not any(character.isspace() or ord(character) < 32 for character in value)
        and not any(character in value for character in '\\":')
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def _render_range(lower: str, upper: str) -> str:
    return lower if lower == upper else f"{lower}..{upper}"


def _render_timestamp_range(value: CatalogTimestampRange) -> str:
    for bound in (value.start, value.end):
        if bound is not None and (
            bound.utcoffset() != timedelta(0) or bound.time() != time()
        ):
            raise ValueError("date search bounds must be UTC midnights")
    lower = "" if value.start is None else value.start.date().isoformat()
    upper = (
        "" if value.end is None else (value.end.date() - timedelta(days=1)).isoformat()
    )
    return _render_range(lower, upper)


def render_search_query(query: CatalogDiscoveryQuery) -> str | None:
    clauses: list[str] = []
    if query.search is not None:
        clauses.append(_quoted(query.search))
    if query.title is not None:
        clauses.append(f"title:{_quoted(query.title)}")
    if query.gid is not None:
        clauses.append(f"gid:{query.gid}")
    clauses.extend(
        f"tag:{_quoted(subject.namespace)}:{_quoted(subject.value)}"
        for subject in query.subjects
    )
    for field, timestamp_range in (
        ("uploaded", query.uploaded),
        ("downloaded", query.downloaded),
    ):
        if timestamp_range is not None:
            clauses.append(f"{field}:{_render_timestamp_range(timestamp_range)}")
    if query.pages is not None:
        lower = "" if query.pages.minimum is None else str(query.pages.minimum)
        upper = "" if query.pages.maximum is None else str(query.pages.maximum)
        clauses.append(f"pages:{_render_range(lower, upper)}")
    if not clauses:
        return None
    rendered = " ".join(clauses)
    _validate_size(rendered)
    return rendered
