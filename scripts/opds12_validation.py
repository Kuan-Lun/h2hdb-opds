"""Shared, offline OPDS 1.2 validation with narrow OPDS-PSE semantics."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from lxml import etree

ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
PSE_NAMESPACE = "http://vaemendis.net/opds-pse/ns"
PSE_STREAM_REL = "http://vaemendis.net/opds-pse/stream"
PSE_PAGE_NUMBER_TOKEN = "{pageNumber}"
PSE_PAGE_COUNT_MAXIMUM = 4096
_PSE_PAGE_COUNT_MAXIMUM_TEXT = "4096"

_ATOM_ENTRY = f"{{{ATOM_NAMESPACE}}}entry"
_ATOM_LINK = f"{{{ATOM_NAMESPACE}}}link"
_PSE_COUNT = f"{{{PSE_NAMESPACE}}}count"
_CANONICAL_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*", re.ASCII)


class OPDS12ValidationError(ValueError):
    """An Atom document failed schema or OPDS-PSE semantic validation."""


def secure_xml_parser() -> etree.XMLParser:
    """Return the common parser used for schemas and untrusted corpus XML."""
    return etree.XMLParser(
        load_dtd=False,
        no_network=True,
        resolve_entities=False,
        huge_tree=False,
    )


def load_relaxng(path: Path) -> etree.RelaxNG:
    """Compile a vendored RELAX NG schema without external resolution."""
    return etree.RelaxNG(etree.parse(path, secure_xml_parser()))


def parse_document_bytes(document: bytes) -> etree._ElementTree:
    """Securely parse one in-memory XML document."""
    return etree.ElementTree(etree.fromstring(document, secure_xml_parser()))


def parse_document_path(path: str) -> etree._ElementTree:
    """Securely parse one XML document from a path."""
    return etree.parse(path, secure_xml_parser())


def _pse_attributes(element: etree._Element) -> set[str]:
    selected: set[str] = set()
    for name in element.attrib:
        if not isinstance(name, str):
            raise OPDS12ValidationError("XML attribute names must be text")
        if etree.QName(name).namespace == PSE_NAMESPACE:
            selected.add(name)
    return selected


def validation_copy(document: etree._ElementTree) -> etree._ElementTree:
    """Build the schema-validation copy after strict PSE semantic checks.

    The official Atom URI datatype correctly rejects braces. OPDS-PSE requires
    one literal ``{pageNumber}`` token, so only a semantically valid stream link
    has that token replaced in this copy. The source document is never changed.
    """
    transformed = copy.deepcopy(document)
    pse_entries: set[etree._Element] = set()
    for element in transformed.iter():
        tag: object = element.tag
        if not isinstance(tag, str):
            continue
        if etree.QName(tag).namespace == PSE_NAMESPACE:
            raise OPDS12ValidationError("OPDS-PSE elements are not supported")

        relation = element.get("rel")
        href = element.get("href")
        pse_attributes = _pse_attributes(element)
        is_stream_link = tag == _ATOM_LINK and relation == PSE_STREAM_REL
        if not is_stream_link:
            if relation == PSE_STREAM_REL:
                raise OPDS12ValidationError(
                    "an OPDS-PSE stream relation requires an Atom link"
                )
            if href is not None and ("{" in href or "}" in href):
                raise OPDS12ValidationError(
                    "braces are allowed only in an OPDS-PSE stream link"
                )
            if pse_attributes:
                raise OPDS12ValidationError(
                    "OPDS-PSE attributes require the OPDS-PSE stream relation"
                )
            continue

        link = element
        if href is None:
            raise OPDS12ValidationError(
                "an OPDS-PSE stream href must contain exactly one {pageNumber} token"
            )
        parent = link.getparent()
        if parent is None or parent.tag != _ATOM_ENTRY:
            raise OPDS12ValidationError(
                "an OPDS-PSE stream link must be a direct child of an Atom entry"
            )
        if parent in pse_entries:
            raise OPDS12ValidationError(
                "an Atom entry must not contain multiple OPDS-PSE stream links"
            )
        pse_entries.add(parent)

        if link.get("type") != "image/jpeg":
            raise OPDS12ValidationError(
                "an OPDS-PSE stream link must have type image/jpeg"
            )
        if pse_attributes != {_PSE_COUNT}:
            raise OPDS12ValidationError(
                "an OPDS-PSE stream link must have only the PSE count attribute"
            )
        count_text = link.get(_PSE_COUNT)
        if (
            count_text is None
            or _CANONICAL_POSITIVE_DECIMAL.fullmatch(count_text) is None
            or len(count_text) > 4
            or (len(count_text) == 4 and count_text > _PSE_PAGE_COUNT_MAXIMUM_TEXT)
        ):
            raise OPDS12ValidationError(
                "OPDS-PSE count must be a canonical integer from 1 through 4096"
            )
        if href.count(PSE_PAGE_NUMBER_TOKEN) != 1:
            raise OPDS12ValidationError(
                "an OPDS-PSE stream href must contain exactly one {pageNumber} token"
            )
        remainder = href.replace(PSE_PAGE_NUMBER_TOKEN, "", 1)
        if "{" in remainder or "}" in remainder:
            raise OPDS12ValidationError(
                "an OPDS-PSE stream href contains an unsupported brace token"
            )
        link.set("href", href.replace(PSE_PAGE_NUMBER_TOKEN, "0", 1))
    return transformed


def validate_document(
    document: etree._ElementTree,
    schema: etree.RelaxNG,
) -> None:
    """Validate one document against PSE semantics and the strict Atom RNG."""
    transformed = validation_copy(document)
    if schema.validate(transformed):
        return
    details = "\n".join(f"  {entry}" for entry in schema.error_log)
    raise OPDS12ValidationError(
        "OPDS 1.2 RELAX NG validation failed" + ("" if not details else f"\n{details}")
    )
