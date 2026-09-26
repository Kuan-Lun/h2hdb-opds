from dataclasses import replace
from datetime import UTC, datetime
from xml.etree import ElementTree

import pytest

from h2hdb_opds import OPDSConfig, create_app
from h2hdb_opds.atom import ATOM_NAMESPACE, DC_TERMS_NAMESPACE

from .fakes import CatalogFixture
from .http_client import app_client


@pytest.mark.parametrize("prefix", ("/opds/v1.2", "/opds/v2"))
async def test_same_gid_uses_current_publication_timestamp_after_revision_changes(
    catalog_fixture: CatalogFixture,
    opds_config: OPDSConfig,
    prefix: str,
) -> None:
    catalog = catalog_fixture.catalog
    original = catalog.publications[0]
    modified = datetime(2026, 8, 5, 14, tzinfo=UTC)
    app = create_app(opds_config, catalog)
    async with app_client(app) as client:
        # A corrected upstream timestamp may move backwards for the same GID.
        # Revision, download and modified times deliberately differ from it.
        for revision, uploaded, expected in (
            (7, datetime(2018, 7, 3, 14, 41, tzinfo=UTC), "2018-07-03T14:41:00Z"),
            (8, datetime(2018, 7, 3, 14, 37, tzinfo=UTC), "2018-07-03T14:37:00Z"),
        ):
            catalog.revision = replace(
                catalog.revision,
                revision=revision,
                published_at=datetime(2026, 8, 5, 16, tzinfo=UTC),
            )
            catalog.publications = (
                replace(original, published_at=uploaded, modified_at=modified),
                *catalog.publications[1:],
            )
            feed = await client.get(
                f"{prefix}/publications", params={"revision": revision}
            )
            detail = await client.get(
                f"{prefix}/publications/{original.publication_id}",
                params={"revision": revision},
            )
            assert feed.status_code == detail.status_code == 200
            assert feed.headers["cache-control"] == "no-store"
            assert detail.headers["cache-control"] == "no-store"
            if prefix == "/opds/v2":
                entry = next(
                    item
                    for item in feed.json()["publications"]
                    if item["metadata"]["identifier"] == original.publication_id
                )
                for document in (entry, detail.json()):
                    assert document["metadata"]["published"] == expected
                    assert document["metadata"]["modified"] == "2026-08-05T14:00:00Z"
            else:
                entry_xml = next(
                    entry
                    for entry in ElementTree.fromstring(feed.content).findall(
                        f"{{{ATOM_NAMESPACE}}}entry"
                    )
                    if entry.findtext(f"{{{ATOM_NAMESPACE}}}id")
                    == original.publication_id
                )
                for document_xml in (entry_xml, ElementTree.fromstring(detail.content)):
                    assert (
                        document_xml.findtext(f"{{{DC_TERMS_NAMESPACE}}}issued")
                        == expected
                    )
                    assert (
                        document_xml.findtext(f"{{{ATOM_NAMESPACE}}}updated")
                        == "2026-08-05T14:00:00Z"
                    )
        stale = await client.get(
            f"{prefix}/publications/{original.publication_id}",
            params={"revision": 7},
        )
        assert stale.status_code == 404
        assert stale.headers["cache-control"] == "no-store"
