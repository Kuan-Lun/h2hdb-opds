from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from h2hdb import (
    CatalogArtifact,
    CatalogContributor,
    CatalogPage,
    CatalogPublication,
    CatalogRevision,
    CatalogRevisionNotFoundError,
    CatalogSubject,
)


class FakeCatalog:
    def __init__(self, publications: tuple[CatalogPublication, ...]) -> None:
        self.publications = publications
        self.revision = CatalogRevision(
            revision=7,
            published_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
            publication_count=len(publications),
        )
        self._snapshots = {
            self.revision.revision: (self.revision, publications),
        }
        self.revision_lookups: list[int | None] = []
        self.list_calls: list[tuple[str | None, int, int]] = []
        self.require_artifact_calls: list[bool] = []
        self.list_revisions: list[CatalogRevision | None] = []
        self.publication_revisions: list[CatalogRevision | None] = []
        self.artifact_revisions: list[CatalogRevision | None] = []

    def add_revision(
        self,
        revision: CatalogRevision,
        publications: tuple[CatalogPublication, ...],
    ) -> None:
        self._snapshots[revision.revision] = (revision, publications)
        self.revision = revision
        self.publications = publications

    def get_catalog_revision(self, revision: int | None = None) -> CatalogRevision:
        self.revision_lookups.append(revision)
        selected_revision = self.revision.revision if revision is None else revision
        try:
            return self._snapshots[selected_revision][0]
        except KeyError as error:
            raise CatalogRevisionNotFoundError(selected_revision) from error

    def _publications_at(
        self,
        revision: CatalogRevision | None,
    ) -> tuple[CatalogPublication, ...]:
        selected_revision = revision or self.revision
        return self._snapshots[selected_revision.revision][1]

    def list_publications(
        self,
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
        revision: CatalogRevision | None = None,
        require_artifact: bool = False,
    ) -> CatalogPage:
        self.list_calls.append((query, offset, limit))
        self.require_artifact_calls.append(require_artifact)
        self.list_revisions.append(revision)
        normalized_query = query.casefold() if query is not None else None
        matches = tuple(
            publication
            for publication in self._publications_at(revision)
            if (not require_artifact or publication.artifacts)
            and (
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
            revision=revision or self.revision,
            publications=matches[offset : offset + limit],
            offset=offset,
            limit=limit,
            total=len(matches),
        )

    def get_publication(
        self,
        publication_id: str,
        *,
        revision: CatalogRevision | None = None,
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
        revision: CatalogRevision | None = None,
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
        revision: CatalogRevision | None = None,
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
    payload: bytes


def build_catalog_fixture(tmp_path: Path) -> CatalogFixture:
    payload = b"0123456789abcdefghijklmnopqrstuvwxyz"
    artifact_path = tmp_path / "alpha.cbz"
    artifact_path.write_bytes(payload)
    timestamp = datetime(2026, 8, 5, 12, 30, 45, tzinfo=UTC)
    artifact = CatalogArtifact(
        artifact_id="artifact-alpha",
        name="alpha.cbz",
        location=artifact_path,
        media_type="application/vnd.comicbook+zip",
        size_bytes=len(payload),
        sha256=sha256(payload).hexdigest(),
        modified_at=timestamp,
    )
    beta_artifact = replace(
        artifact,
        artifact_id="artifact-beta",
        name="beta.cbz",
    )
    gamma_artifact = replace(
        artifact,
        artifact_id="artifact-gamma",
        name="gamma.cbz",
    )
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
            contributors=(
                CatalogContributor(name="Alice", role="artist", sort_as="Alice"),
            ),
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
        payload=payload,
    )
