"""Unit tests for :mod:`sovyx.voice.health.probe`.

Pins ADR §4.3 semantics in code: every ``Diagnosis`` branch is exercised
through the public :func:`~sovyx.voice.health.probe.probe` entry point,
using fake ``sounddevice`` + fake ``SileroVAD`` injections so no
ONNX / PortAudio / scipy dependency participates in the assertion
surface.

Test topology:

* ``_FakeInputStream`` — stands in for ``sd.InputStream``. Spawns a
  background thread that invokes the probe's callback at the rate /
  block size the ``Combo`` requested, feeding synthetic audio from a
  caller-specified generator.
* ``_FakeSoundDevice`` — minimal module-like facade exposing
  ``InputStream`` (and optionally ``WasapiSettings``) so the probe can
  import nothing at test time.
* ``_FakeSileroVAD`` — returns probabilities from a caller-specified
  list / callable, letting tests drive the warm-mode branching
  deterministically.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from sovyx.voice.health.contract import Combo, Diagnosis, ProbeMode
from sovyx.voice.health.probe import _classify_open_error, probe

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeVADEvent:
    probability: float
    is_speech: bool = False
    state: object = None


class _FakeSileroVAD:
    """Minimal VAD stand-in — returns caller-provided probabilities.

    ``probs`` is consumed in order; once exhausted the last value
    repeats so tests don't have to size the list against the probe's
    exact window count.
    """

    def __init__(self, probs: list[float]) -> None:
        if not probs:
            msg = "_FakeSileroVAD requires at least one probability"
            raise ValueError(msg)
        self._probs = list(probs)
        self._idx = 0
        self.frames_seen = 0

    def process_frame(self, _frame: Any) -> _FakeVADEvent:
        self.frames_seen += 1
        if self._idx < len(self._probs):
            p = self._probs[self._idx]
            self._idx += 1
        else:
            p = self._probs[-1]
        return _FakeVADEvent(probability=p)


class _FakeInputStream:
    """In-process ``sd.InputStream`` stand-in.

    Spawns a background thread on :meth:`start` that invokes the
    probe's callback with synthetic audio until :meth:`stop`. The audio
    is provided by ``block_factory(frame_idx)`` returning a numpy array
    with the correct dtype + shape for the ``Combo``.
    """

    def __init__(
        self,
        *,
        device: int,
        samplerate: int,
        channels: int,
        dtype: str,
        blocksize: int,
        callback: Callable[..., None],
        block_factory: Callable[[int], np.ndarray] | None = None,
        open_exc: BaseException | None = None,
        silent: bool = False,
        **_kwargs: Any,
    ) -> None:
        if open_exc is not None:
            raise open_exc
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        self.callback = callback
        self._block_factory = block_factory
        self._silent = silent
        self._running = False
        self._thread: threading.Thread | None = None
        self.started = False
        self.stopped = False
        self.closed = False
        self.started_at: float | None = None
        self.stopped_at: float | None = None

    def start(self) -> None:
        self.started = True
        self.started_at = time.monotonic()
        if self._silent:
            return
        self._running = True
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopped = True
        self.stopped_at = time.monotonic()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def close(self) -> None:
        self.closed = True

    def _feed(self) -> None:
        block_ms = self.blocksize * 1000.0 / self.samplerate
        block_s = block_ms / 1000.0
        frame_idx = 0
        while self._running:
            if self._block_factory is None:
                block = np.zeros(
                    (self.blocksize,) if self.channels == 1 else (self.blocksize, self.channels),
                    dtype=self.dtype if self.dtype != "int24" else "int32",
                )
            else:
                block = self._block_factory(frame_idx)
            try:
                self.callback(block, self.blocksize, None, None)
            except Exception:  # noqa: BLE001
                return
            frame_idx += 1
            time.sleep(block_s)


class _FakeSoundDevice:
    """Minimal ``sounddevice``-like module the probe can dispatch on."""

    def __init__(
        self,
        *,
        stream_factory: Callable[..., _FakeInputStream] | None = None,
        open_exc: BaseException | None = None,
    ) -> None:
        self._stream_factory = stream_factory
        self._open_exc = open_exc
        self.last_stream: _FakeInputStream | None = None

    def InputStream(self, **kwargs: Any) -> _FakeInputStream:  # noqa: N802
        if self._stream_factory is not None:
            stream = self._stream_factory(**kwargs)
        else:
            stream = _FakeInputStream(open_exc=self._open_exc, **kwargs)
        self.last_stream = stream
        return stream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _combo(
    *,
    host_api: str = "Windows WASAPI",
    sample_rate: int = 16_000,
    channels: int = 1,
    sample_format: str = "int16",
    exclusive: bool = False,
    auto_convert: bool = False,
    frames_per_buffer: int = 480,
) -> Combo:
    return Combo(
        host_api=host_api,
        sample_rate=sample_rate,
        channels=channels,
        sample_format=sample_format,
        exclusive=exclusive,
        auto_convert=auto_convert,
        frames_per_buffer=frames_per_buffer,
        platform_key="win32",
    )


def _noise_block_int16(idx: int, blocksize: int, channels: int, amplitude: int) -> np.ndarray:
    """Deterministic pseudo-random noise at the requested amplitude."""
    rng = np.random.default_rng(seed=idx + 1)
    noise = rng.integers(-amplitude, amplitude, size=blocksize, dtype=np.int32).astype(np.int16)
    if channels == 1:
        return noise
    return np.tile(noise.reshape(-1, 1), (1, channels))


def _silence_block_int16(blocksize: int, channels: int) -> np.ndarray:
    if channels == 1:
        return np.zeros(blocksize, dtype=np.int16)
    return np.zeros((blocksize, channels), dtype=np.int16)


# ---------------------------------------------------------------------------
# Cold probe
# ---------------------------------------------------------------------------


class TestColdProbe:
    """ADR §4.3 cold-mode diagnoses."""

    @pytest.mark.asyncio()
    async def test_cold_probe_with_callbacks_reports_healthy(self) -> None:
        """Stream opens + callbacks fire → HEALTHY regardless of RMS."""
        combo = _combo(frames_per_buffer=480)
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda idx: _noise_block_int16(idx, kw["blocksize"], 1, 4_096),
                **kw,
            ),
        )

        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=300,
            sd_module=sd,
        )

        assert result.diagnosis is Diagnosis.HEALTHY
        assert result.mode is ProbeMode.COLD
        assert result.callbacks_fired > 0
        assert result.vad_max_prob is None
        assert result.vad_mean_prob is None
        assert result.duration_ms >= 200

    @pytest.mark.asyncio()
    async def test_cold_probe_with_no_callbacks_reports_no_signal(self) -> None:
        """Stream opens but no callback ever fires → NO_SIGNAL."""
        combo = _combo(frames_per_buffer=480)
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(silent=True, **kw),
        )
        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=200,
            sd_module=sd,
        )
        assert result.diagnosis is Diagnosis.NO_SIGNAL
        assert result.callbacks_fired == 0
        assert result.rms_db == float("-inf")

    @pytest.mark.asyncio()
    async def test_cold_probe_default_duration_is_1500ms(self) -> None:
        """No ``duration_ms`` → ADR default 1 500 ms for cold."""
        combo = _combo()
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(silent=True, **kw),
        )
        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            sd_module=sd,
        )
        # Allow for small OS scheduling jitter
        assert 1_400 <= result.duration_ms <= 1_700

    @pytest.mark.asyncio()
    async def test_cold_probe_ignores_muted_flag(self) -> None:
        """Cold mode can't surface MUTED (no user-intent to match against)."""
        combo = _combo()
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(silent=True, **kw),
        )
        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=150,
            sd_module=sd,
            os_muted=True,
        )
        assert result.diagnosis is Diagnosis.NO_SIGNAL  # still driven by callbacks


