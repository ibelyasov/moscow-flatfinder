"""Scoped advisory lock for local writers and the persistent browser profile."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AnotherRun(RuntimeError):
    pass


@contextmanager
def acquire_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AnotherRun("another writer or browser operation is active") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
