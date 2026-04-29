"""Tests for ``tools/lint_imm_callbacks.py``.

Two layers of coverage mirroring ``test_lint_audio_callback.py``:

1. **Production source** — the canonical
   ``src/sovyx/voice/_mm_notification_client.py`` MUST lint clean
   on every test run. A regression that adds a blocking call to
   any IMMNotificationClient COM callback method fails the gate
   at ``pytest`` time, BEFORE the deadlocked-Windows-audio-service
   bug ever reaches an operator's machine.
2. **Synthetic source** — each forbidden pattern is rejected by
   the linter when planted into a synthetic guarded class +
   guarded method. Pins the denylist contract — adding/removing
   entries from ``FORBIDDEN_CALLS`` requires updating these tests.

The linter targets methods whose name is in
:data:`GUARDED_METHOD_NAMES` (the five IMMNotificationClient COM
callbacks plus ``register`` / ``unregister`` lifecycle methods)
on classes whose name matches the
:data:`GUARDED_CLASS_NAME_HINTS` heuristic
(``"MMNotification"`` / ``"IMMNotification"`` substring).
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

from lint_imm_callbacks import (  # noqa: E402 — sys.path setup above must precede import
    FORBIDDEN_ATTRIBUTE_NAMES,
    FORBIDDEN_CALLS,
    GUARDED_CLASS_NAME_HINTS,
    GUARDED_METHOD_NAMES,
    Violation,
    lint_imm_callbacks,
    lint_source,
)


class TestProductionSourceClean:
    """The shipped IMM callback module MUST lint clean."""

    def test_mm_notification_client_module_no_violations(self) -> None:
        target = _REPO_ROOT / "src" / "sovyx" / "voice" / "_mm_notification_client.py"
        assert target.is_file()
        violations = lint_imm_callbacks(target)
        assert violations == [], (
            "voice/_mm_notification_client.py contains a blocking pattern "
            "flagged by the IMM-callback linter. Each violation indicates "
            "the MMDevice notifier thread can deadlock the Windows audio "
            "service on this call:\n" + "\n".join(v.format(target) for v in violations)
        )


class TestSyntheticForbiddenPatterns:
    """Each forbidden pattern is rejected when planted in a synthetic body."""

    def _wrap_in_imm_callback(
        self,
        *body_lines: str,
        method_name: str = "OnDefaultDeviceChanged",
        class_name: str = "WindowsMMNotificationListener",
    ) -> str:
        """Wrap statement(s) in a minimal guarded class + method.

        ``method_name`` defaults to one of the canonical COM
        callbacks; tests can override it to exercise other guarded
        names (``OnDeviceStateChanged``, ``register``, etc.).
        ``class_name`` defaults to one matching the
        ``MMNotification`` heuristic; the unguarded-class test
        below uses a different class name to assert the heuristic
        actually scopes the guard.
        """
        indented = "\n".join(f"        {line}" for line in body_lines)
        return f"class {class_name}:\n    def {method_name}(self, *args) -> int:\n{indented}\n"

    def test_time_sleep_rejected(self) -> None:
        source = self._wrap_in_imm_callback("import time", "time.sleep(0.1)")
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "IMM_CB_FORBIDDEN_CALL"
        assert "time.sleep" in violations[0].message

    def test_builtin_open_rejected(self) -> None:
        source = self._wrap_in_imm_callback('open("/tmp/file", "w")')
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "IMM_CB_FORBIDDEN_CALL"
        assert "open" in violations[0].message

    def test_subprocess_run_rejected(self) -> None:
        source = self._wrap_in_imm_callback(
            "import subprocess",
            "subprocess.run(['ls'])",
        )
        violations = lint_source(source)
        assert any(
            v.code == "IMM_CB_FORBIDDEN_CALL" and "subprocess.run" in v.message for v in violations
        )

    def test_socket_socket_rejected(self) -> None:
        source = self._wrap_in_imm_callback(
            "import socket",
            "socket.socket()",
        )
        violations = lint_source(source)
        assert any(
            v.code == "IMM_CB_FORBIDDEN_CALL" and "socket.socket" in v.message for v in violations
        )

    def test_requests_get_rejected(self) -> None:
        source = self._wrap_in_imm_callback(
            "import requests",
            "requests.get('http://x.example')",
        )
        violations = lint_source(source)
        assert any(
            v.code == "IMM_CB_FORBIDDEN_CALL" and "requests.get" in v.message for v in violations
        )

    def test_asyncio_run_rejected(self) -> None:
        """Nested event loops on a COM apartment thread corrupt the
        message-pump state — even more dangerous than on PortAudio."""
        source = self._wrap_in_imm_callback(
            "import asyncio",
            "asyncio.run(self._coro())",
        )
        violations = lint_source(source)
        assert any(
            v.code == "IMM_CB_FORBIDDEN_CALL" and "asyncio.run" in v.message for v in violations
        )

    def test_pythoncom_pump_messages_rejected(self) -> None:
        """COM-specific risk — pumping messages from inside a callback
        re-enters the apartment and deadlocks the audio service."""
        source = self._wrap_in_imm_callback(
            "import pythoncom",
            "pythoncom.PumpMessages()",
        )
        violations = lint_source(source)
        assert any(
            v.code == "IMM_CB_FORBIDDEN_CALL" and "pythoncom.PumpMessages" in v.message
            for v in violations
        )

    def test_lock_acquire_rejected_via_attribute_heuristic(self) -> None:
        source = self._wrap_in_imm_callback("self._lock.acquire()")
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "IMM_CB_SUSPICIOUS_ATTR"
        assert "acquire" in violations[0].message

    def test_thread_join_rejected_via_attribute_heuristic(self) -> None:
        """``join`` on a Thread blocks the COM thread until the worker
        finishes — common pitfall when spawning a worker from a
        callback."""
        source = self._wrap_in_imm_callback("worker.join()")
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "IMM_CB_SUSPICIOUS_ATTR"
        assert "join" in violations[0].message

    def test_event_wait_rejected_via_attribute_heuristic(self) -> None:
        """``wait`` on Event/Condition blocks the COM thread."""
        source = self._wrap_in_imm_callback("event.wait()")
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "IMM_CB_SUSPICIOUS_ATTR"
        assert "wait" in violations[0].message

    def test_await_rejected(self) -> None:
        """The COM callback is sync by definition — ``await`` is either
        a syntax error or a deadlock vector via a misplaced
        ``async def``."""
        source = (
            "class WindowsMMNotificationListener:\n"
            "    async def OnDefaultDeviceChanged(self, *args) -> int:\n"
            "        await self._coro()\n"
        )
        violations = lint_source(source)
        assert any(v.code == "IMM_CB_AWAIT" for v in violations)


class TestGuardScope:
    """The linter only walks classes matching the IMM heuristic +
    methods matching the guarded-name set. False-positives outside
    that scope would block legitimate code."""

    def test_unguarded_class_name_skipped(self) -> None:
        """A method named ``register`` on a class unrelated to
        IMMNotification is NOT walked — the lint is scoped to COM-
        callback contexts."""
        source = (
            "class PluginRegistry:\n"
            "    def register(self) -> None:\n"
            "        import time\n"
            "        time.sleep(0.1)\n"
        )
        violations = lint_source(source)
        assert violations == []

    def test_unguarded_method_name_skipped(self) -> None:
        """A method on a guarded class but with a non-callback name
        (``__init__``, ``_helper``) is NOT walked. Blocking calls in
        ``__init__`` can be acceptable (the listener is constructed
        on the asyncio loop thread, not the COM thread)."""
        source = (
            "class WindowsMMNotificationListener:\n"
            "    def __init__(self) -> None:\n"
            "        import time\n"
            "        time.sleep(0.1)\n"
        )
        violations = lint_source(source)
        assert violations == []

    def test_register_lifecycle_is_guarded(self) -> None:
        """``register`` IS in the guarded set — UnregisterEndpoint…
        racing a wedged callback is a documented deadlock vector."""
        source = (
            "class WindowsMMNotificationListener:\n"
            "    def register(self) -> None:\n"
            "        import time\n"
            "        time.sleep(0.1)\n"
        )
        violations = lint_source(source)
        assert len(violations) == 1
        assert "time.sleep" in violations[0].message

    def test_unregister_lifecycle_is_guarded(self) -> None:
        source = (
            "class WindowsMMNotificationListener:\n"
            "    def unregister(self) -> None:\n"
            "        self._wedged_lock.acquire()\n"
        )
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].code == "IMM_CB_SUSPICIOUS_ATTR"

    def test_all_five_com_callbacks_guarded(self) -> None:
        """The five canonical IMMNotificationClient callbacks are all
        in the guarded set — pin the contract so a future addition or
        removal updates GUARDED_METHOD_NAMES deliberately."""
        canonical = {
            "OnDefaultDeviceChanged",
            "OnDeviceAdded",
            "OnDeviceRemoved",
            "OnDeviceStateChanged",
            "OnPropertyValueChanged",
        }
        assert canonical <= GUARDED_METHOD_NAMES, (
            f"GUARDED_METHOD_NAMES is missing canonical IMM callbacks: "
            f"{canonical - GUARDED_METHOD_NAMES}"
        )

    def test_imm_class_hint_substring_match(self) -> None:
        """The class-name heuristic uses substring match so future
        concrete subclasses (`IMMNotificationClient`,
        `MMNotificationListener_v2`, etc.) inherit the guard
        automatically."""
        for hint in GUARDED_CLASS_NAME_HINTS:
            assert (
                hint
                in (
                    "WindowsMMNotificationListenerExtra"  # synthetic test name
                    if "MMNotification" in hint
                    else "FooIMMNotificationClient"
                )
                or "IMMNotification" in hint
            ), f"hint {hint!r} should match obvious subclass names"


class TestAllowedPatterns:
    """Sanity check — the linter does NOT fire on the actual COM-
    callback-safe patterns (``call_soon_threadsafe``, ``logger.*``,
    ``return 0``, primitive ops). False-positives here would block
    legitimate v0.25.0 wire-up."""

    def _wrap(self, *body_lines: str) -> str:
        indented = "\n".join(f"        {line}" for line in body_lines)
        return (
            "class WindowsMMNotificationListener:\n"
            "    def OnDefaultDeviceChanged(self, *args) -> int:\n"
            f"{indented}\n"
        )

    def test_call_soon_threadsafe_allowed(self) -> None:
        """The ONE asyncio-from-COM-thread surface that's safe."""
        source = self._wrap("self._loop.call_soon_threadsafe(self._dispatch, *args)")
        assert lint_source(source) == []

    def test_logger_debug_allowed(self) -> None:
        source = self._wrap('logger.debug("voice.imm.callback_fired", role=role)')
        assert lint_source(source) == []

    def test_return_zero_allowed(self) -> None:
        """``return 0`` is the canonical S_OK HRESULT — must not fire."""
        source = self._wrap("return 0")
        assert lint_source(source) == []

    def test_attribute_increment_allowed(self) -> None:
        source = self._wrap("self._callback_count += 1")
        assert lint_source(source) == []

    def test_getattr_allowed(self) -> None:
        source = self._wrap("device_id = getattr(args[0], 'device_id', '')")
        assert lint_source(source) == []

    def test_try_except_baseexception_allowed(self) -> None:
        """The mandatory try/except wrapper required by anti-pattern #29."""
        source = self._wrap(
            "try:",
            "    self._loop.call_soon_threadsafe(self._dispatch, *args)",
            "except BaseException:",
            "    pass",
        )
        assert lint_source(source) == []