# ---------------------------------------------------------------------------
# Warm probe — diagnosis table
# ---------------------------------------------------------------------------


class TestWarmProbeDiagnosisTable:
    """Each warm-mode diagnosis branch from ADR §4.3 must fire."""

    @pytest.mark.asyncio()
    async def test_warm_probe_healthy_vad_high_prob(self) -> None:
        """VAD max ≥ 0.5 with healthy RMS → HEALTHY."""
        combo = _combo(frames_per_buffer=512)
        # Amplitude ~0.5 full-scale → RMS > -10 dB (well above -55)
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda idx: _noise_block_int16(idx, kw["blocksize"], 1, 16_384),
                **kw,
            ),
        )
        vad = _FakeSileroVAD(probs=[0.9])

        result = await probe(
            combo=combo,
            mode=ProbeMode.WARM,
            device_index=0,
            duration_ms=400,
            sd_module=sd,
            vad=vad,
        )
        assert result.diagnosis is Diagnosis.HEALTHY
        assert result.vad_max_prob is not None
        assert result.vad_max_prob >= 0.5
        assert result.vad_mean_prob is not None

    @pytest.mark.asyncio()
    async def test_warm_probe_no_signal_below_minus_70(self) -> None:
        """RMS < -70 dBFS → NO_SIGNAL even with callbacks."""
        combo = _combo(frames_per_buffer=512)
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda _idx: _silence_block_int16(kw["blocksize"], 1),
                **kw,
            ),
        )
        vad = _FakeSileroVAD(probs=[0.0])

        result = await probe(
            combo=combo,
            mode=ProbeMode.WARM,
            device_index=0,
            duration_ms=400,
            sd_module=sd,
            vad=vad,
        )
        assert result.diagnosis is Diagnosis.NO_SIGNAL

    @pytest.mark.asyncio()
    async def test_warm_probe_low_signal_band(self) -> None:
        """-70 ≤ RMS < -55 → LOW_SIGNAL."""
        combo = _combo(frames_per_buffer=512)
        # amplitude ~16 → RMS ≈ -66 dBFS (in the LOW_SIGNAL band)
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda idx: _noise_block_int16(idx, kw["blocksize"], 1, 30),
                **kw,
            ),
        )
        vad = _FakeSileroVAD(probs=[0.0])

        result = await probe(
            combo=combo,
            mode=ProbeMode.WARM,
            device_index=0,
            duration_ms=400,
            sd_module=sd,
            vad=vad,
        )
        assert result.diagnosis is Diagnosis.LOW_SIGNAL
        assert -70.0 <= result.rms_db < -55.0

    @pytest.mark.asyncio()
    async def test_warm_probe_apo_degraded_when_rms_ok_but_vad_dead(self) -> None:
        """Healthy RMS + VAD max < 0.05 → APO_DEGRADED (the whole point of VCHL)."""
        combo = _combo(frames_per_buffer=512)
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda idx: _noise_block_int16(idx, kw["blocksize"], 1, 16_384),
                **kw,
            ),
        )
        vad = _FakeSileroVAD(probs=[0.01])  # dead VAD despite loud input

        result = await probe(
            combo=combo,
            mode=ProbeMode.WARM,
            device_index=0,
            duration_ms=400,
            sd_module=sd,
            vad=vad,
        )
        assert result.diagnosis is Diagnosis.APO_DEGRADED
        assert result.rms_db >= -55.0
        assert result.vad_max_prob is not None
        assert result.vad_max_prob < 0.05

    @pytest.mark.asyncio()
    async def test_warm_probe_vad_insensitive_intermediate(self) -> None:
        """Healthy RMS + 0.05 ≤ VAD max < 0.5 → VAD_INSENSITIVE."""
        combo = _combo(frames_per_buffer=512)
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda idx: _noise_block_int16(idx, kw["blocksize"], 1, 16_384),
                **kw,
            ),
        )
        vad = _FakeSileroVAD(probs=[0.3])

        result = await probe(
            combo=combo,
            mode=ProbeMode.WARM,
            device_index=0,
            duration_ms=400,
            sd_module=sd,
            vad=vad,
        )
        assert result.diagnosis is Diagnosis.VAD_INSENSITIVE

    @pytest.mark.asyncio()
    async def test_warm_probe_muted_short_circuits(self) -> None:
        """``os_muted=True`` in warm mode → MUTED (no stream opened)."""
        combo = _combo()
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda idx: _noise_block_int16(idx, kw["blocksize"], 1, 16_384),
                **kw,
            ),
        )
        vad = _FakeSileroVAD(probs=[0.9])

        result = await probe(
            combo=combo,
            mode=ProbeMode.WARM,
            device_index=0,
            duration_ms=400,
            sd_module=sd,
            vad=vad,
            os_muted=True,
        )
        assert result.diagnosis is Diagnosis.MUTED
        assert sd.last_stream is None  # never opened
        assert result.callbacks_fired == 0


