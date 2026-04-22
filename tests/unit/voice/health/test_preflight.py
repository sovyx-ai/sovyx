"""Unit tests for :mod:`sovyx.voice.health.preflight` (ADR §4.5)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from sovyx.voice.health import (
    PreflightReport,
    PreflightStep,
    PreflightStepCode,
    PreflightStepSpec,
    check_portaudio,
    check_tts_synthesize,
    check_wake_word_smoke,
    current_platform_key,
    default_step_names,
    run_preflight,
)


def _pass_check(
    details: dict[str, Any] | None = None,
) -> Any:
    """Return an async check that reports success with empty hint."""

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        return True, "", details or {}

    return _check


def _fail_check(hint: str = "nope", details: dict[str, Any] | None = None) -> Any:
    """Return an async check that reports failure with ``hint``."""

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        return False, hint, details or {}

    return _check


def _crash_check(exc: Exception) -> Any:
    """Return an async check that raises ``exc``."""

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        raise exc

    return _check


class _MonotonicStub:
    """Deterministic monotonic clock for duration assertions."""

    def __init__(self, *, start: float = 1000.0, step: float = 0.1) -> None:
        self._t = start
        self._step = step

    def __call__(self) -> float:
        now = self._t
        self._t += self._step
        return now


class TestDefaultStepNames:
    """Canonical 9-step mapping stays stable."""

    def test_covers_all_nine_steps(self) -> None:
        names = default_step_names()
        assert set(names.keys()) == {1, 2, 3, 4, 5, 6, 7, 8, 9}

    def test_codes_align_with_enum(self) -> None:
        names = default_step_names()
        assert names[1][1] is PreflightStepCode.MIC_MUTED
        assert names[4][1] is PreflightStepCode.PORTAUDIO_UNAVAILABLE
        assert names[8][1] is PreflightStepCode.WAKE_WORD_MISBEHAVING
        assert names[9][1] is PreflightStepCode.LINUX_MIXER_SATURATED

    def test_current_platform_key_matches_sys_platform(self) -> None:
        import sys

        assert current_platform_key() == sys.platform


class TestPreflightOrchestrator:
    """``run_preflight`` contract and exception semantics."""

    @pytest.mark.asyncio()
    async def test_all_pass_report(self) -> None:
        specs = [
            PreflightStepSpec(
                step=1, name="one", code=PreflightStepCode.MIC_MUTED, check=_pass_check()
            ),
            PreflightStepSpec(
                step=2,
                name="two",
                code=PreflightStepCode.MIC_PERMISSION_DENIED,
                check=_pass_check({"level": "ok"}),
            ),
        ]
        report = await run_preflight(steps=specs, clock=_MonotonicStub())
        assert isinstance(report, PreflightReport)
        assert report.passed is True
        assert report.first_failure is None
        assert len(report.steps) == 2
        assert [s.step for s in report.steps] == [1, 2]
        assert all(isinstance(s, PreflightStep) for s in report.steps)
        assert report.steps[1].details == {"level": "ok"}

    @pytest.mark.asyncio()
    async def test_runs_steps_in_input_order(self) -> None:
        order: list[int] = []

        def _tracer(step: int) -> Any:
            async def _check() -> tuple[bool, str, dict[str, Any]]:
                order.append(step)
                return True, "", {}

            return _check

        specs = [
            PreflightStepSpec(
                step=3, name="c", code=PreflightStepCode.MODELS_CORRUPT, check=_tracer(3)
            ),
            PreflightStepSpec(
                step=1, name="a", code=PreflightStepCode.MIC_MUTED, check=_tracer(1)
            ),
            PreflightStepSpec(
                step=7,
                name="g",
                code=PreflightStepCode.LLM_UNREACHABLE,
                check=_tracer(7),
            ),
        ]
        await run_preflight(steps=specs)
        assert order == [3, 1, 7]

    @pytest.mark.asyncio()
    async def test_stop_on_first_failure_default(self) -> None:
        ran: list[int] = []

        def _trace_pass(step: int) -> Any:
            async def _check() -> tuple[bool, str, dict[str, Any]]:
                ran.append(step)
                return True, "", {}

            return _check

        def _trace_fail(step: int) -> Any:
            async def _check() -> tuple[bool, str, dict[str, Any]]:
                ran.append(step)
                return False, "boom", {}

            return _check

        specs = [
            PreflightStepSpec(
                step=1, name="a", code=PreflightStepCode.MIC_MUTED, check=_trace_pass(1)
            ),
            PreflightStepSpec(
                step=2,
                name="b",
                code=PreflightStepCode.MIC_PERMISSION_DENIED,
                check=_trace_fail(2),
            ),
            PreflightStepSpec(
                step=3,
                name="c",
                code=PreflightStepCode.MODELS_CORRUPT,
                check=_trace_pass(3),
            ),
        ]
        report = await run_preflight(steps=specs)
        assert ran == [1, 2]
        assert report.passed is False
        assert report.first_failure is not None
        assert report.first_failure.step == 2
        assert report.first_failure.hint == "boom"
        assert len(report.steps) == 2

    @pytest.mark.asyncio()
    async def test_no_short_circuit_runs_every_step(self) -> None:
        specs = [
            PreflightStepSpec(
                step=1, name="a", code=PreflightStepCode.MIC_MUTED, check=_pass_check()
            ),
            PreflightStepSpec(
                step=2,
                name="b",
                code=PreflightStepCode.MIC_PERMISSION_DENIED,
                check=_fail_check("first"),
            ),
            PreflightStepSpec(
                step=3,
                name="c",
                code=PreflightStepCode.MODELS_CORRUPT,
                check=_fail_check("second"),
            ),
        ]
        report = await run_preflight(steps=specs, stop_on_first_failure=False)
        assert len(report.steps) == 3
        assert report.passed is False
        assert report.first_failure is not None
        assert report.first_failure.step == 2
        assert report.first_failure.hint == "first"

    @pytest.mark.asyncio()
    async def test_exception_becomes_failed_step(self) -> None:
        specs = [
            PreflightStepSpec(
                step=4,
                name="pa",
                code=PreflightStepCode.PORTAUDIO_UNAVAILABLE,
                check=_crash_check(RuntimeError("host api init failed")),
            ),
        ]
        report = await run_preflight(steps=specs)
        assert report.passed is False
        assert report.first_failure is not None
        assert report.first_failure.passed is False
        assert "host api init failed" in report.first_failure.hint
        assert report.first_failure.details.get("exception_type") == "RuntimeError"

    @pytest.mark.asyncio()
    async def test_empty_steps_raises_value_error(self) -> None:
        with pytest.raises(ValueError) as exc:
            await run_preflight(steps=[])
        assert "at least one step" in str(exc.value)

    @pytest.mark.asyncio()
    async def test_duplicate_step_number_raises(self) -> None:
        specs = [
            PreflightStepSpec(
                step=2, name="a", code=PreflightStepCode.MIC_MUTED, check=_pass_check()
            ),
            PreflightStepSpec(
                step=2,
                name="b",
                code=PreflightStepCode.MIC_PERMISSION_DENIED,
                check=_pass_check(),
            ),
        ]
        with pytest.raises(ValueError) as exc:
            await run_preflight(steps=specs)
        assert "duplicate" in str(exc.value)

    @pytest.mark.asyncio()
    async def test_duration_tracked_with_injected_clock(self) -> None:
        clock = _MonotonicStub(start=10.0, step=0.25)
        specs = [
            PreflightStepSpec(
                step=1, name="a", code=PreflightStepCode.MIC_MUTED, check=_pass_check()
            ),
            PreflightStepSpec(
                step=2,
                name="b",
                code=PreflightStepCode.MIC_PERMISSION_DENIED,
                check=_pass_check(),
            ),
        ]
        report = await run_preflight(steps=specs, clock=clock)
        assert report.steps[0].duration_ms == pytest.approx(250.0)
        assert report.steps[1].duration_ms == pytest.approx(250.0)
        assert report.total_duration_ms > 0.0

    @pytest.mark.asyncio()
    async def test_details_are_snapshot_into_dict(self) -> None:
        live: dict[str, Any] = {"count": 1}

        async def _check() -> tuple[bool, str, dict[str, Any]]:
            return True, "", live

        specs = [
            PreflightStepSpec(
                step=1,
                name="a",
                code=PreflightStepCode.MIC_MUTED,
                check=_check,
            ),
        ]
        report = await run_preflight(steps=specs)
        assert report.steps[0].details == {"count": 1}
        live["count"] = 999
        assert report.steps[0].details == {"count": 1}


class _FakeSD:
    """Minimal ``sounddevice`` stub."""

    def __init__(
        self,
        *,
        host_apis: list[dict[str, Any]] | None = None,
        devices: list[dict[str, Any]] | None = None,
        raise_on_query: Exception | None = None,
    ) -> None:
        self._host_apis = host_apis if host_apis is not None else [{"name": "WASAPI"}]
        self._devices = (
            devices
            if devices is not None
            else [
                {"name": "default", "max_input_channels": 2},
            ]
        )
        self._raise = raise_on_query

    def query_hostapis(self) -> list[dict[str, Any]]:
        if self._raise is not None:
            raise self._raise
        return self._host_apis

    def query_devices(self) -> list[dict[str, Any]]:
        if self._raise is not None:
            raise self._raise
        return self._devices


class TestCheckPortaudio:
    """Step 4 default check."""

    @pytest.mark.asyncio()
    async def test_pass_when_host_and_input_present(self) -> None:
        check = check_portaudio(sd_module=_FakeSD())
        passed, hint, details = await check()
        assert passed is True
        assert hint == ""
        assert details["host_api_count"] == 1
        assert details["input_device_count"] == 1

    @pytest.mark.asyncio()
    async def test_fail_when_no_input_devices(self) -> None:
        sd = _FakeSD(devices=[{"name": "speaker", "max_input_channels": 0}])
        passed, hint, details = await check_portaudio(sd_module=sd)()
        assert passed is False
        assert "input-capable" in hint
        assert details["input_device_count"] == 0

    @pytest.mark.asyncio()
    async def test_fail_when_no_host_apis(self) -> None:
        sd = _FakeSD(host_apis=[], devices=[])
        passed, hint, _ = await check_portaudio(sd_module=sd)()
        assert passed is False
        assert hint

    @pytest.mark.asyncio()
    async def test_fail_on_query_exception(self) -> None:
        sd = _FakeSD(raise_on_query=RuntimeError("PortAudio error"))
        passed, hint, details = await check_portaudio(sd_module=sd)()
        assert passed is False
        assert "audio service" in hint.lower()
        assert details["exception_type"] == "RuntimeError"
        assert "PortAudio error" in details["error"]


class _StubDetector:
    """Test double mimicking :class:`WakeWordDetector.process_frame`."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = list(scores)
        self._idx = 0

    def process_frame(self, frame: np.ndarray) -> Any:  # noqa: ARG002
        score = self._scores[self._idx % len(self._scores)]
        self._idx += 1
        return type("Event", (), {"score": score, "detected": False})()


