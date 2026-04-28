"""Tests for ``tools/lint_audio_callback.py`` (T1.29).

Two layers of coverage:

1. **Production source** — the canonical
   ``src/sovyx/voice/_capture_task.py::_audio_callback`` MUST lint
   clean on every test run. A regression that adds a blocking call
   to the audio callback fails the gate at ``pytest`` time.
2. **Synthetic source** — each forbidden pattern (``await``,
   ``time.sleep``, ``open``, ``socket.socket``, ``subprocess.run``,
   ``requests.get``, attribute ``acquire`` heuristic) is rejected by
   the linter when planted into a synthetic ``_audio_callback`` body.
   Pins the denylist contract — adding/removing entries from
   ``FORBIDDEN_CALLS`` requires updating these tests.

The linter is also invocable standalone via
``python tools/lint_audio_callback.py``; that path is not exercised
here but the CLI's exit-code contract (0 = clean, 1 = violations) is
documented in the module docstring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The linter lives outside ``src/`` (it's a tooling helper, not a
# runtime import). Add ``tools/`` to sys.path for the import.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLS = _REPO_ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from lint_audio_callback import (  # noqa: E402 — sys.path setup above must precede import
    FORBIDDEN_ATTRIBUTE_NAMES,
    FORBIDDEN_CALLS,
    Violation,
    lint_audio_callback,
    lint_source,
)


class TestProductionSourceClean:
    """The shipped ``_audio_callback`` MUST lint clean."""

    def test_capture_task_audio_callback_no_violations(self) -> None:
        target = _REPO_ROOT / "src" / "sovyx" / "voice" / "_capture_task.py"
        assert target.is_file()
        violations = lint_audio_callback(target)
        assert violations == [], (
            "_audio_callback contains a blocking pattern flagged by the "
            "T1.29 linter. Each violation indicates the audio thread can "
            "stall on this call:\n" + "\n".join(v.format(target) for v in violations)
        )


class TestSyntheticForbiddenPatterns:
    """Each forbidden pattern is rejected when planted in a synthetic body."""

    def _wrap_in_audio_callback(self, *body_lines: str) -> str:
        """Wrap the given statement(s) in a minimal ``_audio_callback``.

        Each positional argument is one line of the method body —
        the wrapper handles indentation. Pass each statement
        separately to avoid the leading-whitespace pitfalls of
        triple-quoted multi-line strings.
        """
        indented = "\n".join(f"        {line}" for line in body_lines)
        return (
            "class _Stub:\n"
            "    def _audio_callback(self, indata, frames, time_info, status) -> None:\n"
            f"{indented}\n"
        )

    def test_time_sleep_rejected(self) -> None:
        source = self._wrap_in_audio_callback("import time", "time.sleep(0.1)")
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "AUDIO_CB_FORBIDDEN_CALL"
        assert "time.sleep" in violations[0].message

    def test_builtin_open_rejected(self) -> None:
        source = self._wrap_in_audio_callback('open("/tmp/file", "w")')
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "AUDIO_CB_FORBIDDEN_CALL"
        assert "open" in violations[0].message

    def test_subprocess_run_rejected(self) -> None:
        source = self._wrap_in_audio_callback("import subprocess", "subprocess.run(['ls'])")
        violations = lint_source(source)
        # ``subprocess.run`` matches; the import statement itself is not a
        # Call so produces no violation.
        assert any(
            v.code == "AUDIO_CB_FORBIDDEN_CALL" and "subprocess.run" in v.message
            for v in violations
        )

    def test_socket_socket_rejected(self) -> None:
        source = self._wrap_in_audio_callback("import socket", "socket.socket()")
        violations = lint_source(source)
        assert any(
            v.code == "AUDIO_CB_FORBIDDEN_CALL" and "socket.socket" in v.message
            for v in violations
        )

    def test_requests_get_rejected(self) -> None:
        source = self._wrap_in_audio_callback(
            "import requests",
            "requests.get('http://example.com')",
        )
        violations = lint_source(source)
        assert any(
            v.code == "AUDIO_CB_FORBIDDEN_CALL" and "requests.get" in v.message for v in violations
        )

    def test_lock_acquire_rejected_via_attribute_heuristic(self) -> None:
        source = self._wrap_in_audio_callback("self._lock.acquire()")
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "AUDIO_CB_SUSPICIOUS_ATTR"
        assert "acquire" in violations[0].message

    def test_await_rejected(self) -> None:
        # An ``await`` in a sync function is a syntax error, but
        # ``_audio_callback`` could be mistakenly turned into ``async def``
        # by a refactor — the linter must still catch the await keyword
        # in that case.
        source = (
            "import asyncio\n"
            "class _Stub:\n"
            "    async def _audio_callback(self, indata, frames, time_info, status) -> None:\n"
            "        await asyncio.sleep(0)\n"
        )
        violations = lint_source(source)
        # Two violations: the ``await`` itself + the ``asyncio.sleep`` call.
        assert any(v.code == "AUDIO_CB_AWAIT" for v in violations)
        assert any(
            v.code == "AUDIO_CB_FORBIDDEN_CALL" and "asyncio.sleep" in v.message
            for v in violations
        )


class TestAllowedPatterns:
    """The current production callback's idioms MUST pass."""

    def _wrap_in_audio_callback(self, *body_lines: str) -> str:
        indented = "\n".join(f"        {line}" for line in body_lines)
        return (
            "class _Stub:\n"
            "    def _audio_callback(self, indata, frames, time_info, status) -> None:\n"
            f"{indented}\n"
        )

    def test_logger_debug_allowed(self) -> None:
        source = self._wrap_in_audio_callback('logger.debug("event")')
        assert lint_source(source) == []

    def test_call_soon_threadsafe_allowed(self) -> None:
        source = self._wrap_in_audio_callback("loop.call_soon_threadsafe(handler, arg)")
        assert lint_source(source) == []

    def test_numpy_copy_allowed(self) -> None:
        source = self._wrap_in_audio_callback("block = indata.copy()")
        assert lint_source(source) == []

    def test_getattr_allowed(self) -> None:
        source = self._wrap_in_audio_callback(
            'overflow = getattr(status, "input_overflow", False)'
        )
        assert lint_source(source) == []

    def test_attribute_increment_allowed(self) -> None:
        source = self._wrap_in_audio_callback("self._counter += 1")
        assert lint_source(source) == []