# ---------------------------------------------------------------------------
# Open errors — ADR §4.3 "Stream open failed" branch
# ---------------------------------------------------------------------------


class TestOpenErrors:
    """Every exception class the cascade can see must map to a diagnosis."""

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        ("error_text", "expected"),
        [
            (
                "Error opening InputStream: Device unavailable [PaErrorCode -9985]",
                Diagnosis.DEVICE_BUSY,
            ),
            ("Device is exclusive-mode busy", Diagnosis.DEVICE_BUSY),
            ("Permission denied (microphone access blocked)", Diagnosis.PERMISSION_DENIED),
            ("Access not authorized", Diagnosis.PERMISSION_DENIED),
            ("Invalid sample rate (48000) for this device", Diagnosis.FORMAT_MISMATCH),
            ("Invalid number of channels", Diagnosis.FORMAT_MISMATCH),
            ("Unsupported format", Diagnosis.FORMAT_MISMATCH),
            ("Unknown PortAudio internal failure XYZ", Diagnosis.DRIVER_ERROR),
        ],
    )
    async def test_exception_classification(
        self,
        error_text: str,
        expected: Diagnosis,
    ) -> None:
        combo = _combo()
        sd = _FakeSoundDevice(open_exc=OSError(error_text))
        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=200,
            sd_module=sd,
        )
        assert result.diagnosis is expected
        assert result.error == error_text
        assert result.callbacks_fired == 0
        assert result.duration_ms == 0

    @pytest.mark.asyncio()
    async def test_open_error_echoes_combo_in_result(self) -> None:
        """The returned ProbeResult must still carry the combo the caller passed."""
        combo = _combo(sample_rate=48_000, channels=2, frames_per_buffer=960)
        sd = _FakeSoundDevice(open_exc=OSError("Invalid sample rate"))
        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=200,
            sd_module=sd,
        )
        assert result.combo is combo


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


