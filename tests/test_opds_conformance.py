import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from h2hdb import CatalogSubject
from lxml import etree

from h2hdb_opds import OPDSConfig, create_app
from scripts.opds12_validation import (
    PSE_NAMESPACE,
    OPDS12ValidationError,
    load_relaxng,
    parse_document_bytes,
    validate_document,
)

from .fakes import CatalogFixture, FakeCatalog
from .http_client import app_client

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_OPDS12_SCHEMA = _REPOSITORY_ROOT / "verification/opds/schemas/opds-1.2/opds.rng"
_OPDS2_VALIDATOR = _REPOSITORY_ROOT / "scripts/validate-opds2.mjs"


def _atom_validator() -> etree.RelaxNG:
    return load_relaxng(_OPDS12_SCHEMA)


def _assert_valid_atom(document: bytes) -> None:
    validate_document(parse_document_bytes(document), _atom_validator())


def _opds2_validation(
    kind: str,
    document: dict[str, Any],
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(_OPDS2_VALIDATOR), kind, "-"],
        input=json.dumps(document),
        text=True,
        capture_output=True,
        check=False,
        cwd=_REPOSITORY_ROOT,
        env=environment,
    )


async def test_every_opds12_runtime_document_matches_official_relax_ng(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    publication_id = catalog_fixture.publications[0].publication_id

    async with app_client(app) as client:
        responses = [
            await client.get("/opds/v1.2/catalog"),
            await client.get("/opds/v1.2/publications"),
            await client.get("/opds/v1.2/search", params={"q": "cobalt"}),
            await client.get("/opds/v1.2/recent/uploaded"),
            await client.get("/opds/v1.2/recent/downloaded"),
            await client.get("/opds/v1.2/facets/language"),
            await client.get(f"/opds/v1.2/publications/{publication_id}"),
        ]

    for response in responses:
        assert response.status_code == 200
        _assert_valid_atom(response.content)


async def test_every_opds2_runtime_document_matches_official_json_schemas(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)
    publication_id = catalog_fixture.publications[0].publication_id

    async with app_client(app) as client:
        feed_responses = [
            await client.get("/opds/v2"),
            await client.get("/opds/v2/publications"),
            await client.get("/opds/v2/search", params={"query": "cobalt"}),
            await client.get("/opds/v2/recent/uploaded"),
            await client.get("/opds/v2/recent/downloaded"),
            await client.get("/opds/v2/facets/language"),
        ]
        publication_response = await client.get(
            f"/opds/v2/publications/{publication_id}"
        )

    for response in feed_responses:
        assert response.status_code == 200
        result = _opds2_validation("feed", response.json())
        assert result.returncode == 0, result.stderr
    result = _opds2_validation("publication", publication_response.json())
    assert result.returncode == 0, result.stderr


async def test_empty_acquisition_catalogs_match_both_official_schemas(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    publications = tuple(
        replace(publication, artifacts=())
        for publication in catalog_fixture.publications
    )
    catalog = FakeCatalog(publications)
    catalog.revision = replace(catalog.revision, artifact_count=0)
    app = create_app(opds_config, catalog)

    async with app_client(app) as client:
        atom = await client.get("/opds/v1.2/publications")
        opds2 = await client.get("/opds/v2/publications")

    _assert_valid_atom(atom.content)
    result = _opds2_validation("feed", opds2.json())
    assert result.returncode == 0, result.stderr


async def test_empty_search_results_match_both_official_schemas(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config, catalog_fixture.catalog)

    async with app_client(app) as client:
        atom = await client.get("/opds/v1.2/search", params={"q": "missing"})
        opds2 = await client.get(
            "/opds/v2/search",
            params={"query": "missing"},
        )

    assert atom.status_code == 200
    _assert_valid_atom(atom.content)
    assert opds2.status_code == 200
    result = _opds2_validation("feed", opds2.json())
    assert result.returncode == 0, result.stderr


async def test_paginated_publication_pages_match_both_official_schemas(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    config = opds_config.model_copy(
        update={"default_page_size": 2, "maximum_page_size": 2}
    )
    app = create_app(config, catalog_fixture.catalog)

    async with app_client(app) as client:
        atom_first = await client.get("/opds/v1.2/publications")
        atom_root = etree.fromstring(atom_first.content)
        atom_next_url = next(
            link.get("href")
            for link in atom_root.findall("{http://www.w3.org/2005/Atom}link")
            if link.get("rel") == "next"
        )
        assert atom_next_url is not None
        atom_next = await client.get(atom_next_url)

        opds2_first = await client.get("/opds/v2/publications")
        opds2_next_url = next(
            link["href"]
            for link in opds2_first.json()["links"]
            if link["rel"] == "next"
        )
        opds2_next = await client.get(opds2_next_url)

    for response in (atom_first, atom_next):
        assert response.status_code == 200
        _assert_valid_atom(response.content)
    for response in (opds2_first, opds2_next):
        assert response.status_code == 200
        result = _opds2_validation("feed", response.json())
        assert result.returncode == 0, result.stderr


async def test_large_facet_pages_match_both_official_schemas(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    subjects = tuple(
        CatalogSubject(
            name=f"tag-{index:03d}",
            scheme="tag",
            code=f"t{index:03d}",
        )
        for index in range(130)
    )
    publication = replace(catalog_fixture.publications[0], subjects=subjects)
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        atom_first = await client.get("/opds/v1.2/publications")
        atom_root = etree.fromstring(atom_first.content)
        atom_more_url = next(
            link.get("href")
            for link in atom_root.findall("{http://www.w3.org/2005/Atom}link")
            if link.get("title") == "More Tag values"
        )
        assert atom_more_url is not None
        atom_next = await client.get(atom_more_url)

        opds2_first = await client.get("/opds/v2/publications")
        tag_facet = next(
            facet
            for facet in opds2_first.json()["facets"]
            if facet["metadata"]["title"] == "Tag"
        )
        opds2_more_url = next(
            link["href"] for link in tag_facet["links"] if link.get("rel") == "next"
        )
        opds2_next = await client.get(opds2_more_url)

    for response in (atom_first, atom_next):
        assert response.status_code == 200
        _assert_valid_atom(response.content)
    for response in (opds2_first, opds2_next):
        assert response.status_code == 200
        result = _opds2_validation("feed", response.json())
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("scheme", "expected"),
    [
        ("urn:h2h:subject:tag", "urn:h2h:subject:tag"),
        (" urn:h2h:subject:tag ", None),
        ("http://[", None),
        ("http://bad space", None),
        ("https://books.example/%not-hex", None),
    ],
)
async def test_optional_subject_uris_are_validated_for_both_protocols(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    scheme: str,
    expected: str | None,
) -> None:
    publication = replace(
        catalog_fixture.publications[0],
        subjects=(CatalogSubject(name="invalid scheme", scheme=scheme, code="bad"),),
    )
    app = create_app(opds_config, FakeCatalog((publication,)))

    async with app_client(app) as client:
        atom = await client.get("/opds/v1.2/publications")
        opds2 = await client.get("/opds/v2/publications")

    assert atom.status_code == 200
    atom_document = parse_document_bytes(atom.content)
    category = atom_document.find(".//{http://www.w3.org/2005/Atom}category")
    assert category is not None
    assert category.get("scheme") == expected
    validate_document(atom_document, _atom_validator())

    assert opds2.status_code == 200
    subject = opds2.json()["publications"][0]["metadata"]["subject"][0]
    assert subject.get("scheme") == expected
    result = _opds2_validation("feed", opds2.json())
    assert result.returncode == 0, result.stderr


def test_official_schema_negative_controls_reject_invalid_documents(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
) -> None:
    invalid_atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'
    atom_validator = _atom_validator()
    assert not atom_validator.validate(etree.fromstring(invalid_atom))
    valid_link = b"""<entry xmlns="http://www.w3.org/2005/Atom">
      <id>urn:test:item</id><title>x</title>
      <updated>2026-01-01T00:00:00Z</updated>
      <author><name>x</name></author>
      <link rel="alternate" href="https://books.example/item" />
    </entry>"""
    invalid_brace_link = valid_link.replace(
        b"https://books.example/item",
        b"https://books.example/{notPage}",
    )
    validate_document(parse_document_bytes(valid_link), atom_validator)
    with pytest.raises(OPDS12ValidationError, match="braces are allowed only"):
        validate_document(parse_document_bytes(invalid_brace_link), atom_validator)

    publication = {
        "metadata": {"title": "Invalid", "identifier": "not a URI"},
        "links": [
            {
                "rel": "http://opds-spec.org/acquisition/open-access",
                "href": "https://books.example/book.cbz",
                "type": "application/vnd.comicbook+zip",
            }
        ],
    }
    invalid_identifier = _opds2_validation("publication", publication)
    assert invalid_identifier.returncode == 1
    assert "format" in invalid_identifier.stderr

    empty_collection = {
        "metadata": {"title": "Invalid"},
        "links": [
            {
                "rel": "self",
                "href": "https://books.example/opds/v2/publications",
                "type": "application/opds+json",
            }
        ],
        "publications": [],
    }
    invalid_empty_collection = _opds2_validation("feed", empty_collection)
    assert invalid_empty_collection.returncode == 1
    assert "minItems" in invalid_empty_collection.stderr


def _valid_pse_entry() -> etree._ElementTree:
    return parse_document_bytes(
        b"""<entry xmlns="http://www.w3.org/2005/Atom"
          xmlns:pse="http://vaemendis.net/opds-pse/ns">
          <id>urn:test:item</id><title>x</title>
          <updated>2026-01-01T00:00:00Z</updated>
          <author><name>x</name></author>
          <link rel="http://vaemendis.net/opds-pse/stream"
            href="https://books.example/pages/{pageNumber}"
            type="image/jpeg" pse:count="1" />
        </entry>"""
    )


def test_pse_validation_transforms_only_a_copy_for_the_strict_rng() -> None:
    document = _valid_pse_entry()
    validator = _atom_validator()

    assert not validator.validate(document)
    validate_document(document, validator)

    link = document.getroot().find("{http://www.w3.org/2005/Atom}link")
    assert link is not None
    assert link.get("href") == "https://books.example/pages/{pageNumber}"


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("href", "https://books.example/pages/{page}", "exactly one"),
        (
            "href",
            "https://books.example/{other}/{pageNumber}",
            "unsupported brace",
        ),
        (
            "href",
            "https://books.example/{pageNumber}/{pageNumber}",
            "exactly one",
        ),
        ("href", "https://books.example/pages/0", "exactly one"),
        ("type", "image/png", "type image/jpeg"),
        ("{http://vaemendis.net/opds-pse/ns}count", "0", "1 through 4096"),
        ("{http://vaemendis.net/opds-pse/ns}count", "01", "1 through 4096"),
        ("{http://vaemendis.net/opds-pse/ns}count", "4097", "1 through 4096"),
        (
            "{http://vaemendis.net/opds-pse/ns}count",
            "9" * 10_000,
            "1 through 4096",
        ),
    ],
)
def test_pse_validation_rejects_invalid_stream_contracts(
    attribute: str,
    value: str,
    message: str,
) -> None:
    document = _valid_pse_entry()
    link = document.getroot().find("{http://www.w3.org/2005/Atom}link")
    assert link is not None
    link.set(attribute, value)

    with pytest.raises(OPDS12ValidationError, match=message):
        validate_document(document, _atom_validator())


@pytest.mark.parametrize("attribute", ["lastRead", "maxWidth"])
def test_pse_validation_rejects_unsupported_pse_attributes(attribute: str) -> None:
    document = _valid_pse_entry()
    link = document.getroot().find("{http://www.w3.org/2005/Atom}link")
    assert link is not None
    link.set(f"{{http://vaemendis.net/opds-pse/ns}}{attribute}", "1")

    with pytest.raises(OPDS12ValidationError, match="only the PSE count"):
        validate_document(document, _atom_validator())


def test_pse_validation_requires_count() -> None:
    document = _valid_pse_entry()
    link = document.getroot().find("{http://www.w3.org/2005/Atom}link")
    assert link is not None
    del link.attrib["{http://vaemendis.net/opds-pse/ns}count"]

    with pytest.raises(OPDS12ValidationError, match="only the PSE count"):
        validate_document(document, _atom_validator())


def test_pse_attributes_require_the_exact_stream_relation() -> None:
    document = _valid_pse_entry()
    link = document.getroot().find("{http://www.w3.org/2005/Atom}link")
    assert link is not None
    link.set("rel", "alternate")
    link.set("href", "https://books.example/pages/0")

    with pytest.raises(OPDS12ValidationError, match="attributes require"):
        validate_document(document, _atom_validator())


@pytest.mark.parametrize(
    "mutation",
    ["entry-attribute", "title-attribute", "pse-element"],
)
def test_pse_namespace_is_allowlisted_across_the_complete_document(
    mutation: str,
) -> None:
    document = _valid_pse_entry()
    root = document.getroot()
    if mutation == "entry-attribute":
        root.set(f"{{{PSE_NAMESPACE}}}lastRead", "1")
    elif mutation == "title-attribute":
        title = root.find("{http://www.w3.org/2005/Atom}title")
        assert title is not None
        title.set(f"{{{PSE_NAMESPACE}}}maxWidth", "1")
    else:
        etree.SubElement(root, f"{{{PSE_NAMESPACE}}}unsupported")

    with pytest.raises(OPDS12ValidationError, match="OPDS-PSE"):
        validate_document(document, _atom_validator())


def test_opds2_validator_fails_when_a_vendored_reference_is_unresolved(
    tmp_path: Path,
) -> None:
    schema_root = tmp_path / "schemas"
    shutil.copytree(
        _REPOSITORY_ROOT / "verification/opds/schemas",
        schema_root,
    )
    missing = (
        schema_root
        / "readium-webpub/schema/extensions/encryption/properties.schema.json"
    )
    missing.unlink()
    environment = os.environ.copy()
    environment["H2HDB_OPDS_SCHEMA_ROOT"] = str(schema_root)

    result = subprocess.run(
        ["node", str(_OPDS2_VALIDATOR), "--check-schemas"],
        text=True,
        capture_output=True,
        check=False,
        cwd=_REPOSITORY_ROOT,
        env=environment,
    )

    assert result.returncode == 2
    assert "can't resolve reference" in result.stderr
