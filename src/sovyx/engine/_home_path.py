"""Defensive home-directory resolution for Sovyx data paths (#32).

``Path.home()`` is the canonical Python idiom for resolving the user's
home directory but it has THREE failure modes Sovyx has hit in real
deployments:

1. **POSIX with ``$HOME`` unset.** ``pathlib.Path.home()`` falls back
   to ``pwd.getpwuid(os.getuid()).pw_dir`` which can ALSO fail in
   containers that ship with a stripped ``/etc/passwd`` — raising
   ``RuntimeError`` ("Could not determine home directory."). The
   daemon then crashes BEFORE it can emit a useful error.

2. **Windows with ``USERPROFILE`` unset and no fallback.** Service
   accounts running Sovyx as a Windows service sometimes have
   ``USERPROFILE`` empty in the service token. ``Path.home()`` falls
   back to ``getpass.getuser()`` resolution which can resolve to a
   path that doesn't exist on disk.

3. **Sandboxed runtimes (Snap / Flatpak / macOS sandbox).** The
   resolved home may be read-only or write-protected so the
   subsequent ``data_dir.mkdir(...)`` raises ``PermissionError``.

This module ships :func:`resolve_home_dir` — a one-call resolver that
tries ``Path.home()`` first, validates write access, and falls back
to a stable ``tempfile.gettempdir() / "sovyx-fallback-<uid>"`` path
with a structured WARN. Sovyx still boots; operators see the warning
in logs / dashboard and can fix the underlying environment issue.

The fallback path is deterministic per user (uid suffix) so repeated
boots reuse the same fallback dir — operators don't accumulate orphan
sovyx-fallback-* directories under /tmp.

Reference: F1 inventory mission task #32; CPython
``pathlib.Path.home()`` source.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_FALLBACK_DIR_PREFIX = "sovyx-fallback"
"""Prefix for the deterministic fallback directory under tempdir.

Suffixed with the current user's uid (POSIX) or username (Windows) so
the path is stable per-user across boots — repeated fallbacks reuse
the same dir instead of accumulating sovyx-fallback-* under /tmp."""


def _user_suffix() -> str:
    """Stable per-user identity for the fallback directory name.

    POSIX uses uid (immutable across renames); Windows uses
    USERNAME (uid is not the right concept). Falls back to a
    constant ``"unknown"`` if even both lookups fail — at that
    point the fallback path becomes per-host shared, which is
    less ideal but still strictly better than crashing."""
    try:
        return str(os.getuid())  # type: ignore[attr-defined]  # POSIX-only
    except AttributeError:
        # Windows path — getuid doesn't exist.
        username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        # Sanitise: strip path separators, length-cap.
        safe = "".join(c for c in username if c.isalnum() or c in "_-")[:32]
        return safe or "unknown"


def resolve_home_dir() -> Path:
    """Return a writable home-equivalent directory, never raising.

    Resolution order:

    1. ``Path.home()`` — the canonical CPython resolver. When it
       returns a path that exists and is writable, we use it
       directly. This is the happy path for >99 % of deployments.

    2. ``Path.home()`` succeeded but the path doesn't exist or
       isn't writable — try to ``mkdir(parents=True, exist_ok=True)``
       and re-test. Many fresh containers have ``HOME=/root`` set
       but ``/root`` doesn't exist yet; creating it is the right
       fix.

    3. ``Path.home()`` raised ``RuntimeError`` OR steps 1+2
       failed — fall back to ``tempfile.gettempdir() /
       sovyx-fallback-<uid>`` with a structured WARN. The fallback
       path is deterministic per user so repeated boots reuse it.

    Returns:
        :class:`Path` to a writable directory. Never raises.

    Note:
        Callers should NOT cache the return value across long-lived
        processes — a transient permission glitch could resolve
        differently on the next call. The cost is microseconds; the
        caller's data path resolution is already cached at
        :class:`EngineConfig` instantiation time.
    """
    candidate: Path | None = None
    home_resolution_failed = False
    try:
        candidate = Path.home()
    except RuntimeError as exc:
        home_resolution_failed = True
        logger.warning(
            "engine.home_path_unresolved",
            error=str(exc),
            error_type=type(exc).__name__,
        )

    if candidate is not None:
        # Try to make the home dir usable. If it doesn't exist,
        # mkdir; if mkdir fails, fall through to the tempdir path.
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # Quick writability probe via os.access — cheaper than
            # creating + deleting a probe file. False positives
            # (filesystem reports writable but actual write fails)
            # are extremely rare; the EngineConfig downstream
            # mkdir() would surface them clearly.
            if os.access(candidate, os.W_OK):
                return candidate
        except OSError as exc:
            home_resolution_failed = True
            logger.warning(
                "engine.home_path_unwritable",
                home_path=str(candidate),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    # Fallback path. Deterministic per user so repeated boots reuse
    # the same dir.
    fallback = Path(tempfile.gettempdir()) / f"{_FALLBACK_DIR_PREFIX}-{_user_suffix()}"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # If even the tempdir mkdir fails the host is in a
        # genuinely broken state. Log loudly and return the path
        # anyway — the next caller's mkdir attempt will surface
        # the underlying error with full traceback.
        logger.error(
            "engine.home_path_fallback_unwritable",
            fallback_path=str(fallback),
            error=str(exc),
            error_type=type(exc).__name__,
        )
    if home_resolution_failed or candidate != fallback:
        logger.warning(
            "engine.home_path_using_fallback",
            fallback_path=str(fallback),
            **{
                "engine.action_required": (
                    f"~/.sovyx is not resolvable — using fallback "
                    f"{fallback}. Sovyx is operational but data persists "
                    f"under tempdir, which may be cleared on host reboot. "
                    f"Set the HOME environment variable (POSIX) / "
                    f"USERPROFILE (Windows) OR pass an explicit "
                    f"data_dir via SOVYX_DATA_DIR / config to silence "
                    f"this warning and persist data correctly."
                ),
            },
        )
    return fallback


__all__ = ["resolve_home_dir"]