class TestGuardrails:
    """Defensive checks at the probe boundary."""

    @pytest.mark.asyncio()
    async def test_warm_mode_without_vad_raises(self) -> None:
        combo = _combo()
        sd = _FakeSoundDevice()
        with pytest.raises(ValueError, match="warm probe requires"):
            await probe(
                combo=combo,
                mode=ProbeMode.WARM,
                device_index=0,
                sd_module=sd,
            )

    @pytest.mark.asyncio()
    async def test_negative_duration_raises(self) -> None:
        combo = _combo()
        sd = _FakeSoundDevice()
        with pytest.raises(ValueError, match="duration_ms must be positive"):
            await probe(
                combo=combo,
                mode=ProbeMode.COLD,
                device_index=0,
                duration_ms=-1,
                sd_module=sd,
            )

    @pytest.mark.asyncio()
    async def test_hard_timeout_returns_driver_error(self) -> None:
        """A stream whose start() never returns is killed by the 5 s cap."""
        combo = _combo()

        class _HangingStream:
            def __init__(self, **_kwargs: Any) -> None:
                self.started = False
                self.stopped = False
                self.closed = False

            def start(self) -> None:
                # Hang longer than the hard timeout
                time.sleep(5.0)

            def stop(self) -> None:
                self.stopped = True

            def close(self) -> None:
                self.closed = True

        sd = _FakeSoundDevice(stream_factory=lambda **kw: _HangingStream(**kw))
        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=3_000,
            sd_module=sd,
            hard_timeout_s=0.3,
        )
        assert result.diagnosis is Diagnosis.DRIVER_ERROR
        assert result.error is not None
        assert "hard timeout" in result.error


