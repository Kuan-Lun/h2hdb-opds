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
        'language:chinese "名:稱":"a  b\\"c\\\\d" '
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
        ('"language:chinese"', CatalogDiscoveryQuery(search="language:chinese")),
        ('"A  B"', CatalogDiscoveryQuery(search="A B")),
        ("title:Alpha title:Beta", CatalogDiscoveryQuery(title="Alpha Beta")),
        (
            'f:fantasy "f":"fantasy"',
            CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter("f", "fantasy"),)),
        ),
        (
            '" ":"  "',
            CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter(" ", "  "),)),
        ),
        (
            "unknown:value",
            CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter("unknown", "value"),)),
        ),
        (
            "語言:中文 123:456 artist-name:alice",
            CatalogDiscoveryQuery(
                subjects=(
                    CatalogSubjectFilter("語言", "中文"),
                    CatalogSubjectFilter("123", "456"),
                    CatalogSubjectFilter("artist-name", "alice"),
                )
            ),
        ),
        (
            'artist:name:with:colons "name:space":value',
            CatalogDiscoveryQuery(
                subjects=(
                    CatalogSubjectFilter("artist", "name:with:colons"),
                    CatalogSubjectFilter("name:space", "value"),
                )
            ),
        ),
        (
            '"tag":"language:chinese" "title":foo title:bar',
            CatalogDiscoveryQuery(
                title="bar",
                subjects=(
                    CatalogSubjectFilter("tag", "language:chinese"),
                    CatalogSubjectFilter("title", "foo"),
                ),
            ),
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


def test_smart_quotes_group_every_field_and_render_ascii_quotes() -> None:
    query = parse_search_query(
        "“不知火 花” title:“Cobalt Gallery” gid:“1834943” "
        "female:“mind control” “名:稱”:“a  b” “title”:“source tag” "
        "uploaded:“2026-09-01..2026-09-05” downloaded:“2026-09-06” pages:“40..200”"
    )
    assert query == CatalogDiscoveryQuery(
        search="不知火 花",
        title="Cobalt Gallery",
        gid=1834943,
        subjects=(
            CatalogSubjectFilter("female", "mind control"),
            CatalogSubjectFilter("title", "source tag"),
            CatalogSubjectFilter("名:稱", "a  b"),
        ),
        uploaded=CatalogTimestampRange(
            datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 6, tzinfo=UTC)
        ),
        downloaded=CatalogTimestampRange(
            datetime(2026, 9, 6, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
        ),
        pages=CatalogPageCountRange(40, 200),
    )
    rendered = render_search_query(query)
    assert rendered is not None
    assert "“" not in rendered and "”" not in rendered
    assert 'female:"mind control"' in rendered
    assert '"title":"source tag"' in rendered
    assert parse_search_query(rendered) == query


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("“female:mind control”", CatalogDiscoveryQuery(search="female:mind control")),
        (
            '“female”:"mind control" "female":“mind control”',
            CatalogDiscoveryQuery(
                subjects=(CatalogSubjectFilter("female", "mind control"),)
            ),
        ),
        (
            "“ ”:“  ”",
            CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter(" ", "  "),)),
        ),
        (
            "“123”:“中文”",
            CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter("123", "中文"),)),
        ),
        (
            r"female:“a\"b\\c\u201d”",
            CatalogDiscoveryQuery(
                subjects=(CatalogSubjectFilter("female", 'a"b\\c”'),)
            ),
        ),
        (
            r"“\u201cname\u201d”:“\u201cmind\u201d”",
            CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter("“name”", "“mind”"),)),
        ),
    ),
)
def test_smart_quotes_preserve_json_escapes_and_exact_subjects(
    text: str, expected: CatalogDiscoveryQuery
) -> None:
    query = parse_search_query(text)
    assert query == expected
    rendered = render_search_query(query)
    assert rendered is not None
    assert parse_search_query(rendered) == expected


def test_literal_smart_quotes_round_trip_without_data_changes() -> None:
    canonical = '"“literal”" title:"“title”" female:"“mind”" "“namespace”":"value”"'
    query = parse_search_query(canonical)
    assert query.search == "“literal”"
    assert query.title == "“title”"
    assert query.subjects == (
        CatalogSubjectFilter("female", "“mind”"),
        CatalogSubjectFilter("“namespace”", "value”"),
    )
    assert render_search_query(query) == canonical
    assert parse_search_query(canonical) == query


