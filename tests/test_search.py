from dataclasses import replace
from datetime import UTC, datetime

import pytest
from h2hdb import (
    CatalogContributorFilter,
    CatalogDiscoveryQuery,
    CatalogFacetKind,
    CatalogFacetValue,
    CatalogPageCountRange,
    CatalogSubjectFilter,
    CatalogTimestampRange,
)

from h2hdb_opds.discovery import (
    discovery_query,
    discovery_query_parameters,
    facet_value_is_selected,
    query_with_facet,
)
from h2hdb_opds.search import (
    SEARCH_QUERY_MAXIMUM_BYTES,
    parse_search_query,
    render_search_query,
)


def test_search_compiles_all_fields_and_round_trips_exact_tag_bytes() -> None:
    query = parse_search_query(
        '不知火 title:"Cobalt Gallery" title:Alpha gid:1834943 '
        'tag:language:chinese tag:"名:稱":"a  b\\"c\\\\d" '
        "uploaded:2026-09-01..2026-09-05 downloaded:2026-09-06 pages:40..200"
    )
    assert query == CatalogDiscoveryQuery(
        search="不知火",
        title="Cobalt Gallery Alpha",
        gid=1834943,
        subjects=(
            CatalogSubjectFilter(namespace="language", value="chinese"),
            CatalogSubjectFilter(namespace="名:稱", value='a  b"c\\d'),
        ),
        uploaded=CatalogTimestampRange(
            start=datetime(2026, 9, 1, tzinfo=UTC),
            end=datetime(2026, 9, 6, tzinfo=UTC),
        ),
        downloaded=CatalogTimestampRange(
            start=datetime(2026, 9, 6, tzinfo=UTC),
            end=datetime(2026, 9, 7, tzinfo=UTC),
        ),
        pages=CatalogPageCountRange(minimum=40, maximum=200),
    )
    rendered = render_search_query(query)
    assert rendered is not None
    assert parse_search_query(rendered) == query


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("1834943", CatalogDiscoveryQuery(search="1834943")),
        ('"gid:1834943"', CatalogDiscoveryQuery(search="gid:1834943")),
        ('"unknown:word"', CatalogDiscoveryQuery(search="unknown:word")),
        ('"A  B"', CatalogDiscoveryQuery(search="A B")),
        ("title:Alpha title:Beta", CatalogDiscoveryQuery(title="Alpha Beta")),
        (
            "tag:f:fantasy tag:f:fantasy",
            CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter("f", "fantasy"),)),
        ),
        (
            'tag:" ":"  "',
            CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter(" ", "  "),)),
        ),
        ("pages:0", CatalogDiscoveryQuery(pages=CatalogPageCountRange(0, 0))),
        ("pages:4096", CatalogDiscoveryQuery(pages=CatalogPageCountRange(4096, 4096))),
        ("pages:40..", CatalogDiscoveryQuery(pages=CatalogPageCountRange(40, None))),
        ("pages:..200", CatalogDiscoveryQuery(pages=CatalogPageCountRange(None, 200))),
        (
            "uploaded:2024-02-29..",
            CatalogDiscoveryQuery(
                uploaded=CatalogTimestampRange(datetime(2024, 2, 29, tzinfo=UTC), None)
            ),
        ),
        (
            "downloaded:..2024-02-29",
            CatalogDiscoveryQuery(
                downloaded=CatalogTimestampRange(None, datetime(2024, 3, 1, tzinfo=UTC))
            ),
        ),
    ),
)
def test_search_literal_and_range_boundaries(
    text: str, expected: CatalogDiscoveryQuery
) -> None:
    actual = parse_search_query(text)
    assert actual == expected
    rendered = render_search_query(actual)
    assert rendered is not None
    assert parse_search_query(rendered) == expected


