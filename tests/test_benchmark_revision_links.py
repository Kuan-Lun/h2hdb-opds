import pytest

from benchmarks.opds_sqlite_scalability import _validate_revision_links


def _document(home: str, next_page: str) -> dict[str, object]:
    return {
        "links": [
            {"rel": "start", "href": home},
            {
                "rel": "self",
                "href": "http://benchmark.invalid/opds/v2/publications?revision=1",
            },
            {"rel": "next", "href": next_page},
        ]
    }


def test_sqlite_http_oracle_accepts_stable_home_with_pinned_pagination() -> None:
    _validate_revision_links(
        _document(
            "http://benchmark.invalid/opds/v2",
            "http://benchmark.invalid/opds/v2/publications?revision=1&cursor=next",
        ),
        operation="discovery",
        revision=1,
    )


@pytest.mark.parametrize(
    "home",
    (
        "http://benchmark.invalid/opds/v2?revision=1",
        "http://benchmark.invalid/opds/v2/publications",
        "http://attacker.invalid/opds/v2",
    ),
)
def test_sqlite_http_oracle_rejects_unstable_or_foreign_home(home: str) -> None:
    with pytest.raises(RuntimeError):
        _validate_revision_links(
            _document(
                home,
                "http://benchmark.invalid/opds/v2/publications?revision=1&cursor=next",
            ),
            operation="discovery",
            revision=1,
        )


@pytest.mark.parametrize(
    "query", ("cursor=next", "revision=10", "revision=1&revision=2")
)
def test_sqlite_http_oracle_requires_exact_revision_outside_home(query: str) -> None:
    with pytest.raises(RuntimeError, match="link is not revision pinned"):
        _validate_revision_links(
            _document(
                "http://benchmark.invalid/opds/v2",
                f"http://benchmark.invalid/opds/v2/publications?{query}",
            ),
            operation="discovery",
            revision=1,
        )
