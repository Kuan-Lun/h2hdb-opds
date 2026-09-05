__all__ = [
    "ACTIVATING_MARKER_NAME",
    "PUBLICATION_LOCK_NAME",
    "LibraryIntegrityError",
    "LibraryReadCoordinator",
    "LibraryUnavailable",
    "open_directory_without_symlinks",
]

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PUBLICATION_LOCK_NAME = "publication.lock"
ACTIVATING_MARKER_NAME = "ACTIVATING"


class LibraryUnavailable(RuntimeError):
    """The public library cannot be read during an activation transition."""


class LibraryIntegrityError(RuntimeError):
    """Durable library or coordination state violates its read contract."""


def open_directory_without_symlinks(path: Path) -> int:
    """Open an absolute directory path without following any symlink component."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    normalized = Path(os.path.abspath(path))
    descriptor = os.open(normalized.anchor, flags)
    try:
        for component in normalized.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_publication_lock(coordination_descriptor: int) -> int:
    # O_NONBLOCK prevents a hostile or damaged FIFO leaf from hanging startup
    # or a request before fstat can reject its type.
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(
        PUBLICATION_LOCK_NAME,
        flags,
        dir_fd=coordination_descriptor,
    )
    try:
        lock_stat = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(lock_stat.st_mode):
        os.close(descriptor)
        raise OSError(errno.EINVAL, "publication lock is not a regular file")
    return descriptor


def _activation_marker_exists(coordination_descriptor: int) -> bool:
    try:
        os.stat(
            ACTIVATING_MARKER_NAME,
            dir_fd=coordination_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    except OSError as error:
        raise LibraryIntegrityError(
            "Library activation state cannot be verified"
        ) from error
    return True


class LibraryReadCoordinator:
    """Coordinate readers with ingest's crash-recoverable library activation."""

    def __init__(self, *, library_root: Path, coordination_root: Path) -> None:
        self.library_root = library_root
        self.coordination_root = coordination_root

    def validate(self) -> None:
        library_descriptor = -1
        try:
            library_descriptor = open_directory_without_symlinks(self.library_root)
            library_stat = os.fstat(library_descriptor)
        except OSError as error:
            raise RuntimeError(
                f"Library root must be a real directory: {self.library_root}"
            ) from error
        finally:
            if library_descriptor >= 0:
                os.close(library_descriptor)

        coordination_descriptor = -1
        lock_descriptor = -1
        try:
            coordination_descriptor = open_directory_without_symlinks(
                self.coordination_root
            )
            coordination_stat = os.fstat(coordination_descriptor)
            if (coordination_stat.st_dev, coordination_stat.st_ino) == (
                library_stat.st_dev,
                library_stat.st_ino,
            ):
                raise OSError(
                    errno.EINVAL,
                    "library and coordination roots resolve to the same directory",
                )
            lock_descriptor = _open_publication_lock(coordination_descriptor)
        except OSError as error:
            raise RuntimeError(
                "Coordination root must be a real directory containing a regular "
                f"{PUBLICATION_LOCK_NAME}: {self.coordination_root}"
            ) from error
        finally:
            if lock_descriptor >= 0:
                os.close(lock_descriptor)
            if coordination_descriptor >= 0:
                os.close(coordination_descriptor)

    @contextmanager
    def read(self) -> Iterator[None]:
        coordination_descriptor = -1
        lock_descriptor = -1
        locked = False
        try:
            try:
                coordination_descriptor = open_directory_without_symlinks(
                    self.coordination_root
                )
                lock_descriptor = _open_publication_lock(coordination_descriptor)
            except OSError as error:
                raise LibraryIntegrityError(
                    "Library publication coordination cannot be opened"
                ) from error
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise LibraryUnavailable(
                        "Library publication is temporarily unavailable"
                    ) from error
                raise LibraryIntegrityError(
                    "Library publication lock cannot be acquired"
                ) from error
            if _activation_marker_exists(coordination_descriptor):
                raise LibraryUnavailable("Library publication is activating")
            yield
        finally:
            try:
                if locked:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                if lock_descriptor >= 0:
                    os.close(lock_descriptor)
                if coordination_descriptor >= 0:
                    os.close(coordination_descriptor)
