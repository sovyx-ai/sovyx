"""Tests for Windows GP detector [Phase 5 T5.46 + T5.47].

Coverage:

* Non-Windows host returns ``platform_supported=False`` snapshot.
* Missing policy root key returns ``platform_supported=True``
  with both raw values None (the common home-machine case).
* ``DisallowExclusiveDevice=1`` flips the boolean + structured
  WARN log fires.
* ``DisallowExclusiveDevice=0`` (explicitly permissive) leaves
  the boolean False.
* Permission-denied probe surfaces as ``probe_failure_reason=
  "permission_denied"``.
* OSError on key open surfaces as ``"registry_unavailable"``.
* :func:`log_group_policy_snapshot` emits INFO when no
  restrictions, WARN when ``exclusive_mode_disallowed=True``.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import MagicMock, patch

import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures

from sovyx.voice import _group_policy_detector as gpd

_GP_LOGGER = "sovyx.voice._group_policy_detector"


def _fake_winreg(
    *,
    open_raises: type[BaseException] | None = None,
    raw_values: dict[str, tuple[int, int] | type[BaseException]] | None = None,
) -> types.SimpleNamespace:
    """Construct a mock winreg module.

    ``raw_values`` maps value name → either a (value, value_type)
    tuple OR an exception class to raise. Anything not listed
    raises FileNotFoundError (mirrors real winreg behaviour for
    absent values).
    """
    raw_values = raw_values or {}

    REG_DWORD = 4

    def _query_value_ex(_key: object, name: str) -> tuple[int, int]:
        entry = raw_values.get(name)
        if entry is None:
            raise FileNotFoundError(name)
        if isinstance(entry, type) and issubclass(entry, BaseException):
            raise entry(name)
        return entry  # type: ignore[return-value]

    def _open_key(*_args: object, **_kwargs: object) -> object:
        if open_raises is not None:
            raise open_raises("simulated")
        return MagicMock()

    return types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=0x80000002,
        KEY_READ=0x20019,
        REG_DWORD=REG_DWORD,
        OpenKey=_open_key,
        QueryValueEx=_query_value_ex,
        CloseKey=lambda _key: None,
    )


class TestNonWindows:
    def test_linux_returns_unsupported_snapshot(self) -> None:
        with patch.object(sys, "platform", "linux"):
            snapshot = gpd.detect_group_policies()
        assert snapshot.platform_supported is False
        assert snapshot.exclusive_mode_disallowed is False
        assert snapshot.devices_limited_to_call_space is False
        assert snapshot.probe_failure_reason is None


class TestPolicyRootMissing:
    def test_no_root_key_means_no_restrictions(self) -> None:
        fake = _fake_winreg(open_raises=FileNotFoundError)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            snapshot = gpd.detect_group_policies()
        assert snapshot.platform_supported is True
        assert snapshot.exclusive_mode_disallowed is False
        assert snapshot.devices_limited_to_call_space is False
        # Both raw values surface as None — operators distinguish
        # "no key" from "key=0 (explicitly permissive)" via this.
        assert snapshot.raw_values["DisallowExclusiveDevice"] is None
        assert snapshot.raw_values["LimitDevicesToCallSpace"] is None
        assert snapshot.probe_failure_reason is None


class TestPolicyValuesPresent:
    def test_disallow_exclusive_set_to_one(self) -> None:
        fake = _fake_winreg(
            raw_values={"DisallowExclusiveDevice": (1, 4)},  # REG_DWORD
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            snapshot = gpd.detect_group_policies()
        assert snapshot.exclusive_mode_disallowed is True
        assert snapshot.raw_values["DisallowExclusiveDevice"] == 1
        assert snapshot.raw_values["LimitDevicesToCallSpace"] is None

    def test_disallow_exclusive_explicitly_zero(self) -> None:
        # Explicit 0 = "policy set, but permissive" — operator
        # configured the GP to disable the restriction. Booleans
        # report False; raw_values surfaces the explicit 0 so
        # operators can distinguish from "absent".
        fake = _fake_winreg(
            raw_values={"DisallowExclusiveDevice": (0, 4)},
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            snapshot = gpd.detect_group_policies()
        assert snapshot.exclusive_mode_disallowed is False
        assert snapshot.raw_values["DisallowExclusiveDevice"] == 0

    def test_both_policies_active(self) -> None:
        fake = _fake_winreg(
            raw_values={
                "DisallowExclusiveDevice": (1, 4),
                "LimitDevicesToCallSpace": (1, 4),
            },
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            snapshot = gpd.detect_group_policies()
        assert snapshot.exclusive_mode_disallowed is True
        assert snapshot.devices_limited_to_call_space is True

    def test_wrong_value_type_treated_as_absent(self) -> None:
        # Someone configured the value as REG_SZ instead of
        # REG_DWORD via gpedit.msc — the detector must NOT
        # crash, NOT misinterpret, just treat as absent.
        fake = _fake_winreg(
            raw_values={"DisallowExclusiveDevice": ("1", 1)},  # REG_SZ
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            snapshot = gpd.detect_group_policies()
        assert snapshot.exclusive_mode_disallowed is False
        assert snapshot.raw_values["DisallowExclusiveDevice"] is None


class TestProbeFailures:
    def test_permission_denied_surfaces_reason(self) -> None:
        fake = _fake_winreg(open_raises=PermissionError)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            snapshot = gpd.detect_group_policies()
        assert snapshot.platform_supported is True
        assert snapshot.probe_failure_reason == "permission_denied"
        # Booleans default to permissive on probe failure (don't
        # block the daemon's bypass strategies on a probe gap).
        assert snapshot.exclusive_mode_disallowed is False

    def test_oserror_surfaces_registry_unavailable(self) -> None:
        fake = _fake_winreg(open_raises=OSError)
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            snapshot = gpd.detect_group_policies()
        assert snapshot.probe_failure_reason == "registry_unavailable"


class TestLogSnapshot:
    def test_no_restrictions_emits_info(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snapshot = gpd.GroupPolicySnapshot(platform_supported=True)
        with caplog.at_level(logging.INFO, logger=_GP_LOGGER):
            gpd.log_group_policy_snapshot(snapshot)
        events = [r for r in caplog.records if r.levelno == logging.INFO]
        # Single INFO with the structured event name.
        assert any(
            isinstance(r.msg, dict) and r.msg.get("event") == "voice.group_policy.no_restrictions"
            for r in events
        )

    def test_exclusive_disallowed_emits_warn(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snapshot = gpd.GroupPolicySnapshot(
            platform_supported=True,
            exclusive_mode_disallowed=True,
        )
        with caplog.at_level(logging.WARNING, logger=_GP_LOGGER):
            gpd.log_group_policy_snapshot(snapshot)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.group_policy.exclusive_mode_disallowed"
            for r in warns
        )

    def test_probe_failure_emits_warn(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snapshot = gpd.GroupPolicySnapshot(
            platform_supported=True,
            probe_failure_reason="permission_denied",
        )
        with caplog.at_level(logging.WARNING, logger=_GP_LOGGER):
            gpd.log_group_policy_snapshot(snapshot)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            isinstance(r.msg, dict) and r.msg.get("event") == "voice.group_policy.probe_failed"
            for r in warns
        )

    def test_non_windows_snapshot_silent(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Non-Windows snapshot has nothing to say.
        snapshot = gpd.GroupPolicySnapshot(platform_supported=False)
        with caplog.at_level(logging.INFO, logger=_GP_LOGGER):
            gpd.log_group_policy_snapshot(snapshot)
        gp_records = [
            r for r in caplog.records if r.name == _GP_LOGGER and isinstance(r.msg, dict)
        ]
        assert gp_records == []
