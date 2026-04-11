"""Tests for Sovyx Plugin Security — AST scanner + ImportGuard.

Coverage target: ≥95% on plugins/security.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sovyx.plugins.security import (
    ImportGuard,
    PluginSecurityScanner,
    SecurityFinding,
)

# ── SecurityFinding ─────────────────────────────────────────────────


class TestSecurityFinding:
    """Tests for SecurityFinding dataclass."""

    def test_create(self) -> None:
        f = SecurityFinding("critical", "plugin.py", 10, "bad import")
        assert f.severity == "critical"
        assert f.file == "plugin.py"
        assert f.line == 10
        assert f.message == "bad import"

    def test_frozen(self) -> None:
        f = SecurityFinding("warning", "x.py", 1, "msg")
        with pytest.raises(AttributeError):
            f.severity = "info"  # type: ignore[misc]


# ── AST Scanner: Blocked Imports ────────────────────────────────────


class TestBlockedImports:
    """Tests for import blocking in AST scanner."""

    def setup_method(self) -> None:
        self.scanner = PluginSecurityScanner()

    def test_import_os(self) -> None:
        findings = self.scanner.scan_source("import os")
        assert len(findings) == 1
        assert findings[0].severity == "critical"
        assert "os" in findings[0].message

    def test_import_subprocess(self) -> None:
        findings = self.scanner.scan_source("import subprocess")
        assert len(findings) == 1
        assert "subprocess" in findings[0].message

    def test_from_os_import(self) -> None:
        findings = self.scanner.scan_source("from os import system")
        assert len(findings) == 1

    def test_from_os_path_allowed(self) -> None:
        """os.path is in ALLOWED_IMPORTS."""
        findings = self.scanner.scan_source("from os.path import join")
        assert len(findings) == 0

    def test_import_sys(self) -> None:
        findings = self.scanner.scan_source("import sys")
        assert len(findings) == 1

    def test_import_ctypes(self) -> None:
        findings = self.scanner.scan_source("import ctypes")
        assert len(findings) == 1

    def test_import_pickle(self) -> None:
        findings = self.scanner.scan_source("import pickle")
        assert len(findings) == 1

    def test_import_socket(self) -> None:
        findings = self.scanner.scan_source("import socket")
        assert len(findings) == 1

    def test_import_multiprocessing(self) -> None:
        findings = self.scanner.scan_source("import multiprocessing")
        assert len(findings) == 1

    def test_import_threading(self) -> None:
        findings = self.scanner.scan_source("import threading")
        assert len(findings) == 1

    def test_import_importlib(self) -> None:
        findings = self.scanner.scan_source("import importlib")
        assert len(findings) == 1

    def test_from_shutil(self) -> None:
        findings = self.scanner.scan_source("from shutil import rmtree")
        assert len(findings) == 1


# ── AST Scanner: Allowed Imports ────────────────────────────────────


class TestAllowedImports:
    """Tests for safe imports that should pass."""

    def setup_method(self) -> None:
        self.scanner = PluginSecurityScanner()

    def test_import_json(self) -> None:
        assert self.scanner.scan_source("import json") == []

    def test_import_datetime(self) -> None:
        assert self.scanner.scan_source("import datetime") == []

    def test_import_re(self) -> None:
        assert self.scanner.scan_source("import re") == []

    def test_import_asyncio(self) -> None:
        assert self.scanner.scan_source("import asyncio") == []

    def test_import_pydantic(self) -> None:
        assert self.scanner.scan_source("import pydantic") == []

    def test_import_typing(self) -> None:
        assert self.scanner.scan_source("import typing") == []

    def test_import_pathlib(self) -> None:
        assert self.scanner.scan_source("from pathlib import Path") == []

    def test_import_math(self) -> None:
        assert self.scanner.scan_source("import math") == []


# ── AST Scanner: Blocked Calls ──────────────────────────────────────


class TestBlockedCalls:
    """Tests for dangerous function call detection."""

    def setup_method(self) -> None:
        self.scanner = PluginSecurityScanner()

    def test_eval(self) -> None:
        findings = self.scanner.scan_source('eval("1+1")')
        assert len(findings) == 1
        assert "eval" in findings[0].message

    def test_exec(self) -> None:
        findings = self.scanner.scan_source('exec("print(1)")')
        assert len(findings) == 1
        assert "exec" in findings[0].message

    def test_compile(self) -> None:
        findings = self.scanner.scan_source('compile("x", "", "exec")')
        assert len(findings) == 1

    def test_dunder_import(self) -> None:
        findings = self.scanner.scan_source('__import__("os")')
        assert len(findings) == 1


# ── AST Scanner: Blocked Attributes ─────────────────────────────────


class TestBlockedAttributes:
    """Tests for dangerous attribute access detection."""

    def setup_method(self) -> None:
        self.scanner = PluginSecurityScanner()

    def test_subclasses(self) -> None:
        findings = self.scanner.scan_source("x.__subclasses__()")
        assert len(findings) >= 1
        assert any("__subclasses__" in f.message for f in findings)

    def test_globals(self) -> None:
        findings = self.scanner.scan_source("x.__globals__")
        assert any("__globals__" in f.message for f in findings)

    def test_code(self) -> None:
        findings = self.scanner.scan_source("func.__code__")
        assert any("__code__" in f.message for f in findings)

    def test_builtins(self) -> None:
        findings = self.scanner.scan_source("x.__builtins__")
        assert any("__builtins__" in f.message for f in findings)


# ── AST Scanner: Syntax Errors ──────────────────────────────────────


class TestSyntaxErrors:
    """Tests for handling malformed Python source."""

    def test_syntax_error(self) -> None:
        scanner = PluginSecurityScanner()
        findings = scanner.scan_source("def f(\n  broken", "bad.py")
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "Syntax error" in findings[0].message
        assert findings[0].file == "bad.py"


# ── AST Scanner: Multiple Issues ────────────────────────────────────


class TestMultipleIssues:
    """Tests for source with multiple security issues."""

    def test_multiple_findings(self) -> None:
        source = """
