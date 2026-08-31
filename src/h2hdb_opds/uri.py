__all__ = ["optional_uri"]

import re
from urllib.parse import urlsplit

_UNRESERVED = r"A-Za-z0-9._~\-"
_PERCENT_ENCODED = r"%[0-9A-Fa-f]{2}"
_SUB_DELIMITERS = r"!$&'()*+,;="
_PCHAR = rf"(?:[{_UNRESERVED}{_SUB_DELIMITERS}:@]|{_PERCENT_ENCODED})"
_AUTHORITY_CHAR = rf"(?:[{_UNRESERVED}{_SUB_DELIMITERS}:@\[\]]|{_PERCENT_ENCODED})"
_QUERY_OR_FRAGMENT_CHAR = rf"(?:{_PCHAR}|[/?])"
_ABSOLUTE_URI = re.compile(
    rf"[A-Za-z][A-Za-z0-9+.-]*:"
    rf"(?:"
    rf"//{_AUTHORITY_CHAR}*(?:/{_PCHAR}*)*"
    rf"|/(?:{_PCHAR}+(?:/{_PCHAR}*)*)?"
    rf"|{_PCHAR}+(?:/{_PCHAR}*)*"
    rf")"
    rf"(?:\?{_QUERY_OR_FRAGMENT_CHAR}*)?"
    rf"(?:#{_QUERY_OR_FRAGMENT_CHAR}*)?",
    re.ASCII,
)


def optional_uri(value: str | None) -> str | None:
    """Return one exact valid absolute ASCII URI, otherwise omit the value.

    Catalog subject schemes are optional metadata.  They must never make an
    otherwise valid publication unserializable, and a loose ``urlsplit`` scheme
    check is insufficient for the JSON Schema ``uri`` format and Atom's URI
    datatype.  This deliberately validates without normalizing the published
    bytes.
    """
    if value is None:
        return None
    if not value:
        return None
    try:
        value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        return None
    if _ABSOLUTE_URI.fullmatch(value) is None:
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    return value if parsed.scheme else None
