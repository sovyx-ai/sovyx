"""Tests for the WI3 wire-up: APO DLL introspection enrichment of
:class:`~sovyx.voice._apo_detector.CaptureApoReport`.

The wire-up is opt-in via
``VoiceTuningConfig.voice_apo_dll_introspection_enabled``. Default
False preserves the prior behaviour (static catalog only). When
enabled, CLSIDs NOT in the catalog get enriched with DLL version-info
via :func:`~sovyx.voice._apo_dll_introspect.introspect_apo_clsid`.

Pure unit tests — exercise ``_maybe_introspect_unknown_clsids``
directly with mocked config + introspection helper. The full
``detect_capture_apos`` path is exercised in pre-existing
``tests/unit/voice/test_apo_detector.py`` and remains unchanged
(default-off ⇒ unknown_clsid_dll_info stays empty ⇒ existing
assertions still hold).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice._apo_detector import (
    CaptureApoReport,
    _maybe_introspect_unknown_clsids,
)
from sovyx.voice._apo_dll_introspect import ApoDllInfo

_KNOWN_MS_VOICE_CLARITY = "{7A8B0F43-6C2E-4C85-A1A6-C9F1F7D50E9D}"  # Voice Focus
_KNOWN_MS_AGC = "{9CF81848-DE9F-4BDF-B177-A9D8B16A7AAB}"  # AGC
_UNKNOWN_VENDOR_CLSID = "{ABCDEF12-3456-7890-ABCD-EF1234567890}"
_UNKNOWN_VENDOR_CLSID_2 = "{99887766-5544-3322-1100-AABBCCDDEEFF}"


# ── Default-off behaviour (regression guard) ──────────────────────


class TestWireupDefault:
    def test_default_returns_empty_dict(self) -> None:
        # Real config (no patch) — default is False.
        result = _maybe_introspect_unknown_clsids(
            [_UNKNOWN_VENDOR_CLSID, _UNKNOWN_VENDOR_CLSID_2],
        )
        assert result == {}

    def test_capture_apo_report_default_field_empty(self) -> None:
        # Construction default — ensures the new field doesn't break
        # existing factory paths that don't set it.
        report = CaptureApoReport(
            endpoint_id="{ENDPOINT}",
            endpoint_name="Test Mic",
            enumerator="USB",
            fx_binding_count=0,
        )
        assert report.unknown_clsid_dll_info == {}


# ── Opt-in behaviour ──────────────────────────────────────────────


class TestEnabledFlag:
    def test_enabled_known_only_returns_empty(self) -> None:
        # All CLSIDs match the static catalog → no introspection runs.
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_apo_dll_introspection_enabled=True),
            ),
            patch(
                "sovyx.voice._apo_dll_introspect.introspect_apo_clsid",
            ) as mock_introspect,
        ):
            result = _maybe_introspect_unknown_clsids(
                [_KNOWN_MS_VOICE_CLARITY, _KNOWN_MS_AGC],
            )
        assert result == {}
        mock_introspect.assert_not_called()

    def test_enabled_unknown_runs_introspection(self) -> None:
        fake_info = ApoDllInfo(
            dll_path=r"C:\Windows\System32\fakeapo.dll",
            file_exists=True,
            file_version="10.0.26100.4351",
            company_name="Microsoft Corporation",
        )
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_apo_dll_introspection_enabled=True),
            ),
            patch(
                "sovyx.voice._apo_dll_introspect.introspect_apo_clsid",
                return_value=fake_info,
            ) as mock_introspect,
        ):
            result = _maybe_introspect_unknown_clsids([_UNKNOWN_VENDOR_CLSID])
        mock_introspect.assert_called_once_with(_UNKNOWN_VENDOR_CLSID)
        assert _UNKNOWN_VENDOR_CLSID in result
        assert result[_UNKNOWN_VENDOR_CLSID].file_version == "10.0.26100.4351"

    def test_enabled_mixed_introspects_only_unknowns(self) -> None:
        fake_info = ApoDllInfo(dll_path="x.dll")
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_apo_dll_introspection_enabled=True),
            ),
            patch(
                "sovyx.voice._apo_dll_introspect.introspect_apo_clsid",
                return_value=fake_info,
            ) as mock_introspect,
        ):
            result = _maybe_introspect_unknown_clsids(
                [_KNOWN_MS_VOICE_CLARITY, _UNKNOWN_VENDOR_CLSID, _KNOWN_MS_AGC],
            )
        # Only the unknown CLSID introspected; known ones skipped
        # (zero registry overhead on the fast path).
        mock_introspect.assert_called_once_with(_UNKNOWN_VENDOR_CLSID)
        assert list(result.keys()) == [_UNKNOWN_VENDOR_CLSID]


# ── Failure isolation ─────────────────────────────────────────────


class TestFailureIsolation:
    def test_per_clsid_failure_does_not_abort_siblings(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ok_info = ApoDllInfo(dll_path="ok.dll")

        def _flaky_introspect(clsid: str) -> ApoDllInfo:
            if clsid == _UNKNOWN_VENDOR_CLSID:
                msg = "intentional"
                raise RuntimeError(msg)
            return ok_info

        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_apo_dll_introspection_enabled=True),
            ),
            patch(
                "sovyx.voice._apo_dll_introspect.introspect_apo_clsid",
                side_effect=_flaky_introspect,
            ),
        ):
            caplog.set_level(logging.WARNING, logger="sovyx.voice._apo_detector")
            result = _maybe_introspect_unknown_clsids(
                [_UNKNOWN_VENDOR_CLSID, _UNKNOWN_VENDOR_CLSID_2],
            )
        # Failed CLSID skipped; sibling still in result.
        assert _UNKNOWN_VENDOR_CLSID not in result
        assert _UNKNOWN_VENDOR_CLSID_2 in result
        # WARN logged for the failed one.
        crash_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.apo.introspect_clsid_failed"
        ]
        assert len(crash_events) == 1
        assert crash_events[0]["clsid"] == _UNKNOWN_VENDOR_CLSID

    def test_config_read_crash_returns_empty_logs_warn(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with patch(
            "sovyx.engine.config.VoiceTuningConfig",
            side_effect=RuntimeError("config boom"),
        ):
            caplog.set_level(logging.WARNING, logger="sovyx.voice._apo_detector")
            result = _maybe_introspect_unknown_clsids([_UNKNOWN_VENDOR_CLSID])
        assert result == {}
        crash_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "voice.apo.config_read_failed"
        ]
        assert len(crash_events) == 1


pytestmark = pytest.mark.timeout(10)
