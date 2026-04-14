"""Sovyx Plugin Security — AST scanner + runtime ImportGuard.

Two complementary layers:
1. PluginSecurityScanner: Static AST analysis at install/validate time
2. ImportGuard: Runtime sys.meta_path hook blocking imports during plugin execution

Together they catch both static patterns (eval, exec, blocked imports)
and dynamic bypass attempts (__import__, importlib at runtime).

Spec: SPE-008-SANDBOX §9.1
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import sys
import typing
from importlib.abc import MetaPathFinder

from sovyx.observability.logging import get_logger

if typing.TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence
    from pathlib import Path

logger = get_logger(__name__)


# ── Security Finding ────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class SecurityFinding:
    """Result of a security scan check.

    Attributes:
        severity: "critical" | "warning" | "info"
        file: Source filename.
        line: Line number (0 if unknown).
        message: Human-readable description.
    """

    severity: str
    file: str
    line: int
    message: str


# ── AST Scanner ─────────────────────────────────────────────────────


class PluginSecurityScanner:
    """Static analysis of plugin source code.

    Scans Python AST for dangerous patterns BEFORE plugin is loaded.
    Used by: ``sovyx plugin validate``, CI pipeline, and at install time.

    Spec: SPE-008-SANDBOX §9.1
    """

    BLOCKED_IMPORTS: frozenset[str] = frozenset(
        {
            # Process / system access
            "os",
            "subprocess",
            "shutil",
            "sys",
            "resource",
            "signal",
            "multiprocessing",
            "threading",
            "pty",
            # Dynamic import / code execution
            "importlib",
            "builtins",
            "code",
            "codeop",
            "compileall",
            "inspect",
            "gc",
            # Memory / native access
            "ctypes",
            "mmap",
            # Unsafe serialization
            "pickle",
            "marshal",
            "shelve",
            "dill",
            # Filesystem (beyond sandboxed paths)
            "tempfile",
            # Network (beyond sandboxed HTTP)
            "socket",
            "http.server",
            "xmlrpc",
            # UI / interactive
            "webbrowser",
            "turtle",
            "tkinter",
        }
    )

    BLOCKED_CALLS: frozenset[str] = frozenset(
        {
            "eval",
            "exec",
            "compile",
            "__import__",
            "breakpoint",
            "open",  # plugins must go through SandboxedFsAccess
        }
    )

    BLOCKED_ATTRIBUTES: frozenset[str] = frozenset(
        {
            # Import / execution bypass
            "__import__",
            # Class hierarchy traversal (blocks the classic escape
            # `().__class__.__base__.__subclasses__()`)
            "__class__",
            "__subclasses__",
            "__bases__",
            "__base__",
            "__mro__",
            # Function / code introspection
            "__globals__",
            "__code__",
            "__closure__",
            "__defaults__",
            # Builtins access
            "__builtins__",
            # Stack / frame access
            "f_back",
            "f_locals",
            "f_globals",
            "f_builtins",
            "gi_frame",
            "cr_frame",
            # Attribute dictionary bypass
            "__dict__",
        }
    )

    ALLOWED_IMPORTS: frozenset[str] = frozenset(
        {
            "os.path",
            "pathlib",
            "json",
            "re",
            "datetime",
            "hashlib",
            "hmac",
            "base64",
            "dataclasses",
            "typing",
            "enum",
            "collections",
            "functools",
            "itertools",
            "math",
            "statistics",
            "uuid",
            "logging",
            "asyncio",
            "aiohttp",
            "pydantic",
        }
    )

    def scan_source(self, source: str, filename: str = "<plugin>") -> list[SecurityFinding]:
        """Scan a single source string.

        Args:
            source: Python source code.
            filename: Filename for error reporting.

        Returns:
            List of security findings.
        """
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as e:
            return [
                SecurityFinding(
                    severity="error",
                    file=filename,
                    line=e.lineno or 0,
                    message=f"Syntax error: {e.msg}",
                )
            ]
        return self._scan_tree(tree, filename)

    def scan_directory(self, source_dir: Path) -> list[SecurityFinding]:
        """Scan all Python files in a plugin directory.

        Args:
            source_dir: Directory containing plugin Python files.

        Returns:
            List of security findings across all files.
        """
        findings: list[SecurityFinding] = []
        for py_file in source_dir.rglob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            findings.extend(self.scan_source(source, py_file.name))
        return findings

    def _scan_tree(self, tree: ast.AST, filename: str) -> list[SecurityFinding]:
        """Walk AST and check for dangerous patterns."""
        findings: list[SecurityFinding] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                findings.extend(self._check_import(node, filename))
            elif isinstance(node, ast.ImportFrom):
                findings.extend(self._check_import_from(node, filename))
            elif isinstance(node, ast.Attribute):
                findings.extend(self._check_attribute(node, filename))
            elif isinstance(node, ast.Call):
                findings.extend(self._check_call(node, filename))

        return findings

    def _check_import(self, node: ast.Import, filename: str) -> list[SecurityFinding]:
        """Check ``import X`` statements."""
        findings: list[SecurityFinding] = []
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in self.BLOCKED_IMPORTS and alias.name not in self.ALLOWED_IMPORTS:
                findings.append(
                    SecurityFinding(
                        severity="critical",
                        file=filename,
                        line=node.lineno,
                        message=f"Blocked import: {alias.name}",
                    )
                )
        return findings

    def _check_import_from(self, node: ast.ImportFrom, filename: str) -> list[SecurityFinding]:
        """Check ``from X import Y`` statements."""
        if not node.module:
            return []
        root = node.module.split(".")[0]
        if root in self.BLOCKED_IMPORTS and node.module not in self.ALLOWED_IMPORTS:
            return [
                SecurityFinding(
                    severity="critical",
                    file=filename,
                    line=node.lineno,
                    message=f"Blocked import: {node.module}",
                )
            ]
        return []

    def _check_attribute(self, node: ast.Attribute, filename: str) -> list[SecurityFinding]:
        """Check dangerous attribute access."""
        if node.attr in self.BLOCKED_ATTRIBUTES:
            return [
                SecurityFinding(
                    severity="critical",
                    file=filename,
                    line=node.lineno,
                    message=f"Blocked attribute access: {node.attr}",
                )
            ]
        return []

    def _check_call(self, node: ast.Call, filename: str) -> list[SecurityFinding]:
        """Check dangerous function calls (eval, exec, etc.)."""
        if isinstance(node.func, ast.Name) and node.func.id in self.BLOCKED_CALLS:
            return [
                SecurityFinding(
                    severity="critical",
                    file=filename,
                    line=node.lineno,
                    message=f"Blocked function call: {node.func.id}",
                )
            ]
        return []

    def has_critical(self, findings: Sequence[SecurityFinding]) -> bool:
        """Check if any finding is critical severity."""
        return any(f.severity == "critical" for f in findings)


# ── ImportGuard (Runtime Hook) ──────────────────────────────────────


class ImportGuard(MetaPathFinder):
    """Runtime import hook blocking unauthorized imports during plugin execution.

    Installed on sys.meta_path when a plugin's code is executing.
    Catches dynamic imports that AST scanning can't detect:
    - ``__import__("os")`` at runtime
    - importlib usage after loading

    Usage::

        guard = ImportGuard("my-plugin", blocked={"os", "subprocess"})
        guard.install()
        try:
            plugin.setup(ctx)
        finally:
            guard.uninstall()

    Thread-safety: Each plugin gets its own guard instance.
    The guard only blocks NEW imports — already-imported modules
    in sys.modules are not blocked (they're already loaded).

    Spec: SPE-008-SANDBOX §4.2 (defense-in-depth)
    """

    def __init__(
        self,
        plugin_name: str,
        blocked: frozenset[str] | None = None,
        allowed: frozenset[str] | None = None,
    ) -> None:
        """Initialize ImportGuard.

        Args:
            plugin_name: Plugin name for logging.
            blocked: Set of blocked module root names.
                Defaults to PluginSecurityScanner.BLOCKED_IMPORTS.
            allowed: Set of explicitly allowed full module names.
                Defaults to PluginSecurityScanner.ALLOWED_IMPORTS.
        """
        self._plugin = plugin_name
        self._blocked = blocked or PluginSecurityScanner.BLOCKED_IMPORTS
        self._allowed = allowed or PluginSecurityScanner.ALLOWED_IMPORTS
        self._installed = False
        self._denial_count = 0

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: object = None,
    ) -> None:
        """PEP 451 MetaPathFinder hook — intercept import requests.

        Raises ImportError directly for blocked modules.
        Returns None to allow normal import.

        This is the MODERN API (Python 3.4+). ``find_module`` is
        deprecated and ignored in Python 3.12+.
        """
        root = fullname.split(".")[0]

        if root in self._blocked and fullname not in self._allowed:
            self._denial_count += 1
            logger.warning(
                "import_guard_blocked",
                plugin=self._plugin,
                module=fullname,
                count=self._denial_count,
            )
            msg = (
                f"Plugin '{self._plugin}' attempted to import blocked module "
                f"'{fullname}'. Use PluginContext methods instead."
            )
            raise ImportError(msg)

        return None  # Allow normal import

    def install(self) -> None:
        """Install on sys.meta_path (prepend for priority)."""
        if not self._installed:
            sys.meta_path.insert(0, self)
            self._installed = True

    def uninstall(self) -> None:
        """Remove from sys.meta_path."""
        if self._installed:
            with contextlib.suppress(ValueError):
                sys.meta_path.remove(self)
            self._installed = False

    @property
    def denial_count(self) -> int:
        """Number of blocked import attempts."""
        return self._denial_count

    @property
    def is_installed(self) -> bool:
        """Whether the guard is currently active."""
        return self._installed

    def __enter__(self) -> ImportGuard:
        """Context manager — install guard."""
        self.install()
        return self

    def __exit__(self, *args: object) -> None:
        """Context manager — uninstall guard."""
        self.uninstall()
