"""Tests for exclusive-mode open failure classifier [Phase 5 T5.48].

Coverage:

* BUSY class: AUDCLNT_E_DEVICE_IN_USE / 0x8889000a /
  paDeviceUnavailable (-9985) / "device or resource busy".
* UNSUPPORTED class: AUDCLNT_E_UNSUPPORTED_FORMAT / 0x88890008 /
  paBadIODeviceCombination (-9988) / paInvalidDevice (-9996) /
  "format not supported".
* GP_BLOCKED class: E_ACCESSDENIED / "access is denied" /
  "group policy" / AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED paired
  with access-denied wording.
* OTHER catch-all when nothing matches.
* Detail field truncated to 256 chars.
* Priority ordering: GP_BLOCKED outranks BUSY (the
  ``exclusive_mode_not_allowed`` substring would otherwise match
  both).
"""

from __future__ import annotations

import pytest  # noqa: F401  — fixture-only

from sovyx.voice._exclusive_open_classifier import (
    ExclusiveOpenFailureClass,
    classify_exclusive_open_failure,
)


def _err(text: str) -> Exception:
    return Exception(text)


class TestBusyClass:
    @pytest.mark.parametrize(
        "msg",
        [
            "AUDCLNT_E_DEVICE_IN_USE",
            "PortAudio error 0x8889000A: device in use",
            "PaErrorCode -9985 paDeviceUnavailable",
            "Device unavailable on host API WASAPI",
            "Error from host API: Device or resource busy",
        ],
    )
    def test_busy_patterns_classified(self, msg: str) -> None:
        report = classify_exclusive_open_failure(_err(msg))
        assert report.failure_class == ExclusiveOpenFailureClass.BUSY
        assert "another app" in report.remediation.lower()


class TestUnsupportedClass:
    @pytest.mark.parametrize(
        "msg",
        [
            "AUDCLNT_E_UNSUPPORTED_FORMAT",
            "PortAudio error 0x88890008",
            "HRESULT -2004287480",
            "PaErrorCode -9988 paBadIODeviceCombination",
            "PaErrorCode -9996 paInvalidDevice",
            "Invalid sample rate for exclusive endpoint",
            "format not supported",
        ],
    )
    def test_unsupported_patterns_classified(self, msg: str) -> None:
        report = classify_exclusive_open_failure(_err(msg))
        assert report.failure_class == ExclusiveOpenFailureClass.UNSUPPORTED
        assert "tier 1" in report.remediation.lower() or "tier 3" in report.remediation.lower()


class TestGpBlockedClass:
    @pytest.mark.parametrize(
        "msg",
        [
            "E_ACCESSDENIED 0x80070005",
            "Access is denied while opening exclusive endpoint",
            "Group Policy restricts exclusive mode",
            "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED — access is denied",
        ],
    )
    def test_gp_blocked_patterns_classified(self, msg: str) -> None:
        report = classify_exclusive_open_failure(_err(msg))
        assert report.failure_class == ExclusiveOpenFailureClass.GP_BLOCKED
        assert "policy" in report.remediation.lower()


class TestOtherClass:
    def test_unrecognised_error_falls_through_to_other(self) -> None:
        report = classify_exclusive_open_failure(
            _err("Unexpected: stream open returned NaN samples"),
        )
        assert report.failure_class == ExclusiveOpenFailureClass.OTHER
        # Original detail preserved (lowercased, truncated).
        assert "nan samples" in report.detail


class TestPriorityOrdering:
    def test_gp_blocked_beats_busy_when_access_denied_present(self) -> None:
        # AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED is in BOTH the
        # busy and gp-blocked patterns. When paired with an
        # access-denied marker, GP_BLOCKED must win — the
        # access-denied marker disambiguates the policy-driven
        # case from transient contention.
        report = classify_exclusive_open_failure(
            _err(
                "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED — Access is denied",
            ),
        )
        assert report.failure_class == ExclusiveOpenFailureClass.GP_BLOCKED


class TestDetailTruncation:
    def test_long_message_truncated_to_256_chars(self) -> None:
        long_msg = "AUDCLNT_E_DEVICE_IN_USE " + ("x" * 1024)
        report = classify_exclusive_open_failure(_err(long_msg))
        assert len(report.detail) <= 256  # noqa: PLR2004
        # The classifying substring is preserved (it sits at
        # position 0 of the truncated string).
        assert "audclnt_e_device_in_use" in report.detail
