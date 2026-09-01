from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from h2hdb import (
    ByteExtent,
    CatalogArtifact,
    CatalogContributor,
    CatalogCursorError,
    CatalogDiscoveryBundle,
    CatalogDiscoveryCursor,
    CatalogDiscoveryPage,
    CatalogDiscoveryQuery,
    CatalogFacetCursor,
    CatalogFacetKind,
    CatalogFacetPage,
    CatalogFacetValue,
    CatalogImageResource,
    CatalogPublication,
    CatalogPublicationPresentation,
    CatalogRecentOrder,
    CatalogRecentWindow,
    CatalogRevision,
    CatalogRevisionNotFoundError,
    CatalogSubject,
    StorageObjectDescriptor,
    StorageObjectKey,
    catalog_search_field_lexemes,
)

_EMPTY_QUERY = CatalogDiscoveryQuery()
_CATALOG_PAYLOAD = b"0123456789abcdefghijklmnopqrstuvwxyz"


def _catalog_artifact_id(gid: int) -> str:
    return (
        f"urn:h2h:artifact:acquisition:{gid}:sha256:"
        f"{sha256(_CATALOG_PAYLOAD).hexdigest()}"
    )


ALPHA_ARTIFACT_ID = _catalog_artifact_id(1001)
BETA_ARTIFACT_ID = _catalog_artifact_id(1002)
GAMMA_ARTIFACT_ID = _catalog_artifact_id(1003)


