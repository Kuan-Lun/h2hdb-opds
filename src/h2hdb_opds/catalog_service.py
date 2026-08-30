__all__ = [
    "ArtifactCursorBoundaryInvalid",
    "ArtifactCursorInvalid",
    "ArtifactPageSelection",
    "ArtifactRead",
    "ArtifactUnavailable",
    "CatalogService",
    "PageLimitExceeded",
    "PublicationSelection",
    "PublicationUnavailable",
    "RevisionUnavailable",
]

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from h2hdb import (
    CatalogArtifact,
    CatalogArtifactCursor,
    CatalogArtifactPage,
    CatalogCursorError,
    CatalogPublication,
    CatalogReader,
    CatalogRevision,
    CatalogRevisionNotFoundError,
)

from .cursor import decode_artifact_cursor
from .library import LibraryReadCoordinator


class RevisionUnavailable(LookupError):
    def __init__(self, revision: int | None) -> None:
        self.revision = revision
        super().__init__(revision)


class ArtifactCursorInvalid(ValueError):
    pass


class ArtifactCursorBoundaryInvalid(ValueError):
    pass


class PageLimitExceeded(ValueError):
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        super().__init__(maximum)


class PublicationUnavailable(LookupError):
    pass


class ArtifactUnavailable(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactPageSelection:
    page: CatalogArtifactPage
    cursor: CatalogArtifactCursor | None


@dataclass(frozen=True, slots=True)
class PublicationSelection:
    publication: CatalogPublication
    revision: CatalogRevision


@dataclass(frozen=True, slots=True)
class ArtifactRead:
    artifact: CatalogArtifact
    revision: CatalogRevision
    revalidate_head: Callable[[], None]


class CatalogService:
    """Protocol-neutral, bounded reads shared by every OPDS representation."""

    def __init__(
        self,
        *,
        reader: Callable[[], CatalogReader],
        library_reads: LibraryReadCoordinator,
        default_page_size: int,
        maximum_page_size: int,
    ) -> None:
        self._reader = reader
        self._library_reads = library_reads
        self._default_page_size = default_page_size
        self._maximum_page_size = maximum_page_size

    def _resolve_revision(self, requested: int | None) -> CatalogRevision:
        try:
            current = self._reader().get_catalog_revision()
        except CatalogRevisionNotFoundError as error:
            raise RevisionUnavailable(requested) from error
        if requested is not None and requested != current.revision:
            raise RevisionUnavailable(requested)
        return current

    @contextmanager
    def _pinned_revision_read(self, selected: CatalogRevision) -> Iterator[None]:
        try:
            yield
        except CatalogRevisionNotFoundError as error:
            # Never retry against a newer head: doing so would mix revisions in
            # one response assembled from several catalog reads.
            raise RevisionUnavailable(selected.revision) from error

    def revision(self, requested: int | None) -> CatalogRevision:
        with self._library_reads.read():
            return self._resolve_revision(requested)

    def list_publications(
        self,
        *,
        cursor: str | None,
        limit: int | None,
        revision: int | None,
    ) -> ArtifactPageSelection:
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            selected_limit = self._default_page_size if limit is None else limit
            if selected_limit > self._maximum_page_size:
                raise PageLimitExceeded(self._maximum_page_size)
            try:
                decoded_cursor = (
                    None if cursor is None else decode_artifact_cursor(cursor)
                )
            except ValueError as error:
                raise ArtifactCursorInvalid from error
            if (
                decoded_cursor is not None
                and decoded_cursor.revision != selected.revision
            ):
                raise RevisionUnavailable(decoded_cursor.revision)
            try:
                with self._pinned_revision_read(selected):
                    page = self._reader().list_artifact_publications(
                        after=decoded_cursor,
                        limit=selected_limit,
                        revision=selected,
                    )
            except CatalogCursorError as error:
                raise ArtifactCursorBoundaryInvalid from error
        return ArtifactPageSelection(page=page, cursor=decoded_cursor)

    def publication(
        self,
        publication_id: str,
        *,
        revision: int | None,
    ) -> PublicationSelection:
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            with self._pinned_revision_read(selected):
                publication = self._reader().get_publication(
                    publication_id,
                    revision=selected,
                )
        if publication is None or not publication.artifacts:
            raise PublicationUnavailable(publication_id)
        return PublicationSelection(publication=publication, revision=selected)

    @contextmanager
    def artifact_read(
        self,
        artifact_id: str,
        *,
        revision: int | None,
    ) -> Iterator[ArtifactRead]:
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            with self._pinned_revision_read(selected):
                artifact = self._reader().get_artifact(
                    artifact_id,
                    revision=selected,
                )
                if artifact is None:
                    raise ArtifactUnavailable(artifact_id)

                def revalidate_head() -> None:
                    confirmed = self._reader().get_catalog_revision()
                    if confirmed.revision != selected.revision:
                        raise RevisionUnavailable(selected.revision)

                yield ArtifactRead(
                    artifact=artifact,
                    revision=selected,
                    revalidate_head=revalidate_head,
                )
