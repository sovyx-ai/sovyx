"""Sovyx Plugin Sandbox — Filesystem access scoped to plugin data_dir.

All paths are resolved to absolute, checked against data_dir prefix,
and symlinks resolved BEFORE path check to prevent traversal attacks.

Limits:
- All operations scoped to data_dir
- Max 50MB per file
- Max 500MB total per plugin
- Symlink resolution before path check
- Path traversal (..) blocked

Spec: SPE-008-SANDBOX §6
"""

from __future__ import annotations

import contextlib
import os
import typing

from sovyx.observability.logging import get_logger
from sovyx.plugins.permissions import PermissionDeniedError, PermissionEnforcer

if typing.TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

logger = get_logger(__name__)


def _record_fs_denial(plugin: str) -> None:
    """T05 helper — record a FS-layer sandbox denial as an OTel counter.

    Lazy-import to avoid bootstrap circularity. Called by every
    ``raise PermissionDeniedError`` site in this module so the
    operator dashboard sees per-plugin FS denial trends. The
    log emission stays separate (different cardinality budget +
    different consumer audience).
    """
    from sovyx.plugins._metrics import record_sandbox_denial  # noqa: PLC0415

    record_sandbox_denial(plugin=plugin, layer="fs")


# ── Constants ───────────────────────────────────────────────────────

_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50MB per file
_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500MB total per plugin


# ── Sandboxed Filesystem Access ─────────────────────────────────────


