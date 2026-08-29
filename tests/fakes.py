from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from h2hdb import (
    CatalogArtifact,
    CatalogArtifactCursor,
    CatalogArtifactPage,
    CatalogContributor,
    CatalogCursorError,
    CatalogPage,
    CatalogPublication,
    CatalogRevision,
    CatalogRevisionNotFoundError,
    CatalogSubject,
    artifact_storage_key,
)


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
        self.list_calls: list[tuple[str | None, int, int]] = []
        self.list_revisions: list[CatalogRevision | int | None] = []
        self.artifact_list_calls: list[tuple[CatalogArtifactCursor | None, int]] = []
        self.publication_revisions: list[CatalogRevision | int | None] = []
        self.artifact_revisions: list[CatalogRevision | int | None] = []

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

    def list_publications(
        self,
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogPage:
        assert 1 <= limit <= 128
        self.list_calls.append((query, offset, limit))
        self.list_revisions.append(revision)
        normalized_query = query.casefold() if query is not None else None
        matches = tuple(
            publication
            for publication in self._publications_at(revision)
            if (
                normalized_query is None
                or normalized_query
                in " ".join(
                    (
                        publication.publication_id,
                        publication.title,
                        publication.summary,
                    )
                ).casefold()
            )
        )
        return CatalogPage(
            revision=self._revision_at(revision),
            publications=matches[offset : offset + limit],
            offset=offset,
            limit=limit,
            total=len(matches),
        )

    def list_artifact_publications(
        self,
        *,
        after: CatalogArtifactCursor | None = None,
        limit: int = 50,
        revision: CatalogRevision | int | None = None,
    ) -> CatalogArtifactPage:
        assert 1 <= limit <= 128
        self.artifact_list_calls.append((after, limit))
        self.list_revisions.append(revision)
        selected_revision = self._revision_at(revision)
        positioned = tuple(
            (position, publication)
            for position, publication in enumerate(self.publications)
            if publication.artifacts
        )
        if after is not None:
            if after.revision != selected_revision.revision:
                raise CatalogRevisionNotFoundError(after.revision)
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
                    "artifact cursor does not identify its boundary"
                )
            positioned = tuple(item for item in positioned if item[0] > after.position)
        page_items = positioned[:limit]
        next_cursor = None
        if len(positioned) > limit and page_items:
            last_position, last_publication = page_items[-1]
            next_cursor = CatalogArtifactCursor(
                revision=selected_revision.revision,
                position=last_position,
                publication_id=last_publication.publication_id,
            )
        return CatalogArtifactPage(
            revision=selected_revision,
            publications=tuple(publication for _, publication in page_items),
            next_cursor=next_cursor,
            limit=limit,
            total=selected_revision.artifact_count,
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
    payload: bytes


def build_catalog_fixture(tmp_path: Path) -> CatalogFixture:
    payload = b"0123456789abcdefghijklmnopqrstuvwxyz"
    library_root = tmp_path / "current"
    library_root.mkdir(exist_ok=True)
    alpha_key = artifact_storage_key(1001)
    artifact_path = library_root.joinpath(*alpha_key.segments)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(payload)
    timestamp = datetime(2026, 8, 5, 12, 30, 45, tzinfo=UTC)
    artifact = CatalogArtifact(
        artifact_id="artifact-alpha",
        name="alpha.cbz",
        storage_key=alpha_key,
        media_type="application/vnd.comicbook+zip",
        size_bytes=len(payload),
        sha256=sha256(payload).hexdigest(),
        modified_at=timestamp,
    )
    beta_artifact = replace(
        artifact,
        artifact_id="artifact-beta",
        name="beta.cbz",
        storage_key=artifact_storage_key(1002),
    )
    gamma_artifact = replace(
        artifact,
        artifact_id="artifact-gamma",
        name="gamma.cbz",
        storage_key=artifact_storage_key(1003),
    )
    for sibling in (beta_artifact, gamma_artifact):
        sibling_path = library_root.joinpath(*sibling.storage_key.segments)
        sibling_path.parent.mkdir(parents=True, exist_ok=True)
        sibling_path.write_bytes(payload)
    publications = (
        CatalogPublication(
            publication_id="publication-alpha",
            gid=1001,
            source_gallery_name="Alpha Gallery [1001]",
            title="Alpha Gallery",
            source_title="",
            sort_title="alpha gallery",
            summary="A cobalt adventure",
            language="en",
            published_at=timestamp,
            modified_at=timestamp,
            contributors=(CatalogContributor(name="Alice", role="artist"),),
            subjects=(
                CatalogSubject(name="fantasy", scheme="tag", code="f"),
                CatalogSubject(name="", scheme="h2h:tag:misc", code="misc"),
            ),
            artifacts=(artifact,),
        ),
        CatalogPublication(
            publication_id="publication-beta",
            gid=1002,
            source_gallery_name="Beta Gallery [1002]",
            title="Beta Gallery",
            source_title="Beta Gallery",
            sort_title="beta gallery",
            summary="A quiet archive",
            language="ja",
            published_at=timestamp,
            modified_at=timestamp,
            artifacts=(beta_artifact,),
        ),
        CatalogPublication(
            publication_id="publication-gamma",
            gid=1003,
            source_gallery_name="Gamma Gallery [1003]",
            title="Gamma Gallery",
            source_title="Gamma Gallery",
            sort_title="gamma gallery",
            summary="Another cobalt record",
            language="zh",
            published_at=timestamp,
            modified_at=timestamp,
            artifacts=(gamma_artifact,),
        ),
    )
    return CatalogFixture(
        catalog=FakeCatalog(publications),
        publications=publications,
        artifact=artifact,
        artifact_path=artifact_path,
        payload=payload,
    )