@pytest.mark.parametrize(
    "text",
    (
        "female:“mind control",
        "female:”mind control“",
        'female:“mind control"',
        'female:"mind control”',
        "female:mind”",
        "female:mi“nd”",
        "female:“mind”suffix",
        "“female”suffix:value",
        '“female":value',
        '"female”:value',
        'female:“mind "control"”',
        r"female:“bad\”escape”",
        r"female:“bad\q”",
        "female:“”",
        "“”:value",
        "title:“  ”",
        "tag:female:“mind control”",
    ),
)
def test_search_rejects_unpaired_mixed_or_malformed_smart_quotes(text: str) -> None:
    with pytest.raises(ValueError):
        parse_search_query(text)


def test_smart_quote_grouping_retains_clause_subject_and_transport_limits() -> None:
    assert (
        len(parse_search_query(" ".join(["female:“mind control”"] * 32)).subjects) == 1
    )
    assert (
        len(
            parse_search_query(
                " ".join(f"f:“value {index}”" for index in range(16))
            ).subjects
        )
        == 16
    )
    namespace = "界" * 42 + "ab"
    value = "中" * 341 + "a"
    assert parse_search_query(f"“{namespace}”:“{value}”").subjects == (
        CatalogSubjectFilter(namespace, value),
    )
    for invalid in (
        " ".join(["female:“mind control”"] * 33),
        " ".join(f"f:“value {index}”" for index in range(17)),
        f"“{namespace}x”:“{value}”",
        f"“{namespace}”:“{value}x”",
    ):
        with pytest.raises(ValueError):
            parse_search_query(invalid)
    with pytest.raises(ValueError, match="128 KiB"):
        parse_search_query("female:“" + "x" * SEARCH_QUERY_MAXIMUM_BYTES + "”")


@pytest.mark.parametrize(
    "namespace", ("tag", "title", "gid", "uploaded", "downloaded", "pages")
)
def test_reserved_subject_namespaces_require_and_retain_quotes(namespace: str) -> None:
    query = CatalogDiscoveryQuery(subjects=(CatalogSubjectFilter(namespace, "value"),))
    canonical = f'"{namespace}":value'
    assert render_search_query(query) == canonical
    assert parse_search_query(canonical) == query


def test_subject_namespaces_and_values_preserve_exact_bytes_and_distinct_case() -> None:
    query = parse_search_query(
        '"Title":" É  " "title":" É  " "a\\"b\\\\c":"x\\ty" "tag":"language:chinese"'
    )
    assert query.subjects == (
        CatalogSubjectFilter("Title", " É  "),
        CatalogSubjectFilter('a"b\\c', "x\ty"),
        CatalogSubjectFilter("tag", "language:chinese"),
        CatalogSubjectFilter("title", " É  "),
    )
    assert parse_search_query(render_search_query(query) or "") == query


def test_subject_namespace_and_value_utf8_bounds() -> None:
    namespace = "界" * 42 + "ab"
    value = "中" * 341 + "a"
    text = f"{namespace}:{value}"
    assert parse_search_query(text).subjects == (
        CatalogSubjectFilter(namespace, value),
    )
    for invalid in (f"{namespace}x:{value}", f"{namespace}:{value}x"):
        with pytest.raises(ValueError):
            parse_search_query(invalid)


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
        ":value",
        '"":value',
        "artist:",
        'artist:""',
        'art"ist":value',
        '"artist"suffix:value',
        '"artist":ab"cd"',
        '"bad\\q":value',
        "gid:0",
        "gid:01",
        "gid:\uff11\uff12\uff13",
        "gid:9223372036854775808",
        "gid:1 gid:1",
        "tag:artist",
        "tag:language:chinese",
        'tag:"language":"chinese"',
        "tag:value:with:colons",
        "tag:",
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
    assert len(parse_search_query(" ".join(["f:one"] * 32)).subjects) == 1
    for text in (
        " ".join(["same"] * 33),
        " ".join(f"word{index}" for index in range(17)),
        " ".join(f"word{index}" for index in range(8))
        + ' title:"'
        + " ".join(f"word{index}" for index in range(9))
        + '"',
        " ".join(f"f:value{index}" for index in range(17)),
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
        search="title:Alpha gid:1001 f:fantasy pages:40..200 uploaded:2026-09-05",
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
