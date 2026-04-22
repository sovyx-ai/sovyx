"""T12.1 — unit tests for ``CaptureDeviceContendedError`` + heuristic."""

from __future__ import annotations

import pytest

from sovyx.voice._capture_task import (
    CaptureDeviceContendedError,
    CaptureError,
    CaptureInoperativeError,
    CaptureSilenceError,
    _is_session_manager_contention_pattern,
    _suggest_session_manager_alternatives,
)
from sovyx.voice._stream_opener import OpenAttempt
from sovyx.voice.device_test._protocol import ErrorCode


def _attempt(
    *,
    error_code: ErrorCode | None,
    host_api: str = "ALSA",
    device_index: int = 4,
    sample_rate: int = 16_000,
    detail: str = "",
) -> OpenAttempt:
    return OpenAttempt(
        host_api=host_api,
        device_index=device_index,
        sample_rate=sample_rate,
        channels=1,
        auto_convert=False,
        exclusive=False,
        error_code=error_code,
        error_detail=detail,
    )


class TestCaptureErrorHierarchy:
    """Base class established by T7 — subclasses preserve isinstance."""

    def test_capture_error_is_runtime_error(self) -> None:
        assert issubclass(CaptureError, RuntimeError)

    def test_silence_error_inherits_capture_error(self) -> None:
        err = CaptureSilenceError(
            "silent",
            device=4,
            host_api="ALSA",
            observed_peak_rms_db=-90.0,
        )
        assert isinstance(err, CaptureError)
        assert isinstance(err, RuntimeError)

    def test_inoperative_error_inherits_capture_error(self) -> None:
        err = CaptureInoperativeError(
            "inoperative",
            device=4,
            host_api="ALSA",
            reason="no_winner",
            attempts=6,
        )
        assert isinstance(err, CaptureError)

    def test_contended_error_inherits_capture_error(self) -> None:
        err = CaptureDeviceContendedError(
            "contended",
            device=4,
            host_api="ALSA",
            suggested_actions=["select_device:pipewire"],
        )
        assert isinstance(err, CaptureError)
        assert err.suggested_actions == ["select_device:pipewire"]
        assert err.contending_process_hint is None
        assert err.attempts == []


class TestIsSessionManagerContentionPattern:
    def test_non_linux_returns_false(self) -> None:
        attempts = [_attempt(error_code=ErrorCode.DEVICE_BUSY)]
        for platform in ("win32", "darwin", "freebsd"):
            assert not _is_session_manager_contention_pattern(
                platform=platform,
                open_attempts=attempts,
            )

    def test_empty_attempts_returns_false(self) -> None:
        assert not _is_session_manager_contention_pattern(
            platform="linux",
            open_attempts=[],
        )

    def test_all_device_busy_returns_true(self) -> None:
        attempts = [_attempt(error_code=ErrorCode.DEVICE_BUSY) for _ in range(3)]
        assert _is_session_manager_contention_pattern(
            platform="linux",
            open_attempts=attempts,
        )

    def test_mixed_error_codes_return_false(self) -> None:
        attempts = [
            _attempt(error_code=ErrorCode.DEVICE_BUSY),
            _attempt(error_code=ErrorCode.UNSUPPORTED_SAMPLERATE),
        ]
        assert not _is_session_manager_contention_pattern(
            platform="linux",
            open_attempts=attempts,
        )

    def test_device_disappeared_and_not_found_count_as_contention(self) -> None:
        attempts = [
            _attempt(error_code=ErrorCode.DEVICE_BUSY),
            _attempt(error_code=ErrorCode.DEVICE_DISAPPEARED),
            _attempt(error_code=ErrorCode.DEVICE_NOT_FOUND),
        ]
        assert _is_session_manager_contention_pattern(
            platform="linux",
            open_attempts=attempts,
        )

    def test_none_error_code_returns_false(self) -> None:
        # An attempt that "succeeded" (error_code=None) means not all
        # attempts were contention; short-circuit to False.
        attempts = [
            _attempt(error_code=ErrorCode.DEVICE_BUSY),
            _attempt(error_code=None),
        ]
        assert not _is_session_manager_contention_pattern(
            platform="linux",
            open_attempts=attempts,
        )

    def test_permission_denied_does_not_count(self) -> None:
        attempts = [_attempt(error_code=ErrorCode.PERMISSION_DENIED)]
        assert not _is_session_manager_contention_pattern(
            platform="linux",
            open_attempts=attempts,
        )


class TestSuggestSessionManagerAlternatives:
    def test_returns_preferred_tokens_in_order(self) -> None:
        actions = _suggest_session_manager_alternatives()
        assert actions[0] == "select_device:pipewire"
        assert "select_device:default" in actions
        assert "stop_process:pipewire" in actions

    def test_stable_across_calls(self) -> None:
        assert _suggest_session_manager_alternatives() == _suggest_session_manager_alternatives()


class TestCaptureDeviceContendedErrorAttributesAreMutable:
    """Defence: copy the inputs so caller mutations don't leak."""

    def test_suggested_actions_is_copied(self) -> None:
        mutable = ["select_device:pipewire"]
        err = CaptureDeviceContendedError(
            "x",
            device=0,
            host_api=None,
            suggested_actions=mutable,
        )
        mutable.append("select_device:poisoned")
        assert err.suggested_actions == ["select_device:pipewire"]

    def test_attempts_is_copied(self) -> None:
        mutable = [_attempt(error_code=ErrorCode.DEVICE_BUSY)]
        err = CaptureDeviceContendedError(
            "x",
            device=0,
            host_api=None,
            suggested_actions=[],
            attempts=mutable,
        )
        mutable.clear()
        assert len(err.attempts) == 1


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.DEVICE_BUSY, True),
        (ErrorCode.DEVICE_DISAPPEARED, True),
        (ErrorCode.DEVICE_NOT_FOUND, True),
        (ErrorCode.UNSUPPORTED_SAMPLERATE, False),
        (ErrorCode.PERMISSION_DENIED, False),
        (ErrorCode.INTERNAL_ERROR, False),
    ],
)
def test_per_error_code_contention_classification(
    code: ErrorCode,
    expected: bool,
) -> None:
    attempts = [_attempt(error_code=code)]
    assert (
        _is_session_manager_contention_pattern(
            platform="linux",
            open_attempts=attempts,
        )
        is expected
    )
