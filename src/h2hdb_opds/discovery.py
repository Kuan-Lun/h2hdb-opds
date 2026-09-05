__all__ = [
    "discovery_query",
    "discovery_query_parameters",
    "facet_value_is_selected",
    "query_with_facet",
]

from dataclasses import replace

from h2hdb import (
    CatalogContributorFilter,
    CatalogDiscoveryQuery,
    CatalogFacetKind,
    CatalogFacetValue,
    CatalogSubjectFilter,
)

from .search import parse_search_query, render_search_query


def _exact_filter(
    value: str | None, *, field: str, allow_whitespace: bool = False
) -> str | None:
    if value is None:
        return None
    if not value or (not allow_whitespace and not value.strip()):
        raise ValueError(f"{field} must not be blank")
    if len(value.encode("utf-8", errors="strict")) > 1024:
        raise ValueError(f"{field} exceeds 1024 UTF-8 bytes")
    return value


def discovery_query(
    *,
    search: str | None,
    required_search_field: str | None = None,
    language: str | None,
    tag: str | None,
    tag_namespace: str | None,
    contributor: str | None,
    role: str | None,
) -> CatalogDiscoveryQuery:
    if required_search_field is not None and (search is None or not search.strip()):
        raise ValueError(f"{required_search_field} must not be blank")
    parsed = CatalogDiscoveryQuery() if search is None else parse_search_query(search)
    selected_language = _exact_filter(language, field="language")
    selected_tag = _exact_filter(tag, field="tag", allow_whitespace=True)
    selected_tag_namespace = _exact_filter(
        tag_namespace,
        field="tag_namespace",
        allow_whitespace=True,
    )
    if (selected_tag is None) != (selected_tag_namespace is None):
        raise ValueError("tag and tag_namespace must be provided together")
    selected_contributor = _exact_filter(contributor, field="contributor")
    selected_role = _exact_filter(role, field="role")
    if (selected_contributor is None) != (selected_role is None):
        raise ValueError("contributor and role must be provided together")
    contributor_filter = (
        None
        if selected_contributor is None or selected_role is None
        else CatalogContributorFilter(
            name=selected_contributor,
            role=selected_role,
        )
    )
    subjects = parsed.subjects
    if selected_tag is not None and selected_tag_namespace is not None:
        subjects = (
            *subjects,
            CatalogSubjectFilter(namespace=selected_tag_namespace, value=selected_tag),
        )
    query = replace(
        parsed,
        language=selected_language,
        subjects=tuple(dict.fromkeys(subjects)),
        contributor=contributor_filter,
    )
    render_search_query(query)
    return query


def discovery_query_parameters(
    query: CatalogDiscoveryQuery,
    *,
    search_parameter: str = "q",
) -> dict[str, str]:
    parameters: dict[str, str] = {}
    search = render_search_query(query)
    if search is not None:
        parameters[search_parameter] = search
    if query.language is not None:
        parameters["language"] = query.language
    if query.contributor is not None:
        parameters["contributor"] = query.contributor.name
        parameters["role"] = query.contributor.role
    return parameters


def query_with_facet(
    query: CatalogDiscoveryQuery,
    facet: CatalogFacetKind,
    value: CatalogFacetValue | None,
) -> CatalogDiscoveryQuery:
    language = query.language
    subjects = query.subjects
    contributor = query.contributor
    if facet is CatalogFacetKind.LANGUAGE:
        language = None if value is None else value.value
    elif facet is CatalogFacetKind.SUBJECT:
        if value is not None and value.namespace is None:
            raise ValueError("subject facet value is missing its namespace")
        subjects = (
            ()
            if value is None
            else (
                CatalogSubjectFilter(
                    namespace=value.namespace or "",
                    value=value.value,
                ),
            )
        )
    else:
        contributor = (
            None
            if value is None
            else CatalogContributorFilter(
                name=value.value,
                role=value.role or "contributor",
            )
        )
    return replace(
        query,
        language=language,
        subjects=subjects,
        contributor=contributor,
    )


def facet_value_is_selected(
    query: CatalogDiscoveryQuery,
    facet: CatalogFacetKind,
    value: CatalogFacetValue | None,
) -> bool:
    if facet is CatalogFacetKind.LANGUAGE:
        return query.language == (None if value is None else value.value)
    if facet is CatalogFacetKind.SUBJECT:
        if value is None:
            return not query.subjects
        return len(query.subjects) == 1 and (
            query.subjects[0].namespace == value.namespace
            and query.subjects[0].value == value.value
        )
    if value is None:
        return query.contributor is None
    return query.contributor is not None and (
        query.contributor.name == value.value
        and query.contributor.role == (value.role or "contributor")
    )