class TestCheckWakeWordSmoke:
    """Step 8 default check."""

    @pytest.mark.asyncio()
    async def test_pass_when_silence_below_threshold(self) -> None:
        detector = _StubDetector(scores=[0.05, 0.1, 0.02])
        check = check_wake_word_smoke(detector=detector, duration_ms=96.0)
        passed, hint, details = await check()
        assert passed is True
        assert hint == ""
        assert details["max_observed_score"] <= 0.3
        assert details["frames_tested"] >= 1

    @pytest.mark.asyncio()
    async def test_fail_when_spurious_score_above_threshold(self) -> None:
        detector = _StubDetector(scores=[0.9])
        check = check_wake_word_smoke(detector=detector, duration_ms=96.0, max_score=0.3)
        passed, hint, details = await check()
        assert passed is False
        assert "miscalibrated" in hint or "wrong file" in hint
        assert details["max_observed_score"] == pytest.approx(0.9)
        assert details["max_allowed_score"] == pytest.approx(0.3)

    @pytest.mark.asyncio()
    async def test_tight_threshold_catches_near_pass(self) -> None:
        detector = _StubDetector(scores=[0.15])
        check = check_wake_word_smoke(detector=detector, duration_ms=96.0, max_score=0.1)
        passed, _, _ = await check()
        assert passed is False