class SandboxedFsAccess:
    """Filesystem access sandboxed to plugin's data_dir.

    Every path operation resolves symlinks and verifies the final
    path is inside data_dir. This prevents:
    - Path traversal (../../etc/passwd)
    - Symlink escape (data_dir/link → /etc)
    - Writing outside sandbox

    Usage::

        fs = SandboxedFsAccess(
            plugin_name="weather",
            data_dir=Path("~/.sovyx/minds/default/plugins/weather"),
            enforcer=enforcer,
        )
        await fs.write("cache.json", '{"temp": 20}')
        data = await fs.read("cache.json")

    Spec: SPE-008-SANDBOX §6
    """

    def __init__(
        self,
        plugin_name: str,
        data_dir: Path,
        enforcer: PermissionEnforcer,
        *,
        max_file_bytes: int = _MAX_FILE_BYTES,
        max_total_bytes: int = _MAX_TOTAL_BYTES,
    ) -> None:
        self._plugin = plugin_name
        self._data_dir = data_dir.resolve()
        self._enforcer = enforcer
        self._max_file = max_file_bytes
        self._max_total = max_total_bytes

        # Ensure data_dir exists
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, relative: str) -> Path:
        """Resolve a relative path safely within data_dir.

        1. Join with data_dir
        2. Resolve symlinks and ..
        3. Verify result is inside data_dir

        Args:
            relative: Relative path string.

        Returns:
            Absolute resolved Path inside data_dir.

        Raises:
            PermissionDeniedError: Path escapes data_dir.
        """
        # Reject absolute paths
        if os.path.isabs(relative):
            logger.warning(
                "plugin.fs.violation",
                **{
                    "plugin_id": self._plugin,
                    "plugin.fs.path_relative": relative,
                    "plugin.fs.violation_kind": "absolute_path",
                },
            )
            _record_fs_denial(self._plugin)
            raise PermissionDeniedError(self._plugin, f"Absolute paths not allowed: {relative}")

        # Join and resolve
        target = (self._data_dir / relative).resolve()

        # Verify inside data_dir
        try:
            target.relative_to(self._data_dir)
        except ValueError:
            logger.warning(
                "plugin.fs.violation",
                **{
                    "plugin_id": self._plugin,
                    "plugin.fs.path_relative": relative,
                    "plugin.fs.violation_kind": "path_escape",
                },
            )
            _record_fs_denial(self._plugin)
            raise PermissionDeniedError(
                self._plugin,
                f"Path escapes sandbox: {relative} → {target}",
            ) from None

        return target

    def _check_storage_budget(self, additional_bytes: int) -> None:
        """Check if writing would exceed total storage budget.

        Args:
            additional_bytes: Bytes about to be written.

        Raises:
            PermissionDeniedError: Storage budget exceeded.
        """
        current = self._get_total_size()
        if current + additional_bytes > self._max_total:
            _record_fs_denial(self._plugin)
            raise PermissionDeniedError(
                self._plugin,
                f"Storage budget exceeded: {current + additional_bytes} > {self._max_total} bytes",
            )

    def _get_total_size(self) -> int:
        """Calculate total storage used by this plugin."""
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self._data_dir):
            for f in filenames:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(dirpath, f))
        return total

    async def read(self, relative: str) -> str:
        """Read a text file from plugin data_dir.

        Args:
            relative: Relative path within data_dir.

        Returns:
            File contents as string.

        Raises:
            PermissionDeniedError: fs:read not granted or path escapes.
            FileNotFoundError: File doesn't exist.
        """
        self._enforcer.check("fs:read")
        path = self._safe_path(relative)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {relative}")

        text = path.read_text(encoding="utf-8")
        logger.info(
            "plugin.fs.read",
            **{
                "plugin_id": self._plugin,
                "plugin.fs.path_relative": relative,
                "plugin.fs.bytes": len(text.encode("utf-8")),
                "plugin.fs.binary": False,
            },
        )
        return text

    async def read_bytes(self, relative: str) -> bytes:
        """Read a binary file from plugin data_dir.

        Args:
            relative: Relative path within data_dir.

        Returns:
            File contents as bytes.

        Raises:
            PermissionDeniedError: fs:read not granted or path escapes.
            FileNotFoundError: File doesn't exist.
        """
        self._enforcer.check("fs:read")
        path = self._safe_path(relative)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {relative}")

        data = path.read_bytes()
        logger.info(
            "plugin.fs.read",
            **{
                "plugin_id": self._plugin,
                "plugin.fs.path_relative": relative,
                "plugin.fs.bytes": len(data),
                "plugin.fs.binary": True,
            },
        )
        return data

    async def write(self, relative: str, content: str) -> None:
        """Write a text file to plugin data_dir.

        Args:
            relative: Relative path within data_dir.
            content: Text content to write.

        Raises:
            PermissionDeniedError: fs:write not granted, path escapes,
                file too large, or storage budget exceeded.
        """
        self._enforcer.check("fs:write")
        data = content.encode("utf-8")
        await self._write_data(relative, data)

    async def write_bytes(self, relative: str, data: bytes) -> None:
        """Write a binary file to plugin data_dir.

        Args:
            relative: Relative path within data_dir.
            data: Binary content to write.

        Raises:
            PermissionDeniedError: fs:write not granted, path escapes,
                file too large, or storage budget exceeded.
        """
        self._enforcer.check("fs:write")
        await self._write_data(relative, data)

    async def _write_data(self, relative: str, data: bytes) -> None:
        """Internal write with size checks."""
        if len(data) > self._max_file:
            logger.warning(
                "plugin.fs.violation",
                **{
                    "plugin_id": self._plugin,
                    "plugin.fs.path_relative": relative,
                    "plugin.fs.violation_kind": "file_too_large",
                    "plugin.fs.bytes": len(data),
                    "plugin.fs.limit_bytes": self._max_file,
                },
            )
            _record_fs_denial(self._plugin)
            raise PermissionDeniedError(
                self._plugin,
                f"File too large: {len(data)} > {self._max_file} bytes",
            )

        try:
            self._check_storage_budget(len(data))
        except PermissionDeniedError:
            logger.warning(
                "plugin.fs.violation",
                **{
                    "plugin_id": self._plugin,
                    "plugin.fs.path_relative": relative,
                    "plugin.fs.violation_kind": "storage_budget",
                    "plugin.fs.bytes": len(data),
                    "plugin.fs.total_limit_bytes": self._max_total,
                },
            )
            raise
        path = self._safe_path(relative)

        # Create parent dirs if needed
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.info(
            "plugin.fs.write",
            **{
                "plugin_id": self._plugin,
                "plugin.fs.path_relative": relative,
                "plugin.fs.bytes": len(data),
                "plugin.fs.mode": "binary",
            },
        )

    async def delete(self, relative: str) -> bool:
        """Delete a file from plugin data_dir.

        Args:
            relative: Relative path within data_dir.

        Returns:
            True if file was deleted, False if it didn't exist.

        Raises:
            PermissionDeniedError: fs:write not granted or path escapes.
        """
        self._enforcer.check("fs:write")
        path = self._safe_path(relative)

        if not path.exists():
            return False

        if path.is_dir():
            _record_fs_denial(self._plugin)
            raise PermissionDeniedError(
                self._plugin,
                f"Cannot delete directory: {relative}. Use delete on files only.",
            )

        path.unlink()
        return True

    async def exists(self, relative: str) -> bool:
        """Check if a file exists in plugin data_dir.

        Args:
            relative: Relative path within data_dir.

        Returns:
            True if file exists.

        Raises:
            PermissionDeniedError: fs:read not granted or path escapes.
        """
        self._enforcer.check("fs:read")
        path = self._safe_path(relative)
        return path.exists()

    async def list_dir(self, relative: str = ".") -> list[str]:
        """List files in a directory within data_dir.

        Args:
            relative: Relative directory path. Default is data_dir root.

        Returns:
            List of filenames (not full paths).

        Raises:
            PermissionDeniedError: fs:read not granted or path escapes.
            FileNotFoundError: Directory doesn't exist.
        """
        self._enforcer.check("fs:read")
        path = self._safe_path(relative)

        if not path.exists():
            raise FileNotFoundError(f"Directory not found: {relative}")

        if not path.is_dir():
            raise FileNotFoundError(f"Not a directory: {relative}")

        entries = sorted(entry.name for entry in path.iterdir())
        logger.info(
            "plugin.fs.list_dir",
            **{
                "plugin_id": self._plugin,
                "plugin.fs.path_relative": relative,
                "plugin.fs.entry_count": len(entries),
            },
        )
        return entries

    @property
    def storage_used(self) -> int:
        """Total bytes used by this plugin."""
        return self._get_total_size()

    @property
    def storage_remaining(self) -> int:
        """Bytes remaining in storage budget."""
        return max(0, self._max_total - self._get_total_size())
