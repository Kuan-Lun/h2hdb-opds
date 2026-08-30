__all__ = [
    "discovery_query",
    "discovery_query_parameters",
    "query_with_facet",
]

from h2hdb import (
    CatalogContributorFilter,
    CatalogDiscoveryQuery,
    CatalogFacetKind,
    CatalogFacetValue,
    CatalogSubjectFilter,
)


def _normalized_search(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _exact_filter(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    if not value.strip():
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
    normalized_search = _normalized_search(search)
    if required_search_field is not None and normalized_search is None:
        raise ValueError(f"{required_search_field} must not be blank")
    selected_language = _exact_filter(language, field="language")
    selected_tag = _exact_filter(tag, field="tag")
    selected_tag_namespace = _exact_filter(
        tag_namespace,
        field="tag_namespace",
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
    return CatalogDiscoveryQuery(
        search=normalized_search,
        language=selected_language,
        subject=(
            None
            if selected_tag is None or selected_tag_namespace is None
            else CatalogSubjectFilter(
                namespace=selected_tag_namespace,
                value=selected_tag,
            )
        ),
        contributor=contributor_filter,
    )


def discovery_query_parameters(
    query: CatalogDiscoveryQuery,
    *,
    search_parameter: str = "q",
) -> dict[str, str]:
    parameters: dict[str, str] = {}
    if query.search is not None:
        parameters[search_parameter] = query.search
    if query.language is not None:
        parameters["language"] = query.language
    if query.subject is not None:
        parameters["tag"] = query.subject.value
        parameters["tag_namespace"] = query.subject.namespace
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
    subject = query.subject
    contributor = query.contributor
    if facet is CatalogFacetKind.LANGUAGE:
        language = None if value is None else value.value
    elif facet is CatalogFacetKind.SUBJECT:
        if value is not None and value.namespace is None:
            raise ValueError("subject facet value is missing its namespace")
        subject = (
            None
            if value is None
            else CatalogSubjectFilter(
                namespace=value.namespace or "",
                value=value.value,
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
    return CatalogDiscoveryQuery(
        search=query.search,
        language=language,
        subject=subject,
        contributor=contributor,
    )
