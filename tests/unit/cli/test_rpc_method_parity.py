"""AP #53 closure — CLI ``DaemonClient.call`` literals ↔ registered RPC methods.

DOCTOR-1 shipped because ``sovyx doctor`` called an RPC method
(``"doctor"``) that no daemon ever registered: the producer and the
consumer of the method-name contract lived as independent string
literals with nothing tying them together. This module makes the whole
class unshippable:

* the CONSUMER side is collected by AST-scanning every string literal
  passed to ``.call(...)`` under ``src/sovyx/cli/``;
* the PRODUCER side is collected twice — by AST-scanning every string
  literal passed to ``.register_method(...)`` under ``src/sovyx/``
  (covers the inline ``status``/``shutdown`` registrations in
  ``cli/main.py``) AND at runtime by building a ``DaemonRPCServer``
  through ``register_cli_handlers`` exactly like ``cli/main.py::start``
  does (guards against dynamic names the AST cannot see);
* every consumer literal must be producible, modulo an explicit,
  self-sunsetting allowlist of pre-existing defects.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import sovyx
import sovyx.cli
from sovyx.engine._rpc_handlers import register_cli_handlers
from sovyx.engine.rpc_server import DaemonRPCServer

_SRC_ROOT = Path(sovyx.__file__).resolve().parent
_CLI_ROOT = Path(sovyx.cli.__file__).resolve().parent

_KNOWN_UNREGISTERED: frozenset[str] = frozenset(
    {
        # Pre-existing AP #53 defects OUTSIDE the DOCTOR-1 wave's scope:
        # these commands have called methods no daemon ever registered
        # since they shipped (same class as DOCTOR-1, separate findings).
        # Fixing one REQUIRES removing it here —
        # test_known_unregistered_allowlist_not_stale enforces cleanup.
        # Adding NEW entries is forbidden: register the handler instead.
        "brain.search",  # cli/main.py::brain_search
        "brain.stats",  # cli/main.py::brain_stats
        "mind.status",  # cli/main.py::mind_status
    },
)


def _string_literal_args(root: Path, attr: str, text_hint: str) -> list[tuple[str, str, int]]:
    """Collect ``(literal, relative_path, lineno)`` for ``*.<attr>("literal", ...)``.

    ``text_hint`` pre-filters files by substring before parsing so the
    scan stays fast across the whole package.
    """
    found: list[tuple[str, str, int]] = []
    for py in sorted(root.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        if text_hint not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                rel = py.relative_to(_SRC_ROOT.parent).as_posix()
                found.append((node.args[0].value, rel, node.lineno))
    return found


def _cli_call_literals() -> list[tuple[str, str, int]]:
    """Every string literal the CLI passes to ``DaemonClient.call``."""
    return _string_literal_args(_CLI_ROOT, "call", ".call(")


def _ast_registered_methods() -> set[str]:
    """Every string literal passed to ``register_method`` under src/sovyx."""
    return {
        name
        for name, _, _ in _string_literal_args(_SRC_ROOT, "register_method", "register_method")
    }


def _runtime_registered_methods() -> set[str]:
    """Method names actually registered by ``register_cli_handlers``.

    Builds the server the same way ``cli/main.py::start`` does
    (constructor performs no I/O; registration resolves nothing from
    the registry, so a MagicMock suffices).
    """
    rpc = DaemonRPCServer(Path("unused-parity-test.sock"))
    register_cli_handlers(rpc, MagicMock())
    return set(rpc._methods)  # noqa: SLF001


class TestRpcMethodParity:
    """Every CLI call literal must name a producible RPC method."""

    def test_scanner_sees_known_call_sites(self) -> None:
        """Guard against a silently broken scanner passing vacuously."""
        names = {name for name, _, _ in _cli_call_literals()}
        assert {"doctor", "status", "shutdown", "chat", "mind.list"} <= names

    def test_runtime_registration_is_ast_visible(self) -> None:
        """Every runtime-registered name is discoverable by the AST scan
        (no dynamic method names the literal check could miss)."""
        assert _runtime_registered_methods() <= _ast_registered_methods()

    def test_doctor_rpc_registered_at_runtime(self) -> None:
        """DOCTOR-1 closure anchor: the daemon serves ``doctor``."""
        assert "doctor" in _runtime_registered_methods()

    def test_every_cli_call_literal_is_registered(self) -> None:
        registered = _ast_registered_methods() | _runtime_registered_methods()
        offenders = [
            f"{name!r} at {rel}:{lineno}"
            for name, rel, lineno in _cli_call_literals()
            if name not in registered and name not in _KNOWN_UNREGISTERED
        ]
        assert not offenders, (
            "CLI calls RPC methods no daemon registers (AP #53 / DOCTOR-1 "
            "class). Register a handler in engine/_rpc_handlers.py — do "
            f"NOT extend the allowlist: {offenders}"
        )

    def test_known_unregistered_allowlist_not_stale(self) -> None:
        """An allowlisted method that gained a handler must leave the list."""
        registered = _ast_registered_methods() | _runtime_registered_methods()
        stale = _KNOWN_UNREGISTERED & registered
        assert not stale, f"Now registered — remove from _KNOWN_UNREGISTERED: {sorted(stale)}"

    def test_allowlisted_methods_still_called(self) -> None:
        """An allowlist entry whose call site disappeared is dead weight."""
        called = {name for name, _, _ in _cli_call_literals()}
        orphaned = _KNOWN_UNREGISTERED - called
        assert not orphaned, (
            f"No CLI call site remains — remove from _KNOWN_UNREGISTERED: {sorted(orphaned)}"
        )
