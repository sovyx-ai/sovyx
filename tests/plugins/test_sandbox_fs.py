"""Tests for Sovyx Plugin Sandbox FS — scoped filesystem access.

Coverage target: ≥95% on plugins/sandbox_fs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sovyx.plugins.permissions import PermissionDeniedError, PermissionEnforcer
from sovyx.plugins.sandbox_fs import _MAX_FILE_BYTES, SandboxedFsAccess

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def enforcer_rw() -> PermissionEnforcer:
    return PermissionEnforcer("test", {"fs:read", "fs:write"})


@pytest.fixture()
def enforcer_ro() -> PermissionEnforcer:
    return PermissionEnforcer("test", {"fs:read"})


@pytest.fixture()
def enforcer_none() -> PermissionEnforcer:
    return PermissionEnforcer("test", set())


@pytest.fixture()
def fs_rw(tmp_path: Path, enforcer_rw: PermissionEnforcer) -> SandboxedFsAccess:
    return SandboxedFsAccess("test", tmp_path, enforcer_rw)


@pytest.fixture()
def fs_ro(tmp_path: Path, enforcer_ro: PermissionEnforcer) -> SandboxedFsAccess:
    return SandboxedFsAccess("test", tmp_path, enforcer_ro)


# ── Path Safety ─────────────────────────────────────────────────────


class TestPathSafety:
    """Tests for path traversal and symlink protection."""

    def test_normal_path(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        path = fs_rw._safe_path("data.json")
        assert path == tmp_path / "data.json"

    def test_nested_path(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        path = fs_rw._safe_path("subdir/data.json")
        assert path == tmp_path / "subdir" / "data.json"

    def test_traversal_blocked(self, fs_rw: SandboxedFsAccess) -> None:
        with pytest.raises(PermissionDeniedError, match="escapes"):
            fs_rw._safe_path("../../etc/passwd")

    def test_absolute_path_blocked(self, fs_rw: SandboxedFsAccess) -> None:
        with pytest.raises(PermissionDeniedError, match="Absolute"):
            fs_rw._safe_path("/etc/passwd")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "Path.symlink_to requires SeCreateSymbolicLinkPrivilege on Windows, "
            "which pytest runners rarely hold — OSError(WinError 1314) blocks "
            "the fixture before the sandbox guard runs. The escape path is "
            "covered on every platform via test_absolute_path_blocked + the "
            "resolve() call in ``_safe_path``; the symlink-specific branch is "
            "exercised on POSIX CI."
        ),
    )
    def test_symlink_escape_blocked(self, tmp_path: Path, enforcer_rw: PermissionEnforcer) -> None:
        """Symlink pointing outside data_dir is blocked."""
        data_dir = tmp_path / "plugin_data"
        data_dir.mkdir()
        # Create symlink: plugin_data/escape → /tmp
        link = data_dir / "escape"
        link.symlink_to("/tmp")

        fs = SandboxedFsAccess("test", data_dir, enforcer_rw)
        with pytest.raises(PermissionDeniedError, match="escapes"):
            fs._safe_path("escape/evil.txt")


# ── Read Operations ─────────────────────────────────────────────────


class TestRead:
    """Tests for read operations."""

    @pytest.mark.anyio()
    async def test_read_text(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("world", encoding="utf-8")
        content = await fs_rw.read("hello.txt")
        assert content == "world"

    @pytest.mark.anyio()
    async def test_read_bytes(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02")
        content = await fs_rw.read_bytes("data.bin")
        assert content == b"\x00\x01\x02"

    @pytest.mark.anyio()
    async def test_read_not_found(self, fs_rw: SandboxedFsAccess) -> None:
        with pytest.raises(FileNotFoundError):
            await fs_rw.read("nonexistent.txt")

    @pytest.mark.anyio()
    async def test_read_bytes_not_found(self, fs_rw: SandboxedFsAccess) -> None:
        with pytest.raises(FileNotFoundError):
            await fs_rw.read_bytes("nonexistent.bin")

    @pytest.mark.anyio()
    async def test_read_denied_without_permission(
        self, tmp_path: Path, enforcer_none: PermissionEnforcer
    ) -> None:
        fs = SandboxedFsAccess("test", tmp_path, enforcer_none)
        with pytest.raises(PermissionDeniedError):
            await fs.read("any.txt")


# ── Write Operations ────────────────────────────────────────────────


class TestWrite:
    """Tests for write operations."""

    @pytest.mark.anyio()
    async def test_write_text(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        await fs_rw.write("output.txt", "hello world")
        assert (tmp_path / "output.txt").read_text() == "hello world"

    @pytest.mark.anyio()
    async def test_write_bytes(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        await fs_rw.write_bytes("data.bin", b"\xff\xfe")
        assert (tmp_path / "data.bin").read_bytes() == b"\xff\xfe"

    @pytest.mark.anyio()
    async def test_write_creates_subdirs(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        await fs_rw.write("sub/dir/file.txt", "nested")
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"

    @pytest.mark.anyio()
    async def test_write_denied_readonly(self, fs_ro: SandboxedFsAccess) -> None:
        with pytest.raises(PermissionDeniedError):
            await fs_ro.write("test.txt", "nope")

    @pytest.mark.anyio()
    async def test_write_file_too_large(self, fs_rw: SandboxedFsAccess) -> None:
        data = "x" * (_MAX_FILE_BYTES + 1)
        with pytest.raises(PermissionDeniedError, match="too large"):
            await fs_rw.write("big.txt", data)

    @pytest.mark.anyio()
    async def test_write_storage_budget(
        self, tmp_path: Path, enforcer_rw: PermissionEnforcer
    ) -> None:
        """Total storage budget enforced."""
        fs = SandboxedFsAccess("test", tmp_path, enforcer_rw, max_total_bytes=100)
        await fs.write("a.txt", "x" * 60)
        with pytest.raises(PermissionDeniedError, match="budget"):
            await fs.write("b.txt", "x" * 60)  # 60 + 60 > 100


# ── Delete ──────────────────────────────────────────────────────────


class TestDelete:
    """Tests for delete operations."""

    @pytest.mark.anyio()
    async def test_delete_file(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        (tmp_path / "to_delete.txt").write_text("bye")
        result = await fs_rw.delete("to_delete.txt")
        assert result is True
        assert not (tmp_path / "to_delete.txt").exists()

    @pytest.mark.anyio()
    async def test_delete_nonexistent(self, fs_rw: SandboxedFsAccess) -> None:
        result = await fs_rw.delete("ghost.txt")
        assert result is False

    @pytest.mark.anyio()
    async def test_delete_directory_blocked(
        self, fs_rw: SandboxedFsAccess, tmp_path: Path
    ) -> None:
        (tmp_path / "mydir").mkdir()
        with pytest.raises(PermissionDeniedError, match="Cannot delete directory"):
            await fs_rw.delete("mydir")

    @pytest.mark.anyio()
    async def test_delete_denied_readonly(self, fs_ro: SandboxedFsAccess) -> None:
        with pytest.raises(PermissionDeniedError):
            await fs_ro.delete("any.txt")


# ── Exists + List ───────────────────────────────────────────────────


class TestExistsAndList:
    """Tests for exists and list_dir."""

    @pytest.mark.anyio()
    async def test_exists_true(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        (tmp_path / "exists.txt").write_text("yes")
        assert await fs_rw.exists("exists.txt") is True

    @pytest.mark.anyio()
    async def test_exists_false(self, fs_rw: SandboxedFsAccess) -> None:
        assert await fs_rw.exists("nope.txt") is False

    @pytest.mark.anyio()
    async def test_list_dir(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        files = await fs_rw.list_dir()
        assert files == ["a.txt", "b.txt"]

    @pytest.mark.anyio()
    async def test_list_subdir(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("c")
        files = await fs_rw.list_dir("sub")
        assert files == ["c.txt"]

    @pytest.mark.anyio()
    async def test_list_nonexistent(self, fs_rw: SandboxedFsAccess) -> None:
        with pytest.raises(FileNotFoundError):
            await fs_rw.list_dir("nope")

    @pytest.mark.anyio()
    async def test_list_file_not_dir(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("x")
        with pytest.raises(FileNotFoundError, match="Not a directory"):
            await fs_rw.list_dir("file.txt")


# ── Properties ──────────────────────────────────────────────────────


class TestProperties:
    """Tests for storage properties."""

    @pytest.mark.anyio()
    async def test_storage_used(self, fs_rw: SandboxedFsAccess, tmp_path: Path) -> None:
        await fs_rw.write("data.txt", "hello")  # 5 bytes
        assert fs_rw.storage_used >= 5

    @pytest.mark.anyio()
    async def test_storage_remaining(
        self, tmp_path: Path, enforcer_rw: PermissionEnforcer
    ) -> None:
        fs = SandboxedFsAccess("test", tmp_path, enforcer_rw, max_total_bytes=1000)
        assert fs.storage_remaining == 1000
        await fs.write("data.txt", "x" * 100)
        assert fs.storage_remaining <= 900

    def test_data_dir_created(self, tmp_path: Path, enforcer_rw: PermissionEnforcer) -> None:
        """data_dir is created if it doesn't exist."""
        new_dir = tmp_path / "new_plugin"
        assert not new_dir.exists()
        SandboxedFsAccess("test", new_dir, enforcer_rw)
        assert new_dir.exists()