class TestLinterMechanics:
    """Lint mechanics + violation-shape contract."""

    def test_line_numbers_reported(self) -> None:
        source = (
            "class WindowsMMNotificationListener:\n"
            "    def OnDeviceStateChanged(self, *args) -> int:\n"
            "        x = 1\n"
            "        import time\n"
            "        time.sleep(0.1)\n"
        )
        violations = lint_source(source)
        assert len(violations) == 1
        assert violations[0].lineno == 5  # noqa: PLR2004

    def test_multiple_violations_collected(self) -> None:
        source = (
            "class WindowsMMNotificationListener:\n"
            "    def OnDeviceAdded(self, device_id) -> int:\n"
            "        import time\n"
            "        time.sleep(0.1)\n"
            "        self._lock.acquire()\n"
        )
        violations = lint_source(source)
        assert len(violations) == 2  # noqa: PLR2004
        codes = {v.code for v in violations}
        assert codes == {"IMM_CB_FORBIDDEN_CALL", "IMM_CB_SUSPICIOUS_ATTR"}

    def test_violation_format_includes_path_and_line(self) -> None:
        v = Violation(lineno=42, code="IMM_CB_FORBIDDEN_CALL", message="boom")
        formatted = v.format("/path/to/file.py")
        assert "/path/to/file.py:42" in formatted
        assert "IMM_CB_FORBIDDEN_CALL" in formatted
        assert "boom" in formatted

    def test_no_imm_class_returns_empty(self) -> None:
        source = "class JustARegularClass:\n    def OnDefaultDeviceChanged(self):\n        import time\n        time.sleep(0.1)\n"
        assert lint_source(source) == []

    def test_forbidden_call_set_documented(self) -> None:
        """Pin a representative subset so a future PR that removes
        an entry hits this test before merging."""
        for required in (
            "time.sleep",
            "asyncio.sleep",
            "asyncio.run",
            "subprocess.run",
            "open",
            "pythoncom.PumpMessages",
        ):
            assert required in FORBIDDEN_CALLS, f"{required!r} dropped from FORBIDDEN_CALLS"

    def test_acquire_in_forbidden_attributes(self) -> None:
        assert "acquire" in FORBIDDEN_ATTRIBUTE_NAMES
        assert "join" in FORBIDDEN_ATTRIBUTE_NAMES
        assert "wait" in FORBIDDEN_ATTRIBUTE_NAMES


@pytest.mark.parametrize("forbidden_name", sorted(FORBIDDEN_CALLS))
def test_every_forbidden_call_is_rejected(forbidden_name: str) -> None:
    """Parametrised across the entire denylist — every entry MUST
    fire when planted into a synthetic guarded callback. Prevents
    the dead-entry case where a name in the set never matches the
    AST-resolved call name."""
    source = (
        "class WindowsMMNotificationListener:\n"
        "    def OnDefaultDeviceChanged(self, *args) -> int:\n"
        f"        {forbidden_name}()\n"
    )
    violations = lint_source(source)
    assert any(
        v.code == "IMM_CB_FORBIDDEN_CALL" and forbidden_name in v.message for v in violations
    ), f"forbidden call {forbidden_name!r} was NOT flagged by the linter"