class TestLinterMechanics:
    """Linter machinery — line numbers, multiple violations, no _audio_callback."""

    def test_line_numbers_reported(self) -> None:
        source = (
            "import time\n"
            "class _Stub:\n"
            "    def _audio_callback(self, indata, frames, time_info, status) -> None:\n"
            "        x = 1\n"
            "        time.sleep(0.1)\n"
        )
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].lineno == 5  # noqa: PLR2004

    def test_multiple_violations_collected(self) -> None:
        source = (
            "import time\n"
            "import socket\n"
            "class _Stub:\n"
            "    def _audio_callback(self, indata, frames, time_info, status) -> None:\n"
            "        time.sleep(0.1)\n"
            "        socket.socket()\n"
        )
        violations = lint_source(source)
        assert len(violations) == 2  # noqa: PLR2004

    def test_no_audio_callback_method_returns_empty(self) -> None:
        """Source with no ``_audio_callback`` method produces no violations."""
        source = (
            "def some_unrelated_function() -> None:\n"
            "    import time\n"
            "    time.sleep(0.1)\n"  # would be flagged if it were inside _audio_callback
        )
        assert lint_source(source) == []

    def test_violation_format_includes_path_and_line(self) -> None:
        v = Violation(lineno=42, code="AUDIO_CB_AWAIT", message="test")
        formatted = v.format(Path("/some/path/file.py"))
        assert "42" in formatted
        assert "AUDIO_CB_AWAIT" in formatted
        assert "test" in formatted

    def test_forbidden_call_set_documented(self) -> None:
        """The denylist must contain at least the canonical entries
        documented in the module docstring (time.sleep, open,
        subprocess.*, socket.*, requests.*, asyncio.run/sleep).
        """
        canonical = {
            "time.sleep",
            "open",
            "subprocess.run",
            "subprocess.Popen",
            "socket.socket",
            "requests.get",
            "asyncio.run",
            "asyncio.sleep",
        }
        assert canonical <= FORBIDDEN_CALLS

    def test_acquire_in_forbidden_attributes(self) -> None:
        assert "acquire" in FORBIDDEN_ATTRIBUTE_NAMES


@pytest.mark.parametrize(
    "forbidden_name",
    sorted(FORBIDDEN_CALLS),
)
def test_every_forbidden_call_is_rejected(forbidden_name: str) -> None:
    """Parametrised — every entry in ``FORBIDDEN_CALLS`` MUST be
    rejected when planted into a synthetic ``_audio_callback`` body.
    Adding to the denylist without a corresponding rejection test
    is a quiet contract violation.
    """
    if forbidden_name in {"open", "input"}:
        body_lines = [f'{forbidden_name}("arg")']
    else:
        module_root = forbidden_name.split(".")[0]
        body_lines = [
            f"import {module_root}",
            f"{forbidden_name}(*args, **kwargs)",
        ]
    indented = "\n".join(f"        {line}" for line in body_lines)
    source = (
        "class _Stub:\n"
        "    def _audio_callback(self, indata, frames, time_info, status) -> None:\n"
        f"{indented}\n"
    )
    violations = lint_source(source)
    assert any(
        v.code == "AUDIO_CB_FORBIDDEN_CALL" and forbidden_name in v.message for v in violations
    ), f"{forbidden_name} should be rejected but was not. Got violations: {violations}"
