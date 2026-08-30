__all__ = [
    "OPDS_ACQUISITION_REL",
    "OPDS_OPEN_ACCESS_REL",
    "acquisition_relation",
    "publication_gid",
    "publication_identifier",
]

from .config import OPDSConfig

OPDS_ACQUISITION_REL = "http://opds-spec.org/acquisition"
OPDS_OPEN_ACCESS_REL = "http://opds-spec.org/acquisition/open-access"

_PUBLICATION_ID_PREFIX = "urn:h2h:gallery:"
_INT63_MAX = (1 << 63) - 1
_INT63_MAX_TEXT = str(_INT63_MAX)


def publication_gid(value: str) -> int:
    """Parse core's exact canonical publication URN at the OPDS boundary."""
    if not isinstance(value, str) or not value.startswith(_PUBLICATION_ID_PREFIX):
        raise ValueError("catalog publication_id is not a canonical H2HDB URI")
    gid_text = value.removeprefix(_PUBLICATION_ID_PREFIX)
    if (
        not gid_text
        or not gid_text.isascii()
        or not gid_text.isdecimal()
        or gid_text.startswith("0")
        or len(gid_text) > len(_INT63_MAX_TEXT)
        or (len(gid_text) == len(_INT63_MAX_TEXT) and gid_text > _INT63_MAX_TEXT)
    ):
        raise ValueError("catalog publication_id is not a canonical H2HDB URI")
    gid = int(gid_text)
    return gid


def publication_identifier(value: str, *, expected_gid: int) -> str:
    """Require a canonical URN congruent with its authoritative positive GID."""
    if (
        isinstance(expected_gid, bool)
        or not isinstance(expected_gid, int)
        or not 1 <= expected_gid <= _INT63_MAX
    ):
        raise ValueError("catalog publication gid is not a positive int63")
    if publication_gid(value) != expected_gid:
        raise ValueError("catalog publication_id disagrees with its gid")
    return value


def acquisition_relation(config: OPDSConfig) -> str:
    """Describe anonymous downloads precisely without overstating protected access."""
    return OPDS_ACQUISITION_REL if config.auth.enabled else OPDS_OPEN_ACCESS_REL
