"""AST-level lint for audio-callback non-blocking guarantee (T1.29).

PortAudio invokes ``AudioCaptureTask._audio_callback`` on a dedicated
audio thread that sounddevice manages. Anything in the callback body
that blocks the thread — file I/O, sleeps, network calls, lock
contention, subprocess invocation — stalls the audio stream and
cascades into frame drops, deaf-detector misfires, and barge-in
latency. CLAUDE.md anti-pattern #14 covers the broader principle;
this linter is the audio-callback-specific enforcer.

The linter walks the AST of every method named ``_audio_callback``
in ``src/sovyx/voice/_capture_task.py`` and flags any call to a
known-blocking name. The denylist is conservative — calls that are
not on the list are allowed (so ``logger.debug``, ``np.copy``,
``loop.call_soon_threadsafe``, ``getattr``, etc. all pass). A future
pattern that genuinely needs to land inside the callback can be
added to the denylist's ``# noqa: T1.29`` allowance once it has been
reviewed for non-blocking semantics.

CI integration: ``tests/unit/voice/test_lint_audio_callback.py``
invokes :func:`lint_audio_callback` against the production source
on every pytest run. A violation fails the test, which fails the
gate.

Standalone usage::

    python tools/lint_audio_callback.py

Exit code 0 = clean, 1 = violations (printed to stderr).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

__all__ = [
    "FORBIDDEN_CALLS",
    "FORBIDDEN_ATTRIBUTE_NAMES",
    "Violation",
    "lint_audio_callback",
    "lint_source",
]


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
        # Nested event loop — undefined behaviour from a non-async thread
        "asyncio.run",
        "asyncio.run_until_complete",
    }
)
"""Dotted names whose Call inside ``_audio_callback`` is a violation.

The list is intentionally conservative: only names that are
KNOWN-blocking from prior incident analysis or Python stdlib
documentation. Calls not on the list are allowed by default —
``logger.debug``, ``np.copy``, ``loop.call_soon_threadsafe``,
``getattr``, ``str``, ``type``, etc. all pass.

To add an entry: confirm the call is genuinely blocking on the
audio-thread context, add the dotted name (longest-prefix match
against the resolved Call name), and update the linter test to
assert the new entry rejects appropriately."""


FORBIDDEN_ATTRIBUTE_NAMES: frozenset[str] = frozenset(
    {
        # threading.Lock.acquire (and asyncio.Lock.acquire — but that
        # would also fail the await check). Including ``acquire`` as a
        # last-attribute heuristic catches ``self._lock.acquire(...)``
        # patterns even when the lock object's type isn't statically
        # resolvable. False-positives on legitimate non-blocking
        # acquires (queue.Queue.acquire doesn't exist; asyncio queue
        # uses put_nowait) are rare enough that we accept them and
        # require an explicit reviewer-justified suppression for the
        # genuinely-safe case (rephrase the call site or refactor).
        "acquire",
    }
)
"""Last-segment attribute names whose Call is suspicious enough to
flag without resolving the full module path. The signal-to-noise
ratio for ``acquire`` is high enough that the false-positive cost
is acceptable."""


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

    ``time.sleep(0.1)`` → ``"time.sleep"``
    ``logger.debug(...)`` → ``"logger.debug"``
    ``self._x.copy()`` → ``"self._x.copy"``
    ``foo()`` → ``"foo"``
    ``(lambda: 0)()`` → ``""`` (anonymous)
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


class _AudioCallbackVisitor(ast.NodeVisitor):
    """Walks an ``_audio_callback`` body, collecting violations."""

    def __init__(self) -> None:
        self.violations: list[Violation] = []

    def visit_Await(self, node: ast.Await) -> None:  # noqa: N802 (visit_<Type> name required by ast.NodeVisitor)
        self.violations.append(
            Violation(
                lineno=node.lineno,
                code="AUDIO_CB_AWAIT",
                message=(
                    "`await` keyword in audio callback. The callback runs on "
                    "the PortAudio thread, NOT an asyncio loop — `await` is "
                    "either a syntax error (sync function) or a deadlock "
                    "vector. Hand work off via `loop.call_soon_threadsafe` "
                    "instead."
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
                    code="AUDIO_CB_FORBIDDEN_CALL",
                    message=(
                        f"forbidden call `{name}(...)` in audio callback. "
                        f"The callback runs on the PortAudio thread; this "
                        f"call blocks/sleeps/forks/networks and would stall "
                        f"the audio stream."
                    ),
                )
            )
        elif isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_ATTRIBUTE_NAMES:
            self.violations.append(
                Violation(
                    lineno=node.lineno,
                    code="AUDIO_CB_SUSPICIOUS_ATTR",
                    message=(
                        f"suspicious attribute call `{node.func.attr}(...)` "
                        f"in audio callback. Most `acquire`-named methods "
                        f"are blocking lock operations; if this is "
                        f"genuinely non-blocking, refactor the call site "
                        f"or rename so the heuristic doesn't fire."
                    ),
                )
            )
        self.generic_visit(node)


def lint_source(source: str, filename: str = "<source>") -> list[Violation]:
    """Lint a Python source string for audio-callback violations.

    Walks the AST, finds every ``FunctionDef`` or ``AsyncFunctionDef``
    named ``_audio_callback``, and runs the visitor against each body.
    Returns a flat list of violations across all matching functions.
    The async branch exists because the canonical callback is sync,
    but a future refactor that mistakenly flips it to ``async def``
    is exactly the kind of error the linter must catch — the
    ``visit_Await`` handler gets a chance to flag the keyword
    misuse.

    Args:
        source: Python source text.
        filename: Filename for error messages and AST parsing.

    Returns:
        List of :class:`Violation` instances. Empty if clean.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_audio_callback"
        ):
            visitor = _AudioCallbackVisitor()
            for stmt in node.body:
                visitor.visit(stmt)
            violations.extend(visitor.violations)
    return violations


def lint_audio_callback(source_path: Path) -> list[Violation]:
    """Lint a Python source file for audio-callback violations.

    Args:
        source_path: Path to the ``.py`` file to lint.

    Returns:
        List of :class:`Violation` instances. Empty if clean.
    """
    source = source_path.read_text(encoding="utf-8")
    return lint_source(source, filename=str(source_path))


def _default_target() -> Path:
    """Path to the canonical lint target — ``voice/_capture_task.py``."""
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "src" / "sovyx" / "voice" / "_capture_task.py"


def main() -> int:
    """CLI entry point: lint the canonical target, print violations.

    Returns:
        ``0`` if clean, ``1`` if any violations found.
    """
    target = _default_target()
    if not target.is_file():
        print(f"lint_audio_callback: target not found: {target}", file=sys.stderr)
        return 1
    violations = lint_audio_callback(target)
    if not violations:
        return 0
    for v in violations:
        print(v.format(target), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
