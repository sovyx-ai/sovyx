"""Tests for :mod:`sovyx.voice.health._mic_permission` (band-aid #34).

Mocks ``winreg`` rather than touching the real registry so the test
suite stays cross-platform and deterministic. Validates:

* Linux returns GRANTED unconditionally (no OS-level capture-consent).
* macOS returns UNKNOWN (TCC probe deferred to MA2).
* Windows GRANTED when both HKLM + HKCU = "Allow".
* Windows DENIED when HKLM = "Deny" (machine policy wins).
* Windows DENIED when HKCU = "Deny" (user opt-out).
* Windows UNKNOWN when keys absent (Win10 < 1809 / Server SKUs).
* Registry OSError → UNKNOWN with structured note.
* Garbage Value strings collapse to safe UNKNOWN (never falsely DENY).
* remediation_hint matches the verdict.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._mic_permission import (
    MicPermissionReport,
    MicPermissionStatus,
    check_microphone_permission,
)

# ── winreg fake ───────────────────────────────────────────────────


def _fake_winreg(
    *,
    machine_value: str | None,
    user_value: str | None,
    machine_open_error: type[BaseException] | None = None,
    user_open_error: type[BaseException] | None = None,
) -> ModuleType:
    """Build a minimal stand-in for the ``winreg`` module.

    Recognises the two hive constants and returns the configured
    ``Value`` per scope. Raise hooks let tests simulate registry
    permission errors without OS dependence."""
    module = ModuleType("winreg")
    module.HKEY_LOCAL_MACHINE = 0x80000002  # type: ignore[attr-defined]
    module.HKEY_CURRENT_USER = 0x80000001  # type: ignore[attr-defined]

    machine_key = MagicMock(name="HKLM_key")
    user_key = MagicMock(name="HKCU_key")

    def open_key(hive: int, path: str) -> Any:
        del path  # Same path under both hives — discriminate by hive only.
        if hive == module.HKEY_LOCAL_MACHINE:  # type: ignore[attr-defined]
            if machine_open_error is not None:
                raise machine_open_error("simulated HKLM open failure")
            return machine_key
        if hive == module.HKEY_CURRENT_USER:  # type: ignore[attr-defined]
            if user_open_error is not None:
                raise user_open_error("simulated HKCU open failure")
            return user_key
        raise FileNotFoundError("unexpected hive")

    def query_value_ex(key: Any, name: str) -> tuple[Any, int]:
        assert name == "Value"
        if key is machine_key:
            if machine_value is None:
                raise FileNotFoundError("Value missing")
            return machine_value, 1  # REG_SZ
        if key is user_key:
            if user_value is None:
                raise FileNotFoundError("Value missing")
            return user_value, 1
        raise FileNotFoundError("unknown key")

    def close_key(_key: Any) -> None:
        return None

    module.OpenKey = open_key  # type: ignore[attr-defined]
    module.QueryValueEx = query_value_ex  # type: ignore[attr-defined]
    module.CloseKey = close_key  # type: ignore[attr-defined]
    return module


def _patch_win32_with_winreg(fake: ModuleType) -> Any:
    """Patch sys.platform → 'win32' AND inject fake winreg."""
    import sovyx.voice.health._mic_permission as mod

    return (
        patch.multiple(
            sys,
            platform="win32",
        ),
        patch.dict(sys.modules, {"winreg": fake}),
        mod,
    )


# ── Cross-platform branches ───────────────────────────────────────


class TestCrossPlatformBranches:
    def test_linux_returns_granted(self) -> None:
        with patch.object(sys, "platform", "linux"):
            report = check_microphone_permission()
        assert report.status is MicPermissionStatus.GRANTED
        assert report.remediation_hint == ""
        assert any("linux" in n for n in report.notes)

    def test_darwin_returns_unknown(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            report = check_microphone_permission()
        assert report.status is MicPermissionStatus.UNKNOWN
        assert any("darwin" in n.lower() for n in report.notes)

    def test_unsupported_platform_returns_unknown(self) -> None:
        with patch.object(sys, "platform", "freebsd"):
            report = check_microphone_permission()
        assert report.status is MicPermissionStatus.UNKNOWN


# ── Windows branches via fake winreg ───────────────────────────────


class TestWindowsConsentReader:
    def _run(self, fake: ModuleType) -> MicPermissionReport:
        # Patch sys.platform AND inject the fake winreg module.
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": fake}),
        ):
            return check_microphone_permission()

    def test_both_allow_returns_granted(self) -> None:
        fake = _fake_winreg(machine_value="Allow", user_value="Allow")
        report = self._run(fake)
        assert report.status is MicPermissionStatus.GRANTED
        assert report.machine_value == "Allow"
        assert report.user_value == "Allow"

    def test_machine_policy_deny_wins_over_user_allow(self) -> None:
        """HKLM Deny overrides HKCU Allow — matches Win10 group-policy
        semantics (machine policy beats user preference)."""
        fake = _fake_winreg(machine_value="Deny", user_value="Allow")
        report = self._run(fake)
        assert report.status is MicPermissionStatus.DENIED
        assert "blocking microphone access" in report.remediation_hint

    def test_user_deny_returns_denied(self) -> None:
        fake = _fake_winreg(machine_value="Allow", user_value="Deny")
        report = self._run(fake)
        assert report.status is MicPermissionStatus.DENIED

    def test_machine_absent_user_allow_returns_granted(self) -> None:
        """Win10 < 1809 sets only HKCU; HKLM is absent. Must not
        falsely UNKNOWN — user opt-in alone is sufficient."""
        fake = _fake_winreg(machine_value=None, user_value="Allow")
        report = self._run(fake)
        assert report.status is MicPermissionStatus.GRANTED
        assert any("machine policy absent" in n for n in report.notes)

    def test_both_absent_returns_unknown(self) -> None:
        fake = _fake_winreg(machine_value=None, user_value=None)
        report = self._run(fake)
        assert report.status is MicPermissionStatus.UNKNOWN
        assert "could not be determined" in report.remediation_hint

    def test_garbage_value_does_not_falsely_deny(self) -> None:
        """A corrupted / unexpected value must not be misread as Deny.
        Defense against malicious or malformed registry state."""
        fake = _fake_winreg(machine_value="¯\\_(ツ)_/¯", user_value="Allow")
        report = self._run(fake)
        # Garbage on machine + Allow on user → both Allow check
        # fails (machine isn't "Allow") → falls through to UNKNOWN
        # (machine value present but unexpected).
        assert report.status is MicPermissionStatus.UNKNOWN

    def test_case_insensitive_deny(self) -> None:
        """Some build localisations capitalise differently; we
        canonicalise via .lower() so DENY / deny / Deny all match."""
        for variant in ("Deny", "deny", "DENY", " Deny "):
            fake = _fake_winreg(machine_value=variant, user_value="Allow")
            report = self._run(fake)
            assert report.status is MicPermissionStatus.DENIED, (
                f"variant {variant!r} not recognised as deny"
            )

    def test_open_oserror_returns_unknown(self) -> None:
        fake = _fake_winreg(
            machine_value=None,
            user_value=None,
            machine_open_error=PermissionError,
            user_open_error=PermissionError,
        )
        report = self._run(fake)
        assert report.status is MicPermissionStatus.UNKNOWN
        assert any("open failed" in n for n in report.notes)


# ── Report contract ────────────────────────────────────────────────


class TestMicPermissionReport:
    def test_granted_remediation_is_empty(self) -> None:
        r = MicPermissionReport(status=MicPermissionStatus.GRANTED)
        assert r.remediation_hint == ""

    def test_denied_remediation_mentions_settings_path(self) -> None:
        r = MicPermissionReport(status=MicPermissionStatus.DENIED)
        # The remediation must include the EXACT settings path users
        # navigate; a vague "fix permissions" message defeats the
        # whole point of the loud-fail.
        assert "Privacy & security" in r.remediation_hint
        assert "Microphone" in r.remediation_hint

    def test_unknown_remediation_offers_manual_check(self) -> None:
        r = MicPermissionReport(status=MicPermissionStatus.UNKNOWN)
        assert "manually" in r.remediation_hint

    def test_status_enum_values_stable(self) -> None:
        # Dashboards key on these strings — renaming is a breaking
        # change for downstream consumers (Grafana, dashboard view).
        assert MicPermissionStatus.GRANTED.value == "granted"
        assert MicPermissionStatus.DENIED.value == "denied"
        assert MicPermissionStatus.UNKNOWN.value == "unknown"


pytestmark = pytest.mark.timeout(10)  # Pure registry-mock — fast.
