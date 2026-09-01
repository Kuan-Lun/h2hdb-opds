__all__ = [
    "ArtifactRead",
    "ArtifactUnavailable",
    "CatalogService",
    "CursorBoundaryInvalid",
    "CursorInvalid",
    "DiscoveryFeedSelection",
    "FacetPageSelection",
    "MediaRead",
    "MediaUnavailable",
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
    CatalogCursorError,
    CatalogDiscoveryCursor,
    CatalogDiscoveryPage,
    CatalogDiscoveryQuery,
    CatalogFacetCursor,
    CatalogFacetKind,
    CatalogFacetPage,
    CatalogImageResource,
    CatalogPublication,
    CatalogPublicationPresentation,
    CatalogReader,
    CatalogRecentOrder,
    CatalogRecentWindow,
    CatalogRevision,
    CatalogRevisionNotFoundError,
)

from .cursor import decode_discovery_cursor, decode_facet_cursor
from .library import LibraryReadCoordinator, LibraryUnavailable
from .publication import publication_gid, publication_identifier

_PSE_PAGE_COUNT_MAXIMUM = 4096
_PSE_IMAGE_MEDIA_TYPE = "image/jpeg"
_SUPPORTED_ARTIFACT_MEDIA_TYPE = "application/vnd.comicbook+zip"


class RevisionUnavailable(LookupError):
    def __init__(self, revision: int | None) -> None:
        self.revision = revision
        super().__init__(revision)


class CursorInvalid(ValueError):
    pass


class CursorBoundaryInvalid(ValueError):
    pass


class PageLimitExceeded(ValueError):
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        super().__init__(maximum)


class PublicationUnavailable(LookupError):
    pass


class MediaUnavailable(LookupError):
    pass