# ---------------------------------------------------------------------------
# Stream lifecycle — start / stop / close must always be called
# ---------------------------------------------------------------------------


class TestStreamLifecycle:
    @pytest.mark.asyncio()
    async def test_stream_is_stopped_and_closed(self) -> None:
        combo = _combo()
        created: list[_FakeInputStream] = []

        def factory(**kw: Any) -> _FakeInputStream:
            s = _FakeInputStream(
                block_factory=lambda _idx: _silence_block_int16(kw["blocksize"], 1),
                **kw,
            )
            created.append(s)
            return s

        sd = _FakeSoundDevice(stream_factory=factory)
        await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=200,
            sd_module=sd,
        )
        assert len(created) == 1
        assert created[0].started
        assert created[0].stopped
        assert created[0].closed

    @pytest.mark.asyncio()
    async def test_stop_exception_does_not_propagate(self) -> None:
        """Errors during stop/close must not leak out of probe()."""
        combo = _combo()

        class _BadStopStream(_FakeInputStream):
            def stop(self) -> None:
                raise OSError("device already gone")

        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _BadStopStream(
                block_factory=lambda _idx: _silence_block_int16(kw["blocksize"], 1),
                **kw,
            ),
        )
        # Should not raise
        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=200,
            sd_module=sd,
        )
        assert result.diagnosis in {Diagnosis.HEALTHY, Diagnosis.NO_SIGNAL}


# ---------------------------------------------------------------------------
# Stereo + int24 capture paths
# ---------------------------------------------------------------------------


class TestFormatsAndChannels:
    @pytest.mark.asyncio()
    async def test_stereo_cold_probe(self) -> None:
        combo = _combo(channels=2, sample_rate=48_000, frames_per_buffer=480)
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda idx: _noise_block_int16(idx, kw["blocksize"], 2, 8_192),
                **kw,
            ),
        )
        result = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=200,
            sd_module=sd,
        )
        assert result.diagnosis is Diagnosis.HEALTHY
        assert result.callbacks_fired > 0
        # last_stream dtype must have been forwarded as int16 (Combo.sample_format)
        assert sd.last_stream is not None
        assert sd.last_stream.dtype == "int16"
        assert sd.last_stream.channels == 2

    @pytest.mark.asyncio()
    async def test_int24_dtype_is_forwarded_to_sounddevice(self) -> None:
        """Combo.sample_format='int24' → sd.InputStream(dtype='int24')."""
        combo = _combo(sample_format="int24", frames_per_buffer=480)

        # We don't actually feed audio — the dtype wiring is the point.
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(silent=True, **kw),
        )
        await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=150,
            sd_module=sd,
        )
        assert sd.last_stream is not None
        assert sd.last_stream.dtype == "int24"

    @pytest.mark.asyncio()
    async def test_exclusive_mode_uses_wasapi_settings_when_available(self) -> None:
        combo = _combo(exclusive=True, frames_per_buffer=480)

        wasapi_calls: list[dict[str, Any]] = []

        class _WasapiSettings:
            def __init__(self, **kwargs: Any) -> None:
                wasapi_calls.append(kwargs)

        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(silent=True, **kw),
        )
        sd.WasapiSettings = _WasapiSettings  # type: ignore[attr-defined]

        await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=150,
            sd_module=sd,
        )
        assert wasapi_calls == [{"exclusive": True}]


# ---------------------------------------------------------------------------
# Determinism — repeated probes don't share state
# ---------------------------------------------------------------------------


