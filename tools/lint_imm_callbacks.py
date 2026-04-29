"""AST-level lint for the IMMNotificationClient COM callback contract.

The Windows ``IMMNotificationClient`` COM interface fires its
``On*`` event handlers on the MMDevice notifier thread inside the
audio service. Per CLAUDE.md anti-pattern #29 the callback body
**MUST** be non-blocking — anything that blocks the COM thread
(file I/O, sleeps, locks, subprocess, network, ``await``, nested
event loops) deadlocks the entire Windows audio service. Recovery
on a deadlocked audio service requires a service restart or
reboot; from the user's perspective the daemon "kills sound".

The five COM callback method names the linter watches for:

* ``OnDefaultDeviceChanged`` — default capture/render endpoint flipped
* ``OnDeviceAdded`` — new endpoint enumerated
* ``OnDeviceRemoved`` — endpoint disappeared
* ``OnDeviceStateChanged`` — endpoint became active / disabled / unplugged
* ``OnPropertyValueChanged`` — endpoint property mutation (Voice Clarity
  toggle, format change, etc.)

Plus ``register`` / ``unregister`` because those touch the COM
lifecycle and are equally subject to "no blocking on the audio
service's COM thread" — ``UnregisterEndpointNotificationCallback``
that races a wedged callback can hang shutdown.

The linter walks the AST of the canonical target
``src/sovyx/voice/_mm_notification_client.py`` and rejects any
known-blocking call inside one of those methods on a class whose
name matches the ``MMNotification`` / ``IMM`` heuristic. The
denylist is conservative — calls that are not on the list are
allowed by default (so ``self._loop.call_soon_threadsafe(...)``,
``logger.debug``, ``getattr``, primitive ops, ``return 0``, etc.
all pass). A future pattern that genuinely needs to land inside
the callback can be added to the denylist's allowance once it has
been reviewed for non-blocking semantics.

CI integration: ``tests/unit/voice/test_lint_imm_callbacks.py``
invokes :func:`lint_imm_callbacks` against the production source
on every pytest run. A violation fails the test, which fails the
gate.

Standalone usage::

    python tools/lint_imm_callbacks.py

Exit code 0 = clean, 1 = violations (printed to stderr).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

__all__ = [
    "FORBIDDEN_ATTRIBUTE_NAMES",
    "FORBIDDEN_CALLS",
    "GUARDED_CLASS_NAME_HINTS",
    "GUARDED_METHOD_NAMES",
    "Violation",
    "lint_imm_callbacks",
    "lint_source",
]


GUARDED_METHOD_NAMES: frozenset[str] = frozenset(
    {
        # IMMNotificationClient COM-callback surface.
        "OnDefaultDeviceChanged",
        "OnDeviceAdded",
        "OnDeviceRemoved",
        "OnDeviceStateChanged",
        "OnPropertyValueChanged",
        # Lifecycle methods — mutate the COM subscription. A blocking
        # call here on shutdown can hang the daemon (e.g. trying to
        # unregister while the notifier thread is wedged).
        "register",
        "unregister",
    }
)
"""Method names whose body the linter walks for blocking calls.

Five canonical IMMNotificationClient callbacks plus the two
lifecycle methods. The lifecycle methods are guarded too because
``UnregisterEndpointNotificationCallback`` racing a wedged
callback is a documented audio-service hang vector — see
spec §D5 + paranoid mission Part 5 critical risk #4."""


GUARDED_CLASS_NAME_HINTS: tuple[str, ...] = (
    "MMNotification",
    "IMMNotification",
)
"""Substrings that mark a class as IMM-callback-bearing.

Match is case-sensitive substring on ``class`` name. Covers
``WindowsMMNotificationListener`` (the v0.24.0 placeholder),
``MMNotificationListener`` (the Protocol), any future
``IMMNotificationClient`` concrete subclass, and noop fallbacks
that share the contract. The guard is on the class name to
avoid false-positives — a method named ``register`` on a class
unrelated to MMNotification (e.g. plugin registry) is not the
same risk surface."""