class _StubChunk:
    def __init__(self, samples: int, sample_rate: int = 22_050) -> None:
        self.audio = np.zeros(samples, dtype=np.float32)
        self.sample_rate = sample_rate


class _StubTTS:
    """Async TTS stub with configurable behavior."""

    def __init__(
        self,
        *,
        chunk: _StubChunk | None = None,
        raise_on_synthesize: Exception | None = None,
    ) -> None:
        self._chunk = chunk
        self._raise = raise_on_synthesize
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> Any:
        self.calls.append(text)
        if self._raise is not None:
            raise self._raise
        return self._chunk


class TestCheckTtsSynthesize:
    """Step 6 default check."""

    @pytest.mark.asyncio()
    async def test_pass_on_sufficient_samples(self) -> None:
        tts = _StubTTS(chunk=_StubChunk(samples=500, sample_rate=22_050))
        check = check_tts_synthesize(tts=tts, phrase="ok", min_samples=100)
        passed, hint, details = await check()
        assert passed is True
        assert hint == ""
        assert details["sample_count"] == 500
        assert details["sample_rate"] == 22_050
        assert tts.calls == ["ok"]

    @pytest.mark.asyncio()
    async def test_fail_on_too_few_samples(self) -> None:
        tts = _StubTTS(chunk=_StubChunk(samples=10))
        passed, hint, details = await check_tts_synthesize(
            tts=tts,
            min_samples=100,
        )()
        assert passed is False
        assert "only 10 samples" in hint
        assert details["sample_count"] == 10
        assert details["min_samples"] == 100

    @pytest.mark.asyncio()
    async def test_fail_on_synthesize_exception(self) -> None:
        tts = _StubTTS(raise_on_synthesize=RuntimeError("onnx crash"))
        passed, hint, details = await check_tts_synthesize(tts=tts)()
        assert passed is False
        assert "text-to-speech" in hint.lower()
        assert details["exception_type"] == "RuntimeError"
        assert "onnx crash" in details["error"]
