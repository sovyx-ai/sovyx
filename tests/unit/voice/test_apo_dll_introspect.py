"""Tests for :mod:`sovyx.voice._apo_dll_introspect` (WI3).

Covers CLSID→DLL path resolution + DLL version-info introspection
with mocked winreg + win32api so the suite stays cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest  # noqa: TC002 — runtime use via monkeypatch fixture types

from sovyx.voice._apo_dll_introspect import (
    ApoDllInfo,
    introspect_apo_clsid,
    query_dll_version_info,
    resolve_clsid_to_dll_path,
)

# ── winreg fake ────────────────────────────────────────────────────


def _fake_winreg(
    *,
    default_value: str | None = r"%SystemRoot%\System32\fakeapo.dll",
    open_error: type[BaseException] | None = None,
    query_error: type[BaseException] | None = None,
) -> ModuleType:
    module = ModuleType("winreg")
    module.HKEY_LOCAL_MACHINE = 0x80000002  # type: ignore[attr-defined]
    fake_key = MagicMock(name="fake_key")

    def open_key(_hive: int, _path: str) -> Any:
        if open_error is not None:
            raise open_error("simulated open failure")
        return fake_key

    def query_value_ex(_key: Any, name: str) -> tuple[Any, int]:
        if query_error is not None:
            raise query_error("simulated query failure")
        if name == "":
            if default_value is None:
                raise FileNotFoundError("Default value missing")
            return default_value, 1
        raise FileNotFoundError(name)

    def close_key(_key: Any) -> None:
        return None

    module.OpenKey = open_key  # type: ignore[attr-defined]
    module.QueryValueEx = query_value_ex  # type: ignore[attr-defined]
    module.CloseKey = close_key  # type: ignore[attr-defined]
    return module


# ── win32api fake ──────────────────────────────────────────────────


def _fake_win32api(
    *,
    file_version_ms: int = (10 << 16) | 0,
    file_version_ls: int = (26100 << 16) | 4351,
    translations: list[tuple[int, int]] | None = None,
    string_fields: dict[str, str] | None = None,
    raise_on_root: type[BaseException] | None = None,
) -> ModuleType:
    module = ModuleType("win32api")
    if translations is None:
        translations = [(0x0409, 0x04B0)]
    if string_fields is None:
        string_fields = {
            "FileVersion": "10.0.26100.4351",
            "ProductName": "Microsoft® Windows® Operating System",
            "CompanyName": "Microsoft Corporation",
            "FileDescription": "Windows Voice Clarity Effect Pack",
        }

    def get_file_version_info(_path: str, query: str) -> Any:
        if query == "\\":
            if raise_on_root is not None:
                raise raise_on_root("root query failure")
            return {"FileVersionMS": file_version_ms, "FileVersionLS": file_version_ls}
        if query == r"\VarFileInfo\Translation":
            return translations
        # \StringFileInfo\<lang><cp>\<field>
        prefix = r"\StringFileInfo\\"
        if query.startswith(prefix.rstrip("\\")):
            field = query.rsplit("\\", 1)[-1]
            if field in string_fields:
                return string_fields[field]
            raise FileNotFoundError(field)
        raise FileNotFoundError(query)

    module.GetFileVersionInfo = get_file_version_info  # type: ignore[attr-defined]
    return module


# ── resolve_clsid_to_dll_path ──────────────────────────────────────


class TestResolveClsid:
    def test_non_windows_returns_empty(self) -> None:
        with patch.object(sys, "platform", "linux"):
            path, notes = resolve_clsid_to_dll_path("{ABC}")
        assert path == ""
        assert any("non-windows" in n for n in notes)

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason=(
            "os.path.expandvars expands %VAR% syntax only on Windows; "
            "on POSIX it's a no-op so the assertion can't hold. The "
            "non-windows path is covered by test_non_windows_returns_empty."
        ),
    )
    def test_windows_resolves_and_expands_envvars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        fake = _fake_winreg(default_value=r"%SystemRoot%\System32\fakeapo.dll")
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            path, notes = resolve_clsid_to_dll_path("{ABC-DEF}")
        assert path == r"C:\Windows\System32\fakeapo.dll"
        assert notes == []

    def test_missing_clsid_returns_empty_with_note(self) -> None:
        fake = _fake_winreg(open_error=FileNotFoundError)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            path, notes = resolve_clsid_to_dll_path("{NOT-INSTALLED}")
        assert path == ""
        assert any("absent" in n for n in notes)

    def test_open_oserror_returns_empty_with_note(self) -> None:
        fake = _fake_winreg(open_error=PermissionError)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            path, notes = resolve_clsid_to_dll_path("{ABC}")
        assert path == ""
        assert any("open failed" in n for n in notes)

    def test_default_value_absent_returns_empty(self) -> None:
        fake = _fake_winreg(default_value=None)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            path, notes = resolve_clsid_to_dll_path("{ABC}")
        assert path == ""
        assert any("Default value read failed" in n for n in notes)

    def test_default_value_empty_string_returns_empty(self) -> None:
        fake = _fake_winreg(default_value="")
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            path, notes = resolve_clsid_to_dll_path("{ABC}")
        assert path == ""
        assert any("empty or non-string" in n for n in notes)


# ── query_dll_version_info ─────────────────────────────────────────


class TestQueryDllVersionInfo:
    def test_empty_path_returns_empty_info(self) -> None:
        info = query_dll_version_info("")
        assert info.dll_path == ""
        assert info.file_exists is False
        assert "empty" in info.notes[0]

    def test_nonexistent_file_returns_empty_with_note(self, tmp_path: Path) -> None:
        bogus = tmp_path / "does-not-exist.dll"
        info = query_dll_version_info(str(bogus))
        assert info.dll_path == str(bogus)
        assert info.file_exists is False
        assert info.file_size_bytes == 0
        assert any("orphan registration" in n for n in info.notes)

    def test_non_windows_returns_path_only(self, tmp_path: Path) -> None:
        # Create a real file so file_exists trips, then non-Windows
        # branch returns without trying to query version info.
        f = tmp_path / "fake.dll"
        f.write_bytes(b"MZ\x00\x00")  # tiny PE header stub
        with patch.object(sys, "platform", "linux"):
            info = query_dll_version_info(str(f))
        assert info.file_exists is True
        assert info.file_size_bytes == 4  # noqa: PLR2004
        assert info.file_version == ""
        assert any("non-windows" in n for n in info.notes)

    def test_windows_pywin32_missing_returns_path_only(self, tmp_path: Path) -> None:
        f = tmp_path / "fake.dll"
        f.write_bytes(b"MZ" + b"\x00" * 100)
        # Inject ImportError for win32api by removing it from
        # sys.modules and pre-empting the import path.
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"win32api": None}),
        ):
            info = query_dll_version_info(str(f))
        assert info.file_exists is True
        assert info.file_version == ""
        assert any("pywin32 not installed" in n for n in info.notes)

    def test_windows_with_pywin32_populates_version_fields(
        self,
        tmp_path: Path,
    ) -> None:
        f = tmp_path / "fake.dll"
        f.write_bytes(b"MZ" + b"\x00" * 1000)
        fake_win32 = _fake_win32api()
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"win32api": fake_win32}),
        ):
            info = query_dll_version_info(str(f))
        assert info.file_exists is True
        assert info.file_version == "10.0.26100.4351"
        assert info.product_name == "Microsoft® Windows® Operating System"
        assert info.company_name == "Microsoft Corporation"
        assert "Voice Clarity" in info.file_description
        assert info.is_microsoft_signed is True

    def test_root_version_info_failure_returns_partial(self, tmp_path: Path) -> None:
        f = tmp_path / "fake.dll"
        f.write_bytes(b"MZ" + b"\x00" * 100)
        fake_win32 = _fake_win32api(raise_on_root=RuntimeError)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"win32api": fake_win32}),
        ):
            info = query_dll_version_info(str(f))
        assert info.file_exists is True
        assert info.file_version == ""
        assert any("FileVersionInfo failed" in n for n in info.notes)

    def test_packed_version_falls_back_when_string_field_absent(
        self,
        tmp_path: Path,
    ) -> None:
        f = tmp_path / "fake.dll"
        f.write_bytes(b"MZ" + b"\x00" * 50)
        # No translations advertised → string fields empty → falls
        # back to packed FileVersion from FileInfo block.
        fake_win32 = _fake_win32api(translations=[])
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"win32api": fake_win32}),
        ):
            info = query_dll_version_info(str(f))
        # Packed version derived from FileVersionMS/LS.
        assert info.file_version == "10.0.26100.4351"


# ── introspect_apo_clsid (aggregation) ─────────────────────────────


class TestIntrospectApoClsid:
    def test_combines_resolution_and_introspection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        f = tmp_path / "fake.dll"
        f.write_bytes(b"MZ" + b"\x00" * 100)
        fake_winreg = _fake_winreg(default_value=str(f))
        fake_win32 = _fake_win32api()
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake_winreg, "win32api": fake_win32}),
        ):
            info = introspect_apo_clsid("{ABC}")
        assert info.dll_path == str(f)
        assert info.file_exists is True
        assert info.company_name == "Microsoft Corporation"

    def test_resolution_failure_returns_empty_info_with_notes(self) -> None:
        fake_winreg = _fake_winreg(open_error=FileNotFoundError)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake_winreg}),
        ):
            info = introspect_apo_clsid("{NOT-INSTALLED}")
        assert info.dll_path == ""
        assert info.file_exists is False
        assert any("absent" in n for n in info.notes)


# ── Report contract ────────────────────────────────────────────────


class TestApoDllInfoContract:
    def test_default_construction_is_safe(self) -> None:
        info = ApoDllInfo()
        assert info.dll_path == ""
        assert info.file_exists is False
        assert info.is_microsoft_signed is False
        assert info.notes == ()

    def test_microsoft_signed_predicate_case_insensitive(self) -> None:
        for variant in (
            "Microsoft Corporation",
            "microsoft Corporation",
            "MICROSOFT Corporation",
        ):
            info = ApoDllInfo(company_name=variant)
            assert info.is_microsoft_signed is True

    def test_non_microsoft_company_returns_false(self) -> None:
        info = ApoDllInfo(company_name="Acme Audio Inc.")
        assert info.is_microsoft_signed is False