# Reuse the audio-callback denylist verbatim — every call that's
# blocking on the PortAudio audio thread is equally blocking on
# the MMDevice COM notifier thread. Plus a few additions specific
# to the COM context (RPC calls, OLE message pumps, etc. that
# don't appear in audio-callback contexts).
FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        # Builtin blocking I/O
        "open",
        "input",
        # Explicit sleep
        "time.sleep",
        "asyncio.sleep",
        # Subprocess invocation — fork/exec is fundamentally blocking
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        # Network
        "socket.socket",
        "socket.create_connection",
        "urllib.request.urlopen",
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.delete",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.delete",
        # Nested event loop — undefined behaviour from a non-async
        # thread, deadlock-prone on COM threads even more than on
        # PortAudio threads (the COM thread owns the apartment's
        # message pump; pumping a nested loop on top corrupts the
        # apartment state).
        "asyncio.run",
        "asyncio.run_until_complete",
        "asyncio.get_event_loop",
        "asyncio.new_event_loop",
        # Direct synchronous asyncio operations from the COM thread
        # are wrong by construction — the only safe asyncio surface
        # from a non-loop thread is loop.call_soon_threadsafe.
        "asyncio.run_coroutine_threadsafe",
        # COM-specific: blocking RPC + OLE message pump.
        "comtypes.client.PumpEvents",
        "pythoncom.PumpMessages",
        "pythoncom.PumpWaitingMessages",
        # Threading primitives that block by definition.
        "threading.Event.wait",
        "threading.Lock.acquire",
        "threading.Condition.wait",
        # Queue.get without a timeout blocks indefinitely.
        "queue.Queue.get",
        "queue.Queue.put",
    }
)
"""Dotted names whose Call inside a guarded method is a violation.

Conservative subset of known-blocking operations from the Python
stdlib + COM ecosystem. Calls not on the list are allowed by
default — ``self._loop.call_soon_threadsafe(...)``,
``logger.debug``, ``getattr``, ``return 0``, primitive ops, etc.
all pass.

To add an entry: confirm the call is genuinely blocking on the
COM-thread context, add the dotted name, and update the linter
test to assert the new entry rejects appropriately."""


FORBIDDEN_ATTRIBUTE_NAMES: frozenset[str] = frozenset(
    {
        # threading.Lock.acquire / asyncio.Lock.acquire / any *.acquire
        # pattern. Last-attribute heuristic — catches
        # ``self._lock.acquire(...)`` patterns even when the lock
        # object's type isn't statically resolvable.
        "acquire",
        # Queue.get without timeout — same shape as audio_callback's
        # heuristic but COM-thread-relevant. ``put`` would also
        # block on a bounded queue at capacity; ``get`` fires more
        # frequently as a real-world deadlock vector.
        # NOTE: not adding ``get`` here because it's far too generic
        # (dict.get / requests Session.get / etc. would false-fire);
        # the dotted form ``queue.Queue.get`` in FORBIDDEN_CALLS
        # covers the intended case.
        # ``join`` on a Thread blocks — common COM-callback bug is
        # spawning a worker and waiting for it to finish inside the
        # callback.
        "join",
        # ``wait`` on Event / Condition / Future blocks.
        "wait",
    }
)
"""Last-segment attribute names whose Call is suspicious enough to
flag without resolving the full module path. The signal-to-noise
ratio for ``acquire`` / ``join`` / ``wait`` is high enough that
the false-positive cost is acceptable; if a legitimate non-
blocking call site fires on this heuristic, refactor the call or
rephrase so the heuristic doesn't fire."""


class Violation:
    """A single lint violation with line number, code, and message."""

    __slots__ = ("code", "lineno", "message")

    def __init__(self, lineno: int, code: str, message: str) -> None:
        self.lineno = lineno
        self.code = code
        self.message = message

    def __repr__(self) -> str:
        return f"Violation(line={self.lineno}, code={self.code!r}, message={self.message!r})"

    def format(self, source_path: Path | str) -> str:
        return f"{source_path}:{self.lineno}: [{self.code}] {self.message}"


def _resolve_call_name(node: ast.Call) -> str:
    """Best-effort dotted name for a Call's callable.

    Mirrors :func:`tools.lint_audio_callback._resolve_call_name`
    — same algorithm, copied here so the IMM linter has zero
    runtime dependency on the audio-callback linter (CI gates
    them independently).
    """
    func = node.func
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    else:
        return ""
    parts.reverse()
    return ".".join(parts)