class TestRepeatedProbes:
    @pytest.mark.asyncio()
    async def test_two_probes_in_sequence_are_independent(self) -> None:
        combo = _combo()
        sd1 = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(silent=True, **kw),
        )
        sd2 = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(
                block_factory=lambda idx: _noise_block_int16(idx, kw["blocksize"], 1, 16_384),
                **kw,
            ),
        )
        r1 = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=150,
            sd_module=sd1,
        )
        r2 = await probe(
            combo=combo,
            mode=ProbeMode.COLD,
            device_index=0,
            duration_ms=150,
            sd_module=sd2,
        )
        assert r1.diagnosis is Diagnosis.NO_SIGNAL
        assert r2.diagnosis is Diagnosis.HEALTHY


# ---------------------------------------------------------------------------
# Concurrency — probes must not block the event loop
# ---------------------------------------------------------------------------


class TestEventLoopCooperation:
    @pytest.mark.asyncio()
    async def test_probe_does_not_block_other_tasks(self) -> None:
        """While a probe is sleeping, other asyncio tasks must keep running."""
        combo = _combo()
        sd = _FakeSoundDevice(
            stream_factory=lambda **kw: _FakeInputStream(silent=True, **kw),
        )

        ticks: list[float] = []

        async def ticker() -> None:
            for _ in range(5):
                ticks.append(asyncio.get_running_loop().time())
                await asyncio.sleep(0.03)

        probe_task = asyncio.create_task(
            probe(
                combo=combo,
                mode=ProbeMode.COLD,
                device_index=0,
                duration_ms=250,
                sd_module=sd,
            ),
        )
        ticker_task = asyncio.create_task(ticker())
        result, _ = await asyncio.gather(probe_task, ticker_task)

        assert len(ticks) == 5  # ticker completed alongside the probe
        assert result.diagnosis is Diagnosis.NO_SIGNAL


# ---------------------------------------------------------------------------
# Open-error classifier — §4.4.7 kernel-invalidated triage
# ---------------------------------------------------------------------------


class TestClassifyOpenError:
    """``_classify_open_error`` maps exception text to Diagnosis values.

    Priority order (per probe.py docstring):
        PERMISSION_DENIED > DEVICE_BUSY > FORMAT_MISMATCH >
        KERNEL_INVALIDATED > DRIVER_ERROR (fallback).
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Invalid device",
            "invalid device",
            "INVALID DEVICE",
            "PaErrorCode -9996",
            "paerrorcode -9996",
            "PA_INVALID_DEVICE",
            "AUDCLNT_E_DEVICE_INVALIDATED",
            "wrapped: AUDCLNT_E_DEVICE_INVALIDATED (hex 0x88890004)",
        ],
    )
    def test_kernel_invalidated_keywords_all_classify(self, text: str) -> None:
        assert _classify_open_error(RuntimeError(text)) is Diagnosis.KERNEL_INVALIDATED

    def test_format_mismatch_wins_over_kernel_invalidated(self) -> None:
        # "invalid sample rate" contains the tokens "invalid" and
        # "sample rate"; format-mismatch set matches first, so the result
        # must be FORMAT_MISMATCH rather than KERNEL_INVALIDATED (because
        # "invalid device" is absent).
        assert (
            _classify_open_error(RuntimeError("invalid sample rate")) is Diagnosis.FORMAT_MISMATCH
        )

    def test_device_busy_wins_over_kernel_invalidated(self) -> None:
        # "device unavailable" matches busy; even a concurrent
        # "invalid device" substring must not override.
        assert (
            _classify_open_error(RuntimeError("device unavailable — invalid device"))
            is Diagnosis.DEVICE_BUSY
        )

    def test_permission_wins_over_all(self) -> None:
        assert (
            _classify_open_error(
                RuntimeError("permission denied opening invalid device"),
            )
            is Diagnosis.PERMISSION_DENIED
        )

    def test_unrecognised_falls_back_to_driver_error(self) -> None:
        assert (
            _classify_open_error(RuntimeError("something weird happened"))
            is Diagnosis.DRIVER_ERROR
        )

    def test_empty_message_falls_back_to_driver_error(self) -> None:
        assert _classify_open_error(RuntimeError("")) is Diagnosis.DRIVER_ERROR
