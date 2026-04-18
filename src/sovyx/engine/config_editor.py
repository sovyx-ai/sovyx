"""Config editor — safe YAML updates preserving comments and structure.

Uses ruamel.yaml to round-trip mind.yaml without destroying comments,
ordering, or formatting. Writes atomically (temp file + rename) and
uses a per-file asyncio lock to prevent concurrent edits.

Usage::

    editor = ConfigEditor()
    await editor.update_section(
        path=Path("~/.sovyx/my-mind/mind.yaml"),
        section="plugins_config.caldav",
        data={"base_url": "https://...", "username": "me"},
    )
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from sovyx.engine._lock_dict import LRULockDict
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.default_flow_style = False


class ConfigEditor:
    """Thread-safe YAML config editor with comment preservation.

    One lock per file path prevents concurrent writes. Atomic write
    via temp file + rename prevents partial writes on crash.
    """

    def __init__(self, max_locks: int = 64) -> None:
        self._locks: LRULockDict[str] = LRULockDict(maxsize=max_locks)

    async def update_section(
        self,
        path: Path,
        section: str,
        data: dict[str, Any],
    ) -> None:
        """Update a dotted section in a YAML file.

        Args:
            path: Path to the YAML file.
            section: Dotted key path (e.g. ``"plugins_config.caldav"``).
            data: Dict to merge into the section.
        """
        resolved = path.expanduser().resolve()
        async with self._locks[str(resolved)]:
            await asyncio.to_thread(self._write_section, resolved, section, data)

        logger.info("config_updated", path=str(resolved), section=section)

    async def read_section(
        self,
        path: Path,
        section: str,
    ) -> dict[str, Any]:
        """Read a dotted section from a YAML file.

        Returns:
            Dict of the section contents, or empty dict if missing.
        """
        resolved = path.expanduser().resolve()
        return await asyncio.to_thread(self._read_section, resolved, section)

    async def set_scalar(
        self,
        path: Path,
        key: str,
        value: Any,  # noqa: ANN401  -- any YAML scalar (str/int/float/bool/None)
    ) -> None:
        """Set a top-level (or dotted) scalar field in a YAML file.

        Unlike :meth:`update_section`, this sets the *leaf* directly to
        ``value`` rather than merging into a dict at ``leaf``. Use this
        for pydantic scalar fields (``voice_id``, ``language``, …) that
        live at the root of ``mind.yaml``.

        Args:
            path: Path to the YAML file.
            key: Dotted key path (usually a bare field name for root scalars).
            value: Scalar value to write (str / int / float / bool / None).
        """
        resolved = path.expanduser().resolve()
        async with self._locks[str(resolved)]:
            await asyncio.to_thread(self._write_scalar, resolved, key, value)

        logger.info("config_scalar_updated", path=str(resolved), key=key)

    @staticmethod
    def _write_section(path: Path, section: str, data: dict[str, Any]) -> None:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                doc = _yaml.load(f)
            if doc is None:
                doc = {}
        else:
            doc = {}

        keys = section.split(".")
        node = doc
        for key in keys[:-1]:
            if key not in node or not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]

        leaf = keys[-1]
        if leaf not in node or not isinstance(node[leaf], dict):
            node[leaf] = {}
        for k, v in data.items():
            node[leaf][k] = v

        dir_path = path.parent
        dir_path.mkdir(parents=True, exist_ok=True)

        import os

        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".yaml.tmp", prefix=".sovyx_")
        os.close(fd)
        tmp = Path(tmp_path)
        try:
            with tmp.open("w", encoding="utf-8") as f:
                _yaml.dump(doc, f)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_scalar(path: Path, key: str, value: Any) -> None:  # noqa: ANN401

        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                doc = _yaml.load(f)
            if doc is None:
                doc = {}
        else:
            doc = {}

        keys = key.split(".")
        node = doc
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value

        dir_path = path.parent
        dir_path.mkdir(parents=True, exist_ok=True)

        import os

        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".yaml.tmp", prefix=".sovyx_")
        os.close(fd)
        tmp = Path(tmp_path)
        try:
            with tmp.open("w", encoding="utf-8") as f:
                _yaml.dump(doc, f)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read_section(path: Path, section: str) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            doc = _yaml.load(f)
        if doc is None:
            return {}

        keys = section.split(".")
        node = doc
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return {}
            node = node[key]
        return dict(node) if isinstance(node, dict) else {}