class _ImmCallbackVisitor(ast.NodeVisitor):
    """Walks a guarded callback method body, collecting violations."""

    def __init__(self) -> None:
        self.violations: list[Violation] = []

    def visit_Await(self, node: ast.Await) -> None:  # noqa: N802 (visit_<Type> name required by ast.NodeVisitor)
        self.violations.append(
            Violation(
                lineno=node.lineno,
                code="IMM_CB_AWAIT",
                message=(
                    "`await` keyword in IMMNotificationClient callback. "
                    "The callback fires on the MMDevice notifier thread, "
                    "NOT an asyncio loop — `await` is either a syntax "
                    "error (sync method, which is mandatory for the COM "
                    "interface) or a deadlock vector. Hand work off via "
                    "`self._loop.call_soon_threadsafe(dispatcher, *args)` "
                    "and `return 0` (S_OK) immediately."
                ),
            )
        )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _resolve_call_name(node)
        if name in FORBIDDEN_CALLS:
            self.violations.append(
                Violation(
                    lineno=node.lineno,
                    code="IMM_CB_FORBIDDEN_CALL",
                    message=(
                        f"forbidden call `{name}(...)` in "
                        "IMMNotificationClient callback. The callback "
                        "fires on the MMDevice COM notifier thread; this "
                        "call blocks/sleeps/forks/networks and would "
                        "deadlock the Windows audio service. Recovery "
                        "from a deadlocked audio service requires a "
                        "service restart or reboot."
                    ),
                )
            )
        elif isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_ATTRIBUTE_NAMES:
            self.violations.append(
                Violation(
                    lineno=node.lineno,
                    code="IMM_CB_SUSPICIOUS_ATTR",
                    message=(
                        f"suspicious attribute call `{node.func.attr}(...)` "
                        "in IMMNotificationClient callback. Most "
                        "`acquire`/`join`/`wait`-named methods are blocking "
                        "synchronisation primitives; if this is genuinely "
                        "non-blocking, refactor the call site or rename so "
                        "the heuristic doesn't fire."
                    ),
                )
            )
        self.generic_visit(node)


def _is_guarded_class(node: ast.ClassDef) -> bool:
    """``True`` iff ``node.name`` matches the IMM-class heuristic."""
    return any(hint in node.name for hint in GUARDED_CLASS_NAME_HINTS)


def lint_source(source: str, filename: str = "<source>") -> list[Violation]:
    """Lint a Python source string for IMM-callback violations.

    Walks the AST, finds every ``ClassDef`` whose name matches
    :data:`GUARDED_CLASS_NAME_HINTS`, then for each method whose
    name is in :data:`GUARDED_METHOD_NAMES` runs the visitor
    against the body. Returns a flat list of violations across
    every matching class × method pair. Both ``def`` and
    ``async def`` shapes are walked: the canonical COM callback is
    sync (the COM interface is sync by definition), but the
    ``visit_Await`` handler exists to flag a future refactor that
    mistakenly flips the method to ``async def``.

    Args:
        source: Python source text.
        filename: Filename for error messages and AST parsing.

    Returns:
        List of :class:`Violation` instances. Empty if clean.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _is_guarded_class(node):
            continue
        for member in node.body:
            if (
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name in GUARDED_METHOD_NAMES
            ):
                visitor = _ImmCallbackVisitor()
                for stmt in member.body:
                    visitor.visit(stmt)
                violations.extend(visitor.violations)
    return violations


def lint_imm_callbacks(source_path: Path) -> list[Violation]:
    """Lint a Python source file for IMM-callback violations.

    Args:
        source_path: Path to the ``.py`` file to lint.

    Returns:
        List of :class:`Violation` instances. Empty if clean.
    """
    source = source_path.read_text(encoding="utf-8")
    return lint_source(source, filename=str(source_path))


def _default_target() -> Path:
    """Path to the canonical lint target — ``voice/_mm_notification_client.py``."""
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "src" / "sovyx" / "voice" / "_mm_notification_client.py"


def main() -> int:
    """CLI entry point: lint the canonical target, print violations.

    Returns:
        ``0`` if clean, ``1`` if any violations found.
    """
    target = _default_target()
    if not target.is_file():
        print(f"lint_imm_callbacks: target not found: {target}", file=sys.stderr)
        return 1
    violations = lint_imm_callbacks(target)
    if not violations:
        return 0
    for v in violations:
        print(v.format(target), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