import os
import subprocess
eval("x")
obj.__globals__
"""
        scanner = PluginSecurityScanner()
        findings = scanner.scan_source(source)
        assert len(findings) >= 4

    def test_has_critical(self) -> None:
        scanner = PluginSecurityScanner()
        findings = scanner.scan_source("import os")
        assert scanner.has_critical(findings) is True

    def test_no_critical(self) -> None:
        scanner = PluginSecurityScanner()
        findings = scanner.scan_source("import json")
        assert scanner.has_critical(findings) is False


# ── AST Scanner: Directory Scan ─────────────────────────────────────


class TestDirectoryScan:
    """Tests for scanning a directory of Python files."""

    def test_scan_directory(self, tmp_path: Path) -> None:
        (tmp_path / "safe.py").write_text("import json\nx = 1")
        (tmp_path / "dangerous.py").write_text("import os\nimport subprocess")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.py").write_text('eval("1")')

        scanner = PluginSecurityScanner()
        findings = scanner.scan_directory(tmp_path)
        assert len(findings) == 3  # os, subprocess, eval
        files = {f.file for f in findings}
        assert "dangerous.py" in files
        assert "nested.py" in files

    def test_scan_empty_directory(self, tmp_path: Path) -> None:
        scanner = PluginSecurityScanner()
        findings = scanner.scan_directory(tmp_path)
        assert findings == []


# ── AST Scanner: Clean Plugin ───────────────────────────────────────


class TestCleanPlugin:
    """Tests that a well-written plugin passes scanning."""

    def test_typical_plugin(self) -> None:
        source = '''
import json
import logging
from typing import Any
from datetime import datetime
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class WeatherData:
    temp: float
    city: str

async def get_weather(city: str) -> str:
    """Get weather for a city."""
    return json.dumps({"city": city, "temp": 20.0})
'''
        scanner = PluginSecurityScanner()
        findings = scanner.scan_source(source)
        assert findings == []


# ── ImportGuard ─────────────────────────────────────────────────────


class TestImportGuard:
    """Tests for runtime ImportGuard."""

    def test_install_uninstall(self) -> None:
        guard = ImportGuard("test")
        assert guard.is_installed is False
        guard.install()
        assert guard.is_installed is True
        assert guard in sys.meta_path
        guard.uninstall()
        assert guard.is_installed is False
        assert guard not in sys.meta_path

    def test_double_install(self) -> None:
        guard = ImportGuard("test")
        guard.install()
        guard.install()  # idempotent
        assert sys.meta_path.count(guard) == 1
        guard.uninstall()

    def test_double_uninstall(self) -> None:
        guard = ImportGuard("test")
        guard.install()
        guard.uninstall()
        guard.uninstall()  # idempotent, no error

    def test_context_manager(self) -> None:
        guard = ImportGuard("test")
        with guard:
            assert guard.is_installed is True
        assert guard.is_installed is False

    def test_find_module_blocks(self) -> None:
        guard = ImportGuard("test")
        result = guard.find_module("os")
        assert result is guard  # Returns self to block

    def test_find_module_allows(self) -> None:
        guard = ImportGuard("test")
        result = guard.find_module("json")
        assert result is None  # Allows

    def test_find_module_allows_os_path(self) -> None:
        guard = ImportGuard("test")
        result = guard.find_module("os.path")
        assert result is None  # In ALLOWED_IMPORTS

    def test_load_module_raises(self) -> None:
        guard = ImportGuard("test")
        with pytest.raises(ImportError, match="blocked module"):
            guard.load_module("os")

    def test_denial_count(self) -> None:
        guard = ImportGuard("test")
        guard.find_module("os")
        guard.find_module("subprocess")
        guard.find_module("json")  # allowed, no count
        assert guard.denial_count == 2

    def test_custom_blocked_set(self) -> None:
        guard = ImportGuard("test", blocked=frozenset({"custom_dangerous"}))
        assert guard.find_module("custom_dangerous") is guard
        assert guard.find_module("os") is None  # Not in custom set

    def test_custom_allowed_set(self) -> None:
        guard = ImportGuard(
            "test",
            blocked=frozenset({"mymod"}),
            allowed=frozenset({"mymod.safe"}),
        )
        assert guard.find_module("mymod") is guard  # blocked
        assert guard.find_module("mymod.safe") is None  # explicitly allowed


import sys