class FakeCatalog:
    def __init__(self, publications: tuple[CatalogPublication, ...]) -> None:
        self.publications = publications
        self.revision = CatalogRevision(
            revision=7,
            published_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
            publication_count=len(publications),
            artifact_count=sum(bool(item.artifacts) for item in publications),
        )
        self.revision_lookups: list[int | None] = []
        self.list_calls: list[
            tuple[CatalogDiscoveryQuery, CatalogDiscoveryCursor | None, int]
        ] = []
        self.list_revisions: list[CatalogRevision | int | None] = []
        self.bundle_calls: list[
            tuple[
                CatalogDiscoveryQuery,
                CatalogDiscoveryCursor | None,
                int,
                int,
                CatalogRevision | int | None,
            ]
        ] = []
        self.facet_calls: list[
            tuple[
                CatalogFacetKind,
                CatalogDiscoveryQuery,
                CatalogFacetCursor | None,
                int,
            ]
        ] = []
        self.recent_list_calls: list[
            tuple[CatalogRecentOrder, CatalogRevision | int | None]
        ] = []
        self.publication_revisions: list[CatalogRevision | int | None] = []
        self.artifact_revisions: list[CatalogRevision | int | None] = []
        self.presentation_revisions: list[CatalogRevision | int | None] = []
        self.page_revisions: list[CatalogRevision | int | None] = []
        self.discovery_corruption: str | None = None
        self.recent_corruption: str | None = None

    def add_revision(
        self,
        revision: CatalogRevision,
        publications: tuple[CatalogPublication, ...],
    ) -> None:
        self.revision = revision
        self.publications = publications

    def get_catalog_revision(self, revision: int | None = None) -> CatalogRevision:
        self.revision_lookups.append(revision)
        if revision is not None and revision != self.revision.revision:
            raise CatalogRevisionNotFoundError(revision)
        return self.revision

    def _publications_at(
        self,
        revision: CatalogRevision | int | None,
    ) -> tuple[CatalogPublication, ...]:
        self._revision_at(revision)
        return self.publications

    def _revision_at(
        self,
        revision: CatalogRevision | int | None,
    ) -> CatalogRevision:
        if revision is None:
            return self.revision
        if isinstance(revision, int):
            if revision != self.revision.revision:
                raise CatalogRevisionNotFoundError(revision)
            return self.revision
        if revision.revision != self.revision.revision:
            raise CatalogRevisionNotFoundError(revision.revision)
        return self.revision

    @staticmethod
    def _query_sha256(query: CatalogDiscoveryQuery) -> str:
        return sha256(repr(query).encode()).hexdigest()

    @staticmethod
    def _matches_query(
        publication: CatalogPublication,
        query: CatalogDiscoveryQuery,
    ) -> bool:
        if query.search is not None:
            searchable_lexemes = {
                lexeme
                for field in (
                    publication.title,
                    publication.source_title,
                    *(item.name for item in publication.contributors),
                    *(item.name for item in publication.subjects),
                )
                for lexeme in catalog_search_field_lexemes(field)
            }
            if not set(query.search_lexemes).issubset(searchable_lexemes):
                return False
        if query.language is not None and publication.language != query.language:
            return False
        if query.subject is not None and not any(
            subject.name == query.subject.value
            and subject.code == query.subject.namespace
            for subject in publication.subjects
        ):
            return False
        if query.contributor is not None and not any(
            contributor.name == query.contributor.name
            and contributor.role == query.contributor.role
            for contributor in publication.contributors
        ):
            return False
        return True

    def discover_publications(
        self,
        *,
        query: CatalogDiscoveryQuery = _EMPTY_QUERY,
        after: CatalogDiscoveryCursor | None = None,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogDiscoveryPage:
        assert 1 <= limit <= 128
        self.list_calls.append((query, after, limit))
        self.list_revisions.append(revision)
        selected_revision = self._revision_at(revision)
        query_sha256 = self._query_sha256(query)
        positioned = tuple(
            (position, publication)
            for position, publication in enumerate(self.publications)
            if (
                publication.artifacts
                or self.discovery_corruption == "include-artifactless"
            )
            and self._matches_query(publication, query)
        )
        if after is not None:
            if after.revision != selected_revision.revision:
                raise CatalogRevisionNotFoundError(after.revision)
            if after.query_sha256 != query_sha256:
                raise CatalogCursorError("discovery cursor belongs to another query")
            boundary = next(
                (
                    (position, publication)
                    for position, publication in positioned
                    if position == after.position
                ),
                None,
            )
            if boundary is None or boundary[1].publication_id != after.publication_id:
                raise CatalogCursorError(
                    "discovery cursor does not identify its boundary"
                )
            positioned = tuple(item for item in positioned if item[0] > after.position)
        page_items = positioned[:limit]
        next_cursor = None
        if len(positioned) > limit and page_items:
            last_position, last_publication = page_items[-1]
            next_cursor = CatalogDiscoveryCursor(
                revision=selected_revision.revision,
                query_sha256=query_sha256,
                position=last_position,
                publication_id=last_publication.publication_id,
            )
        return CatalogDiscoveryPage(
            revision=selected_revision,
            publications=tuple(publication for _, publication in page_items),
            next_cursor=next_cursor,
            limit=limit,
            total=(
                selected_revision.publication_count
                if query == CatalogDiscoveryQuery()
                else None
            ),
        )

    def discover_publications_with_facets(
        self,
        *,
        query: CatalogDiscoveryQuery = _EMPTY_QUERY,
        after: CatalogDiscoveryCursor | None = None,
        limit: int = 50,
        facet_limit: int = 128,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogDiscoveryBundle:
        self.bundle_calls.append((query, after, limit, facet_limit, revision))
        page = self.discover_publications(
            query=query,
            after=after,
            limit=limit,
            revision=revision,
        )
        return CatalogDiscoveryBundle(
            page=page,
            facets=tuple(
                self.list_publication_facets(
                    facet=facet,
                    query=query,
                    after=None,
                    limit=facet_limit,
                    revision=page.revision,
                )
                for facet in CatalogFacetKind
            ),
        )

    def list_publication_facets(
        self,
        *,
        facet: CatalogFacetKind,
        query: CatalogDiscoveryQuery = _EMPTY_QUERY,
        after: CatalogFacetCursor | None = None,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogFacetPage:
        assert 1 <= limit <= 128
        self.facet_calls.append((facet, query, after, limit))
        selected_revision = self._revision_at(revision)
        query_sha256 = self._query_sha256(query)
        counts: dict[
            tuple[str, str | None, str | None],
            tuple[str, int],
        ] = {}
        effective_query = query
        if facet is CatalogFacetKind.LANGUAGE:
            effective_query = replace(query, language=None)
        elif facet is CatalogFacetKind.SUBJECT:
            effective_query = replace(query, subject=None)
        elif facet is CatalogFacetKind.CONTRIBUTOR:
            effective_query = replace(query, contributor=None)
        for publication in self._publications_at(revision):
            if not publication.artifacts or not self._matches_query(
                publication,
                effective_query,
            ):
                continue
            raw_values: tuple[
                tuple[str, str, str | None, str | None],
                ...,
            ]
            if facet is CatalogFacetKind.LANGUAGE:
                raw_values = ((publication.language, publication.language, None, None),)
            elif facet is CatalogFacetKind.SUBJECT:
                raw_values = tuple(
                    (
                        subject.name,
                        subject.name,
                        None,
                        subject.code,
                    )
                    for subject in publication.subjects
                    if subject.name and subject.code
                )
            else:
                raw_values = tuple(
                    (
                        contributor.name,
                        contributor.name,
                        contributor.role,
                        None,
                    )
                    for contributor in publication.contributors
                    if contributor.name and contributor.role
                )
            for value, label, role, namespace in raw_values:
                key = (value, role, namespace)
                previous = counts.get(key)
                counts[key] = (label, 1 if previous is None else previous[1] + 1)
        values = tuple(
            CatalogFacetValue(
                value=value,
                label=label,
                publication_count=count,
                role=role,
                namespace=namespace,
            )
            for (value, role, namespace), (label, count) in sorted(
                counts.items(),
                key=lambda item: (
                    -item[1][1],
                    item[0][0],
                    item[0][1] or "",
                    item[0][2] or "",
                ),
            )
        )
        start = 0
        if after is not None:
            if (
                after.revision != selected_revision.revision
                or after.query_sha256 != query_sha256
                or after.facet is not facet
                or after.position >= len(values)
            ):
                raise CatalogCursorError("facet cursor is invalid")
            boundary = values[after.position]
            if sha256(boundary.value.encode()).hexdigest() != after.value_sha256:
                raise CatalogCursorError("facet cursor boundary is invalid")
            start = after.position + 1
        visible = values[start : start + limit]
        next_cursor = None
        if start + limit < len(values) and visible:
            position = start + len(visible) - 1
            next_cursor = CatalogFacetCursor(
                revision=selected_revision.revision,
                query_sha256=query_sha256,
                facet=facet,
                position=position,
                value_sha256=sha256(visible[-1].value.encode()).hexdigest(),
            )
        return CatalogFacetPage(
            revision=selected_revision,
            facet=facet,
            values=visible,
            next_cursor=next_cursor,
            limit=limit,
        )

    def get_publication(
        self,
        publication_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogPublication | None:
        self.publication_revisions.append(revision)
        return next(
            (
                publication
                for publication in self._publications_at(revision)
                if publication.publication_id == publication_id
            ),
            None,
        )

    def list_recent_publications(
        self,
        *,
        order: CatalogRecentOrder,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogRecentWindow:
        self.recent_list_calls.append((order, revision))
        selected_revision = self._revision_at(revision)
        publications = tuple(
            sorted(
                (
                    publication
                    for publication in self.publications
                    if publication.artifacts
                ),
                key=lambda publication: (
                    publication.published_at
                    if order is CatalogRecentOrder.UPLOADED
                    else publication.downloaded_at,
                    publication.gid,
                ),
                reverse=True,
            )[:128]
        )
        window = CatalogRecentWindow(
            revision=selected_revision,
            order=order,
            publications=publications,
        )
        if self.recent_corruption == "order":
            replacement = (
                CatalogRecentOrder.DOWNLOADED
                if order is CatalogRecentOrder.UPLOADED
                else CatalogRecentOrder.UPLOADED
            )
            object.__setattr__(window, "order", replacement)
        elif self.recent_corruption == "oversized":
            object.__setattr__(window, "publications", publications * 43)
        elif self.recent_corruption == "artifactless":
            object.__setattr__(
                window,
                "publications",
                (replace(publications[0], artifacts=()), *publications[1:]),
            )
        return window

    def get_publication_presentation(
        self,
        publication_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogPublicationPresentation | None:
        self.presentation_revisions.append(revision)
        publication = self.get_publication(publication_id, revision=revision)
        if publication is None:
            return None
        return CatalogPublicationPresentation(
            publication_id=publication.publication_id,
            page_count=publication.page_count,
            cover=publication.cover,
            thumbnail=publication.thumbnail,
        )

    def get_publication_page(
        self,
        publication_id: str,
        page_index: int,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogImageResource | None:
        self.page_revisions.append(revision)
        publication = self.get_publication(publication_id, revision=revision)
        if publication is None or page_index != 0:
            return None
        return publication.cover

    def get_publications_by_artifact_names(
        self,
        names: Sequence[str],
        *,
        revision: CatalogRevision | int | None = None,
    ) -> Mapping[str, CatalogPublication]:
        requested = set(names)
        return {
            artifact.name: publication
            for publication in self._publications_at(revision)
            for artifact in publication.artifacts
            if artifact.name in requested
        }

    def get_artifact(
        self,
        artifact_id: str,
        *,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogArtifact | None:
        self.artifact_revisions.append(revision)
        return next(
            (
                artifact
                for publication in self._publications_at(revision)
                for artifact in publication.artifacts
                if artifact.artifact_id == artifact_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class CatalogFixture:
    catalog: FakeCatalog
    publications: tuple[CatalogPublication, ...]
    artifact: CatalogArtifact
    artifact_path: Path
    thumbnail_path: Path
    payload: bytes
    thumbnail_payload: bytes


def build_catalog_fixture(tmp_path: Path) -> CatalogFixture:
    payload = _CATALOG_PAYLOAD
    library_root = tmp_path / "current"
    library_root.mkdir(exist_ok=True)
    alpha_key = StorageObjectKey(
        codec="managed-filesystem-v2",
        segments=("artifacts", "1001.cbz"),
    )
    artifact_path = library_root.joinpath(*alpha_key.segments)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(payload)
    timestamp = datetime(2026, 8, 5, 12, 30, 45, tzinfo=UTC)
    alpha_downloaded = datetime(2026, 8, 5, 15, 0, tzinfo=UTC)
    artifact = CatalogArtifact(
        artifact_id=ALPHA_ARTIFACT_ID,
        name="alpha.cbz",
        storage_object=StorageObjectDescriptor(
            key=alpha_key,
            size_bytes=len(payload),
            sha256=sha256(payload).hexdigest(),
            modified_at=timestamp,
        ),
        media_type="application/vnd.comicbook+zip",
    )
    page_extent = ByteExtent(offset=4, length=12)
    cover = CatalogImageResource(
        storage_object=StorageObjectDescriptor(
            key=StorageObjectKey(
                codec="managed-filesystem-v2",
                segments=artifact.storage_object.key.segments,
            ),
            size_bytes=len(payload),
            sha256=sha256(payload).hexdigest(),
            modified_at=timestamp,
        ),
        extent=page_extent,
        media_type="image/jpeg",
        sha256=sha256(
            payload[page_extent.offset : page_extent.offset + page_extent.length]
        ).hexdigest(),
        width=1200,
        height=1800,
    )
    thumbnail_payload = b"thumbnail-320"
    thumbnail_key = StorageObjectKey(
        codec="managed-filesystem-v2",
        segments=("presentations", "1001", "thumbnail-320.jpg"),
    )
    thumbnail_path = library_root.joinpath(*thumbnail_key.segments)
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnail_path.write_bytes(thumbnail_payload)
    thumbnail = CatalogImageResource(
        storage_object=StorageObjectDescriptor(
            key=thumbnail_key,
            size_bytes=len(thumbnail_payload),
            sha256=sha256(thumbnail_payload).hexdigest(),
            modified_at=timestamp,
        ),
        extent=ByteExtent(offset=0, length=len(thumbnail_payload)),
        media_type="image/jpeg",
        sha256=sha256(thumbnail_payload).hexdigest(),
        width=213,
        height=320,
    )
    beta_artifact = replace(
        artifact,
        artifact_id=BETA_ARTIFACT_ID,
        name="beta.cbz",
        storage_object=replace(
            artifact.storage_object,
            key=StorageObjectKey(
                codec="managed-filesystem-v2",
                segments=("artifacts", "1002.cbz"),
            ),
        ),
    )
    gamma_artifact = replace(
        artifact,
        artifact_id=GAMMA_ARTIFACT_ID,
        name="gamma.cbz",
        storage_object=replace(
            artifact.storage_object,
            key=StorageObjectKey(
                codec="managed-filesystem-v2",
                segments=("artifacts", "1003.cbz"),
            ),
        ),
    )
    for sibling in (beta_artifact, gamma_artifact):
        sibling_path = library_root.joinpath(*sibling.storage_object.key.segments)
        sibling_path.parent.mkdir(parents=True, exist_ok=True)
        sibling_path.write_bytes(payload)
    publications = (
        CatalogPublication(
            publication_id="urn:h2h:gallery:1001",
            gid=1001,
            source_gallery_name="Alpha Gallery [1001]",
            title="Alpha Gallery",
            source_title="Cobalt Alpha",
            sort_title="alpha gallery",
            summary="A cobalt adventure",
            language="en",
            published_at=timestamp,
            downloaded_at=alpha_downloaded,
            modified_at=timestamp,
            page_count=1,
            cover=cover,
            thumbnail=thumbnail,
            contributors=(CatalogContributor(name="Alice", role="artist"),),
            subjects=(
                CatalogSubject(name="fantasy", scheme="tag", code="f"),
                CatalogSubject(name="", scheme="h2h:tag:misc", code="misc"),
            ),
            artifacts=(artifact,),
        ),
        CatalogPublication(
            publication_id="urn:h2h:gallery:1002",
            gid=1002,
            source_gallery_name="Beta Gallery [1002]",
            title="Beta Gallery",
            source_title="Beta Gallery",
            sort_title="beta gallery",
            summary="A quiet archive",
            language="ja",
            published_at=datetime(2026, 8, 5, 13, 0, tzinfo=UTC),
            downloaded_at=datetime(2026, 8, 5, 14, 0, tzinfo=UTC),
            modified_at=timestamp,
            page_count=0,
            cover=None,
            thumbnail=None,
            artifacts=(beta_artifact,),
        ),
        CatalogPublication(
            publication_id="urn:h2h:gallery:1003",
            gid=1003,
            source_gallery_name="Gamma Gallery [1003]",
            title="Gamma Gallery",
            source_title="Cobalt Gamma",
            sort_title="gamma gallery",
            summary="Another cobalt record",
            language="zh",
            published_at=datetime(2026, 8, 5, 11, 0, tzinfo=UTC),
            downloaded_at=datetime(2026, 8, 5, 16, 0, tzinfo=UTC),
            modified_at=timestamp,
            page_count=0,
            cover=None,
            thumbnail=None,
            artifacts=(gamma_artifact,),
        ),
    )
    return CatalogFixture(
        catalog=FakeCatalog(publications),
        publications=publications,
        artifact=artifact,
        artifact_path=artifact_path,
        thumbnail_path=thumbnail_path,
        payload=payload,
        thumbnail_payload=thumbnail_payload,
    )