class ArtifactUnavailable(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class DiscoveryFeedSelection:
    page: CatalogDiscoveryPage
    cursor: CatalogDiscoveryCursor | None
    query: CatalogDiscoveryQuery
    facets: tuple[CatalogFacetPage, ...]


@dataclass(frozen=True, slots=True)
class FacetPageSelection:
    page: CatalogFacetPage
    cursor: CatalogFacetCursor | None
    query: CatalogDiscoveryQuery


@dataclass(frozen=True, slots=True)
class PublicationSelection:
    publication: CatalogPublication
    revision: CatalogRevision


@dataclass(frozen=True, slots=True)
class MediaRead:
    resource: CatalogImageResource
    revision: CatalogRevision
    revalidate_head: Callable[[], None]


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
        if current.artifact_count not in {0, current.publication_count}:
            raise LibraryUnavailable(
                "catalog revision violates the all-or-none artifact contract"
            )
        return current

    @contextmanager
    def _pinned_revision_read(self, selected: CatalogRevision) -> Iterator[None]:
        try:
            yield
        except CatalogRevisionNotFoundError as error:
            raise RevisionUnavailable(selected.revision) from error

    def _selected_limit(self, requested: int | None) -> int:
        selected = self._default_page_size if requested is None else requested
        if selected > self._maximum_page_size:
            raise PageLimitExceeded(self._maximum_page_size)
        return selected

    @staticmethod
    def _validate_requested_publication_id(
        publication_id: str,
        *,
        unavailable: type[PublicationUnavailable] | type[MediaUnavailable],
    ) -> None:
        try:
            publication_gid(publication_id)
        except (TypeError, ValueError) as error:
            raise unavailable(publication_id) from error

    @staticmethod
    def _validate_publication(publication: CatalogPublication) -> None:
        try:
            publication_identifier(
                publication.publication_id,
                expected_gid=publication.gid,
            )
        except (TypeError, ValueError) as error:
            raise LibraryUnavailable(
                "catalog publication identity violates the canonical GID contract"
            ) from error
        page_count = publication.page_count
        if (
            isinstance(page_count, bool)
            or not isinstance(page_count, int)
            or not 0 <= page_count <= _PSE_PAGE_COUNT_MAXIMUM
        ):
            raise LibraryUnavailable(
                "catalog publication presentation violates the OPDS-PSE contract"
            )
        has_pages = page_count > 0
        if (publication.cover is not None) != has_pages or (
            publication.thumbnail is not None
        ) != has_pages:
            raise LibraryUnavailable(
                "catalog publication presentation violates the OPDS-PSE contract"
            )
        if has_pages and (
            publication.cover is None
            or publication.cover.media_type != _PSE_IMAGE_MEDIA_TYPE
            or publication.thumbnail is None
            or publication.thumbnail.media_type != _PSE_IMAGE_MEDIA_TYPE
        ):
            raise LibraryUnavailable(
                "catalog publication presentation violates the OPDS-PSE contract"
            )
        if len(publication.artifacts) != 1:
            raise LibraryUnavailable(
                "catalog publication must have exactly one direct CBZ acquisition"
            )
        for artifact in publication.artifacts:
            CatalogService._validate_artifact(artifact)

    @staticmethod
    def _validate_artifact(artifact: CatalogArtifact) -> None:
        if artifact.media_type != _SUPPORTED_ARTIFACT_MEDIA_TYPE:
            raise LibraryUnavailable(
                "catalog artifact is not a supported direct CBZ acquisition"
            )

    @classmethod
    def _validate_presentation(
        cls,
        publication: CatalogPublication,
        presentation: CatalogPublicationPresentation,
    ) -> None:
        cls._validate_publication(publication)
        if (
            presentation.publication_id != publication.publication_id
            or presentation.page_count != publication.page_count
            or presentation.cover != publication.cover
            or presentation.thumbnail != publication.thumbnail
        ):
            raise LibraryUnavailable(
                "catalog presentation disagrees with its publication"
            )

    @staticmethod
    def _has_acquisition_catalog(selected: CatalogRevision) -> bool:
        return selected.artifact_count > 0

    def _revalidator(self, selected: CatalogRevision) -> Callable[[], None]:
        def revalidate_head() -> None:
            self._resolve_revision(selected.revision)

        return revalidate_head

    def revision(self, requested: int | None) -> CatalogRevision:
        with self._library_reads.read():
            return self._resolve_revision(requested)

    def discovery_feed(
        self,
        *,
        query: CatalogDiscoveryQuery,
        cursor: str | None,
        limit: int | None,
        revision: int | None,
    ) -> DiscoveryFeedSelection:
        """Read one page and all facet families under one catalog-head fence."""

        with self._library_reads.read():
            try:
                selected_limit = self._selected_limit(limit)
            except PageLimitExceeded:
                # Preserve the established public error precedence without
                # adding a revision lookup to the successful hot path.
                self._resolve_revision(revision)
                raise
            try:
                decoded = None if cursor is None else decode_discovery_cursor(cursor)
            except ValueError as error:
                # Revision authority preceded cursor parsing before discovery
                # and facet reads were bundled.  Probe only this error path so
                # a stale/partial catalog retains the same public response.
                self._resolve_revision(revision)
                raise CursorInvalid from error
            if (
                decoded is not None
                and revision is not None
                and decoded.revision != revision
            ):
                self._resolve_revision(revision)
                raise RevisionUnavailable(decoded.revision)
            requested_revision = (
                revision
                if revision is not None
                else (None if decoded is None else decoded.revision)
            )
            try:
                bundle = self._reader().discover_publications_with_facets(
                    query=query,
                    after=decoded,
                    limit=selected_limit,
                    facet_limit=self._maximum_page_size,
                    revision=requested_revision,
                )
            except CatalogRevisionNotFoundError as error:
                raise RevisionUnavailable(requested_revision) from error
            except CatalogCursorError as error:
                raise CursorBoundaryInvalid from error

            selected = bundle.page.revision
            if (
                requested_revision is not None
                and selected.revision != requested_revision
            ):
                raise RevisionUnavailable(requested_revision)
            if selected.artifact_count not in {0, selected.publication_count}:
                raise LibraryUnavailable(
                    "catalog revision violates the all-or-none artifact contract"
                )
            if decoded is not None and decoded.revision != selected.revision:
                raise RevisionUnavailable(decoded.revision)
            if not self._has_acquisition_catalog(selected):
                if decoded is not None:
                    raise CursorBoundaryInvalid
                return DiscoveryFeedSelection(
                    page=CatalogDiscoveryPage(
                        revision=selected,
                        publications=(),
                        next_cursor=None,
                        limit=selected_limit,
                        total=0 if query == CatalogDiscoveryQuery() else None,
                    ),
                    cursor=None,
                    query=query,
                    facets=tuple(
                        CatalogFacetPage(
                            revision=selected,
                            facet=facet,
                            values=(),
                            next_cursor=None,
                            limit=self._maximum_page_size,
                        )
                        for facet in CatalogFacetKind
                    ),
                )

            page = bundle.page
            facets = bundle.facets
            if page.revision != selected or any(
                facet.revision != selected for facet in facets
            ):
                raise RevisionUnavailable(selected.revision)
            if tuple(facet.facet for facet in facets) != tuple(CatalogFacetKind):
                raise RevisionUnavailable(selected.revision)
        for publication in page.publications:
            self._validate_publication(publication)
            if not publication.artifacts:
                raise LibraryUnavailable(
                    "OPDS discovery returned an artifactless publication"
                )
        return DiscoveryFeedSelection(
            page=page,
            cursor=decoded,
            query=query,
            facets=facets,
        )

    def facet_page(
        self,
        *,
        facet: CatalogFacetKind,
        query: CatalogDiscoveryQuery,
        cursor: str | None,
        limit: int | None,
        revision: int | None,
    ) -> FacetPageSelection:
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            selected_limit = self._selected_limit(limit)
            try:
                decoded = None if cursor is None else decode_facet_cursor(cursor)
            except ValueError as error:
                raise CursorInvalid from error
            if decoded is not None and decoded.revision != selected.revision:
                raise RevisionUnavailable(decoded.revision)
            if not self._has_acquisition_catalog(selected):
                if decoded is not None:
                    raise CursorBoundaryInvalid
                return FacetPageSelection(
                    page=CatalogFacetPage(
                        revision=selected,
                        facet=facet,
                        values=(),
                        next_cursor=None,
                        limit=selected_limit,
                    ),
                    cursor=None,
                    query=query,
                )
            try:
                with self._pinned_revision_read(selected):
                    page = self._reader().list_publication_facets(
                        facet=facet,
                        query=query,
                        after=decoded,
                        limit=selected_limit,
                        revision=selected,
                    )
            except CatalogCursorError as error:
                raise CursorBoundaryInvalid from error
        if page.revision != selected or page.facet is not facet:
            raise RevisionUnavailable(selected.revision)
        return FacetPageSelection(page=page, cursor=decoded, query=query)

    def publication(
        self,
        publication_id: str,
        *,
        revision: int | None,
    ) -> PublicationSelection:
        self._validate_requested_publication_id(
            publication_id,
            unavailable=PublicationUnavailable,
        )
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            if not self._has_acquisition_catalog(selected):
                raise PublicationUnavailable(publication_id)
            with self._pinned_revision_read(selected):
                publication = self._reader().get_publication(
                    publication_id,
                    revision=selected,
                )
        if publication is None:
            raise PublicationUnavailable(publication_id)
        self._validate_publication(publication)
        if not publication.artifacts:
            raise PublicationUnavailable(publication_id)
        return PublicationSelection(publication=publication, revision=selected)

    def recent_publications(
        self,
        *,
        order: CatalogRecentOrder,
        revision: int | None,
    ) -> CatalogRecentWindow:
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            if not self._has_acquisition_catalog(selected):
                return CatalogRecentWindow(
                    revision=selected,
                    order=order,
                    publications=(),
                )
            with self._pinned_revision_read(selected):
                window = self._reader().list_recent_publications(
                    order=order,
                    revision=selected,
                )
        if window.revision != selected:
            raise RevisionUnavailable(selected.revision)
        if window.order is not order:
            raise LibraryUnavailable(
                "recent artifact window order differs from the request"
            )
        if len(window.publications) > 128 or any(
            not publication.artifacts for publication in window.publications
        ):
            raise LibraryUnavailable(
                "recent window is not an acquisition-only top-128 set"
            )
        for publication in window.publications:
            self._validate_publication(publication)
        return window

    def presentation(
        self,
        publication_id: str,
        *,
        revision: int | None,
    ) -> tuple[CatalogPublicationPresentation, CatalogRevision]:
        self._validate_requested_publication_id(
            publication_id,
            unavailable=PublicationUnavailable,
        )
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            if not self._has_acquisition_catalog(selected):
                raise PublicationUnavailable(publication_id)
            with self._pinned_revision_read(selected):
                publication = self._reader().get_publication(
                    publication_id,
                    revision=selected,
                )
                if publication is None:
                    raise PublicationUnavailable(publication_id)
                self._validate_publication(publication)
                if not publication.artifacts:
                    raise PublicationUnavailable(publication_id)
                presentation = self._reader().get_publication_presentation(
                    publication_id,
                    revision=selected,
                )
        if presentation is None:
            raise PublicationUnavailable(publication_id)
        self._validate_presentation(publication, presentation)
        return presentation, selected

    @contextmanager
    def artifact_read(
        self,
        artifact_id: str,
        *,
        revision: int | None,
    ) -> Iterator[ArtifactRead]:
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            if not self._has_acquisition_catalog(selected):
                raise ArtifactUnavailable(artifact_id)
            with self._pinned_revision_read(selected):
                artifact = self._reader().get_artifact(
                    artifact_id,
                    revision=selected,
                )
                if artifact is None:
                    raise ArtifactUnavailable(artifact_id)
                self._validate_artifact(artifact)
                yield ArtifactRead(
                    artifact=artifact,
                    revision=selected,
                    revalidate_head=self._revalidator(selected),
                )

    @contextmanager
    def page_read(
        self,
        publication_id: str,
        page_index: int,
        *,
        revision: int | None,
    ) -> Iterator[MediaRead]:
        self._validate_requested_publication_id(
            publication_id,
            unavailable=MediaUnavailable,
        )
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            if not self._has_acquisition_catalog(selected):
                raise MediaUnavailable(publication_id)
            with self._pinned_revision_read(selected):
                publication = self._reader().get_publication(
                    publication_id,
                    revision=selected,
                )
                if publication is None:
                    raise MediaUnavailable(publication_id)
                self._validate_publication(publication)
                if not publication.artifacts:
                    raise MediaUnavailable(publication_id)
                resource = self._reader().get_publication_page(
                    publication_id,
                    page_index,
                    revision=selected,
                )
                if resource is None:
                    raise MediaUnavailable(publication_id)
                if (
                    not 0 <= page_index < publication.page_count
                    or resource.media_type != _PSE_IMAGE_MEDIA_TYPE
                    or (page_index == 0 and resource != publication.cover)
                ):
                    raise LibraryUnavailable(
                        "catalog page disagrees with its publication presentation"
                    )
                yield MediaRead(
                    resource=resource,
                    revision=selected,
                    revalidate_head=self._revalidator(selected),
                )

    @contextmanager
    def thumbnail_read(
        self,
        publication_id: str,
        *,
        revision: int | None,
    ) -> Iterator[MediaRead]:
        self._validate_requested_publication_id(
            publication_id,
            unavailable=MediaUnavailable,
        )
        with self._library_reads.read():
            selected = self._resolve_revision(revision)
            if not self._has_acquisition_catalog(selected):
                raise MediaUnavailable(publication_id)
            with self._pinned_revision_read(selected):
                publication = self._reader().get_publication(
                    publication_id,
                    revision=selected,
                )
                if publication is None:
                    raise MediaUnavailable(publication_id)
                self._validate_publication(publication)
                if not publication.artifacts:
                    raise MediaUnavailable(publication_id)
                presentation = self._reader().get_publication_presentation(
                    publication_id,
                    revision=selected,
                )
                if presentation is None:
                    raise MediaUnavailable(publication_id)
                self._validate_presentation(publication, presentation)
                if presentation.thumbnail is None:
                    raise MediaUnavailable(publication_id)
                yield MediaRead(
                    resource=presentation.thumbnail,
                    revision=selected,
                    revalidate_head=self._revalidator(selected),
                )
