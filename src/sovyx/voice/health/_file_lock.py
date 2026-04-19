"""Cross-platform OS file lock for ComboStore (and CaptureOverrides).

§2 of ADR-combo-store-schema requires a single-writer-at-a-time guard
across daemon and doctor CLI processes. Pure ``asyncio.Lock`` won't do
— the two processes don't share an event loop. We use the OS-level
advisory locking primitive available on each platform:

* **Windows** — :func:`msvcrt.locking` with ``LK_NBLCK`` for a non-blocking
  test-and-set; we wrap it in a polling loop for the timeout API.
* **POSIX** — :func:`fcntl.flock` with ``LOCK_EX | LOCK_NB``; same
  polling loop wraps it.

The lock file is a sibling of the protected file
(``capture_combos.json.lock``). Holding the file open for the duration
of the critical section is what holds the lock — closing the handle
releases it implicitly. We always use the context-manager API so a
crash inside the critical section can never strand the lock.

Design notes
============

* **Polling loop**, not blocking acquire. Both ``msvcrt.locking`` and
  ``flock`` have OS-blocking variants, but mixing them with our
  asyncio-driven daemon is messy. A 50 ms poll is plenty for the
  ~10 ms typical contention window.
* **Stale-lock recovery is the OS's job.** When a holder process dies,
  the OS releases its file locks immediately. We do NOT delete the
  ``.lock`` file ourselves on errors — that would race with a healthy
  holder.
* **Lock file is created on demand** with restrictive permissions
  (``0o600`` on POSIX). The *contents* of the file are irrelevant;
  only its kernel-level lock state matters.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from contextlib import contextmanager
from typing import IO, TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = get_logger(__name__)


_DEFAULT_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 0.05


class FileLockTimeoutError(TimeoutError):
    """Raised when :func:`acquire_file_lock` cannot acquire within ``timeout_s``."""

    def __init__(self, path: Path, timeout_s: float) -> None:
        super().__init__(f"could not acquire {path} within {timeout_s:.1f}s")
        self.path = path
        self.timeout_s = timeout_s


@contextmanager
def acquire_file_lock(
    lock_path: Path,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Iterator[None]:
    """Acquire an exclusive OS file lock at ``lock_path``.

    Polls every 50 ms until the lock is held or ``timeout_s`` elapses.
    Raises :class:`FileLockTimeout` on timeout. Releases the lock when
    the context exits, regardless of how (normal return or exception).

    The parent directory of ``lock_path`` must already exist; callers
    typically share the directory with the file they're protecting and
    create it before opening the store.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    handle: IO[bytes] | None = None
    try:
        while True:
            handle = open(lock_path, "ab+")  # noqa: SIM115, PTH123 — held until release
            if _try_lock(handle):
                try:
                    yield
                finally:
                    _unlock(handle)
                return
            handle.close()
            handle = None
            if time.monotonic() >= deadline:
                raise FileLockTimeoutError(lock_path, timeout_s)
            time.sleep(_POLL_INTERVAL_S)
    except FileLockTimeoutError:
        raise
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):  # pragma: no cover — best-effort
                handle.close()


def _try_lock(handle: IO[bytes]) -> bool:
    """Attempt a non-blocking exclusive lock. Returns ``True`` on success."""
    if sys.platform == "win32":
        try:
            import msvcrt
        except ImportError:  # pragma: no cover — impossible on win32
            return True
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    try:
        import fcntl
    except ImportError:  # pragma: no cover — POSIX only
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    # POSIX: tighten the file permissions on first creation.
    with contextlib.suppress(OSError):  # pragma: no cover — best-effort
        os.fchmod(handle.fileno(), 0o600)
    return True


def _unlock(handle: IO[bytes]) -> None:
    """Release the lock previously taken by :func:`_try_lock`."""
    if sys.platform == "win32":
        try:
            import msvcrt
        except ImportError:  # pragma: no cover
            return
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError as exc:  # pragma: no cover — best-effort
            logger.debug("file_lock_unlock_failed", detail=str(exc))
        return

    try:
        import fcntl
    except ImportError:  # pragma: no cover — POSIX only
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:  # pragma: no cover — best-effort
        logger.debug("file_lock_unlock_failed", detail=str(exc))


__all__ = [
    "FileLockTimeoutError",
    "acquire_file_lock",
]