@pytest.mark.parametrize(
    "text",
    (
        "",
        "   ",
        "!!!",
        "title:!!!",
        "title:",
        'title:""',
        'title:"unfinished',
        'title:"bad\\q"',
        'title:ab"cd"',
        "unknown:value",
        "gid:0",
        "gid:01",
        "gid:\uff11\uff12\uff13",
        "gid:9223372036854775808",
        "gid:1 gid:1",
        "tag:artist",
        "tag::name",
        "tag:artist:",
        'tag:artist:""',
        "pages:..",
        "pages:1..2..3",
        "pages:200..40",
        "pages:4097",
        "pages:-1",
        "pages:01",
        "pages:40 pages:200",
        "uploaded:2026-2-03",
        "uploaded:2025-02-29",
        "uploaded:2026-09-06..2026-09-05",
        "uploaded:1969-12-31",
        "uploaded:9999-12-31",
        "downloaded:..",
        "downloaded:2026-09-05 downloaded:2026-09-05",
    ),
)
def test_search_rejects_malformed_or_unsearchable_conditions(text: str) -> None:
    with pytest.raises(ValueError):
        parse_search_query(text)


def test_search_enforces_bounded_clauses_lexemes_tags_and_utf8_bytes() -> None:
    assert parse_search_query(" ".join(["same"] * 32)).search is not None
    assert len(parse_search_query(" ".join(["tag:f:one"] * 32)).subjects) == 1
    for text in (
        " ".join(["same"] * 33),
        " ".join(f"word{index}" for index in range(17)),
        " ".join(f"word{index}" for index in range(8))
        + ' title:"'
        + " ".join(f"word{index}" for index in range(9))
        + '"',
        " ".join(f"tag:f:value{index}" for index in range(17)),
        "中" * 400,
    ):
        with pytest.raises(ValueError):
            parse_search_query(text)
    with pytest.raises(ValueError, match="128 KiB"):
        parse_search_query("x" * (SEARCH_QUERY_MAXIMUM_BYTES + 1))


def test_canonical_transport_fits_maximum_core_fields_and_json_expansion() -> None:
    query = CatalogDiscoveryQuery(
        search=" ".join(["abc"] * 256),
        title=" ".join(["xyz"] * 256),
        subjects=tuple(
            CatalogSubjectFilter(
                namespace=f"n{index:02}" + "\x01" * 125,
                value="\x01" * 1023 + "a",
            )
            for index in range(16)
        ),
    )
    rendered = render_search_query(query)
    assert rendered is not None
    assert len(rendered.encode("utf-8")) < SEARCH_QUERY_MAXIMUM_BYTES
    assert parse_search_query(rendered) == query


def test_http_filters_combine_with_dsl_and_facets_replace_only_their_family() -> None:
    query = discovery_query(
        search="title:Alpha gid:1001 tag:f:fantasy pages:40..200 uploaded:2026-09-05",
        language="en",
        tag="a  b",
        tag_namespace="artist",
        contributor="Alice",
        role="artist",
    )
    assert query.subjects == (
        CatalogSubjectFilter("artist", "a  b"),
        CatalogSubjectFilter("f", "fantasy"),
    )
    assert query.language == "en"
    assert query.contributor == CatalogContributorFilter("Alice", "artist")
    parameters = discovery_query_parameters(query, search_parameter="query")
    assert parameters["language"] == "en"
    assert parameters["contributor"] == "Alice"
    assert parameters["role"] == "artist"
    assert parse_search_query(parameters["query"]) == replace(
        query, language=None, contributor=None
    )
    selected = CatalogFacetValue(
        value="fantasy", label="fantasy", publication_count=1, namespace="f"
    )
    assert not facet_value_is_selected(query, CatalogFacetKind.SUBJECT, selected)
    replaced = query_with_facet(query, CatalogFacetKind.SUBJECT, selected)
    assert replaced == replace(query, subjects=(CatalogSubjectFilter("f", "fantasy"),))
    assert facet_value_is_selected(replaced, CatalogFacetKind.SUBJECT, selected)
    assert query_with_facet(query, CatalogFacetKind.SUBJECT, None) == replace(
        query, subjects=()
    )
    assert query_with_facet(query, CatalogFacetKind.LANGUAGE, None) == replace(
        query, language=None
    )
    assert query_with_facet(query, CatalogFacetKind.CONTRIBUTOR, None) == replace(
        query, contributor=None
    )
