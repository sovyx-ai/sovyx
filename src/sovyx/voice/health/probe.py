"""L3 — Voice Capture Health Lifecycle probe.

Single entry point for the two probe modes described in
``docs-internal/ADR-voice-capture-health-lifecycle.md`` §4.3:

* :attr:`~sovyx.voice.health.contract.ProbeMode.COLD` — boot-time
  validation with no user speaking. Verifies that the stream opens
  cleanly and that PortAudio callbacks are firing. Cannot tell a silent
  room apart from a destroyed signal, so its diagnosis surface is
  deliberately coarse (``HEALTHY`` / ``NO_SIGNAL`` / open-error family).

* :attr:`~sovyx.voice.health.contract.ProbeMode.WARM` — wizard or
  first-interaction validation where the user is asked to speak. Runs
  the captured audio through :class:`~sovyx.voice._frame_normalizer.FrameNormalizer`
  and :class:`~sovyx.voice.vad.SileroVAD` so the probe can derive the
  full :class:`~sovyx.voice.health.contract.Diagnosis` surface (in
  particular :attr:`~sovyx.voice.health.contract.Diagnosis.APO_DEGRADED`,
  which requires signal *content* evidence — healthy RMS + dead VAD).

Design constraints:

* Open a stream with *exactly* the :class:`~sovyx.voice.health.contract.Combo`
  the caller supplied. No internal fallback pyramid — the probe is the
  atomic unit the cascade (L2) uses to evaluate a single combo. Host-API
  / rate / channel fallback belongs to :mod:`sovyx.voice.health.cascade`,
  not here.
* Blocking PortAudio calls (``sd.InputStream`` constructor, ``.start()``,
  ``.stop()``, ``.close()``) run on ``asyncio.to_thread`` so the event
  loop never stalls (CLAUDE.md anti-pattern #14).
* Hard 5 s wall-clock ceiling per probe via :func:`asyncio.wait_for` —
  the ADR commits to this, and without it a misbehaving driver could
  block the cascade indefinitely.
* Open failures are classified into
  :class:`~sovyx.voice.health.contract.Diagnosis` immediately so the
  cascade can drive its fallthrough logic without having to inspect raw
  exception text.
* Dependency injection for ``sd_module`` / ``vad`` / ``frame_normalizer_factory``
  keeps tests free of ``sys.modules`` patching (CLAUDE.md anti-pattern #2)
  and of the real ONNX weights.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import threading
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice.health._metrics import record_probe_result
from sovyx.voice.health.contract import (
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy.typing as npt

    from sovyx.voice.vad import SileroVAD

logger = get_logger(__name__)


# ── Probe tuning defaults ───────────────────────────────────────────────
#
# Sourced from :class:`VoiceTuningConfig` so every knob is overridable via
# ``SOVYX_TUNING__VOICE__PROBE_*`` env vars. CLAUDE.md anti-pattern #17.

_DEFAULT_COLD_DURATION_MS = _VoiceTuning().probe_cold_duration_ms
"""Cold probe target duration. ADR §4.3."""

_DEFAULT_WARM_DURATION_MS = _VoiceTuning().probe_warm_duration_ms
"""Warm probe target duration. ADR §4.3."""

_WARMUP_DISCARD_MS = _VoiceTuning().probe_warmup_discard_ms
"""Audio discarded at the start of every probe (VAD warmup + driver settle)."""

_HARD_TIMEOUT_S = _VoiceTuning().probe_hard_timeout_s
"""Hard wall-clock ceiling per probe (ADR §4.3)."""

_RMS_DB_NO_SIGNAL_CEILING = _VoiceTuning().probe_rms_db_no_signal
"""Below this dBFS, warm-probe diagnosis is :attr:`Diagnosis.NO_SIGNAL`."""

_RMS_DB_LOW_SIGNAL_CEILING = _VoiceTuning().probe_rms_db_low_signal
"""Between no_signal and low-signal, diagnosis is :attr:`Diagnosis.LOW_SIGNAL`."""

_VAD_APO_DEGRADED_CEILING = _VoiceTuning().probe_vad_apo_degraded_ceiling
"""Max VAD probability below which a healthy-RMS signal is diagnosed as APO-corrupted."""

_VAD_HEALTHY_FLOOR = _VoiceTuning().probe_vad_healthy_floor
"""Max VAD probability above which the warm probe is :attr:`Diagnosis.HEALTHY`."""

_TARGET_PIPELINE_RATE = 16_000
_TARGET_PIPELINE_WINDOW = 512

_FORMAT_TO_SD_DTYPE: dict[str, str] = {
    "int16": "int16",
    "int24": "int24",
    "float32": "float32",
}
"""Combo.sample_format → sounddevice ``dtype`` string.

``int24`` is a sounddevice-specific alias that maps to PortAudio's
``paInt24`` and delivers int32 numpy buffers with 24-bit sign-extended
payload (the scaling the FrameNormalizer expects).
"""

# Keywords mapped to the ADR's open-error diagnoses. Matching is done
# case-insensitively against the exception message after classification
# attempts via the exception type.
_DEVICE_BUSY_KEYWORDS = ("device unavailable", "busy", "exclusive", "in use")
_PERMISSION_KEYWORDS = ("permission", "denied", "access", "not authoriz")
_FORMAT_MISMATCH_KEYWORDS = (
    "invalid sample rate",
    "invalid samplerate",
    "sample rate",
    "samplerate",
    "format",
    "channels",
    "invalid number of channels",
    "unsupported",
)


SoundDeviceModule = Any
"""Structural typing placeholder for the sounddevice module.

In production we import :mod:`sounddevice`; in tests a fake with the
subset of the public surface the probe touches is injected via
``sd_module``.
"""

InputStreamLike = Any
"""Structural typing placeholder for ``sd.InputStream`` instances.

The probe only calls ``start()`` / ``stop()`` / ``close()`` on it plus
treats it as a context manager implicitly via the shutdown helper;
fakes in tests implement the same structural surface.
"""


def _classify_open_error(exc: BaseException) -> Diagnosis:
    """Map a PortAudio / OS exception to a :class:`Diagnosis`.

    Exact string matching is fragile; we match keyword sets instead and
    fall back to :attr:`Diagnosis.DRIVER_ERROR` for anything we don't
    recognise (still actionable — the cascade treats DRIVER_ERROR as a
    retry-with-different-combo signal).
    """
    msg = str(exc).lower()
    if any(keyword in msg for keyword in _PERMISSION_KEYWORDS):
        return Diagnosis.PERMISSION_DENIED
    if any(keyword in msg for keyword in _DEVICE_BUSY_KEYWORDS):
        return Diagnosis.DEVICE_BUSY
    if any(keyword in msg for keyword in _FORMAT_MISMATCH_KEYWORDS):
        return Diagnosis.FORMAT_MISMATCH
    return Diagnosis.DRIVER_ERROR


def _linear_to_db(linear: float) -> float:
    """Convert a linear amplitude to dBFS. Returns ``-inf`` for zero."""
    if linear <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(linear)


def _compute_rms_db(block: npt.NDArray[Any], scale: float) -> float:
    """RMS of ``block`` expressed in dBFS.

    ``scale`` normalises the input to the ``[-1, 1]`` range the dBFS
    convention expects (``2**15`` for int16, ``2**23`` for int24,
    ``1.0`` for float32).
    """
    import numpy as np

    if block.size == 0:
        return float("-inf")
    arr = block.astype(np.float64) / scale
    mean_sq = float(np.mean(arr * arr))
    if mean_sq <= 0.0:
        return float("-inf")
    rms_linear = math.sqrt(mean_sq)
    return _linear_to_db(rms_linear)


def _format_scale(sample_format: str) -> float:
    """Return the divisor that puts one sample in ``[-1, 1]``."""
    if sample_format == "int16":
        return float(1 << 15)
    if sample_format == "int24":
        return float(1 << 23)
    if sample_format == "float32":
        return 1.0
    msg = f"unexpected sample_format={sample_format!r}"  # pragma: no cover
    raise ValueError(msg)


def _warmup_samples(combo: Combo) -> int:
    """Count of source-rate samples to discard at probe start."""
    return int(combo.sample_rate * _WARMUP_DISCARD_MS / 1000.0)


async def probe(
    *,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,
    duration_ms: int | None = None,
    sd_module: SoundDeviceModule | None = None,
    vad: SileroVAD | None = None,
    frame_normalizer_factory: (Callable[[int, int, str], FrameNormalizer] | None) = None,
    os_muted: bool = False,
    hard_timeout_s: float = _HARD_TIMEOUT_S,
) -> ProbeResult:
    """Run a single probe and return a :class:`ProbeResult`.

    Args:
        combo: Audio configuration to test. The probe does not attempt
            any alternative rates / channels / formats — that is the
            cascade's responsibility.
        mode: Cold (boot-time, no user) or warm (user asked to speak).
        device_index: PortAudio device index to open.
        duration_ms: Probe window in milliseconds. Defaults to 1 500 ms
            for cold and 3 000 ms for warm, matching ADR §4.3.
        sd_module: Optional ``sounddevice`` module injected for tests.
            Production callers pass ``None`` and the module is imported
            lazily.
        vad: A warm-ready :class:`~sovyx.voice.vad.SileroVAD`. Required
            for :attr:`ProbeMode.WARM` and ignored for
            :attr:`ProbeMode.COLD`.
        frame_normalizer_factory: Optional factory for the resampler
            stage. Only invoked for warm mode. Tests override to avoid
            scipy dependencies; production callers pass ``None`` and
            :class:`FrameNormalizer` is constructed directly.
        os_muted: ``True`` when the OS reports the microphone as muted.
            Warm probes use this to short-circuit to
            :attr:`Diagnosis.MUTED`. Cold probes ignore the flag because
            the stream can still open while muted (RMS will sit at
            ``-inf`` but that is not a distinct cold-mode signal).
        hard_timeout_s: Wall-clock ceiling on the whole probe. Defaults
            to the ADR's 5 s cap.

    Returns:
        :class:`ProbeResult` with ``combo`` echoed for cascade telemetry.

    Raises:
        ValueError: If warm mode is requested without a :class:`SileroVAD`
            instance, or ``duration_ms`` is not positive.
    """
    if mode is ProbeMode.WARM and vad is None:
        msg = "warm probe requires a SileroVAD instance"
        raise ValueError(msg)
    if duration_ms is not None and duration_ms <= 0:
        msg = f"duration_ms must be positive, got {duration_ms}"
        raise ValueError(msg)

    resolved_duration_ms = duration_ms or (
        _DEFAULT_COLD_DURATION_MS if mode is ProbeMode.COLD else _DEFAULT_WARM_DURATION_MS
    )

    try:
        result = await asyncio.wait_for(
            _run_probe(
                combo=combo,
                mode=mode,
                device_index=device_index,
                duration_ms=resolved_duration_ms,
                sd_module=sd_module,
                vad=vad,
                frame_normalizer_factory=frame_normalizer_factory,
                os_muted=os_muted,
            ),
            timeout=hard_timeout_s,
        )
    except TimeoutError:
        logger.warning(
            "voice_probe_hard_timeout",
            mode=str(mode),
            host_api=combo.host_api,
            combo=_combo_tag(combo),
            timeout_s=hard_timeout_s,
        )
        result = ProbeResult(
            diagnosis=Diagnosis.DRIVER_ERROR,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=int(hard_timeout_s * 1000),
            error=f"probe exceeded {hard_timeout_s:.1f}s hard timeout",
        )
    # Emit §5.8 probe metrics once per public invocation. The inner
    # _run_probe may early-return multiple ProbeResult instances; gathering
    # the recording here guarantees exactly one diagnosis + one duration
    # sample per probe() call.
    record_probe_result(result)
    return result


async def _run_probe(
    *,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,
    duration_ms: int,
    sd_module: SoundDeviceModule | None,
    vad: SileroVAD | None,
    frame_normalizer_factory: Callable[[int, int, str], FrameNormalizer] | None,
    os_muted: bool,
) -> ProbeResult:
    """Perform the probe without the outer timeout wrapper."""

    if os_muted and mode is ProbeMode.WARM:
        return ProbeResult(
            diagnosis=Diagnosis.MUTED,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=0,
            error=None,
        )

    sd = sd_module if sd_module is not None else _load_sounddevice()

    blocks: list[npt.NDArray[Any]] = []
    blocks_lock = threading.Lock()
    callbacks_fired = 0

    def _callback(
        indata: npt.NDArray[Any],
        _frames: int,
        _time_info: object,
        _status: object,
    ) -> None:
        nonlocal callbacks_fired
        callbacks_fired += 1
        # PortAudio reuses the incoming buffer; copy so we keep a
        # stable reference after the callback returns.
        with blocks_lock:
            blocks.append(indata.copy())

    try:
        stream = await asyncio.to_thread(
            _open_input_stream,
            sd=sd,
            device_index=device_index,
            combo=combo,
            callback=_callback,
        )
    except BaseException as exc:  # noqa: BLE001 — translate into Diagnosis
        diagnosis = _classify_open_error(exc)
        logger.info(
            "voice_probe_open_failed",
            mode=str(mode),
            combo=_combo_tag(combo),
            diagnosis=str(diagnosis),
            error=str(exc),
        )
        return ProbeResult(
            diagnosis=diagnosis,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=0,
            error=str(exc),
        )

    wall_start = time.monotonic()
    try:
        await asyncio.to_thread(stream.start)
        await asyncio.sleep(duration_ms / 1000.0)
    finally:
        # stop() / close() must not mask a probe-time exception.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.stop)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)

    elapsed_ms = int((time.monotonic() - wall_start) * 1000)

    with blocks_lock:
        collected = list(blocks)

    rms_db = _analyse_rms(collected, combo)

    if mode is ProbeMode.COLD:
        diagnosis = _diagnose_cold(callbacks_fired=callbacks_fired)
        return ProbeResult(
            diagnosis=diagnosis,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=rms_db,
            callbacks_fired=callbacks_fired,
            duration_ms=elapsed_ms,
            error=None,
        )

    assert vad is not None  # enforced by the outer probe() guard
    vad_max_prob, vad_mean_prob = _analyse_vad(
        collected,
        combo=combo,
        vad=vad,
        frame_normalizer_factory=frame_normalizer_factory,
    )
    diagnosis = _diagnose_warm(
        rms_db=rms_db,
        vad_max_prob=vad_max_prob,
        callbacks_fired=callbacks_fired,
    )
    return ProbeResult(
        diagnosis=diagnosis,
        mode=mode,
        combo=combo,
        vad_max_prob=vad_max_prob,
        vad_mean_prob=vad_mean_prob,
        rms_db=rms_db,
        callbacks_fired=callbacks_fired,
        duration_ms=elapsed_ms,
        error=None,
    )


def _open_input_stream(
    *,
    sd: SoundDeviceModule,
    device_index: int,
    combo: Combo,
    callback: Callable[..., None],
) -> InputStreamLike:
    """Construct and return an ``sd.InputStream`` matching ``combo``.

    Exclusive-mode WASAPI requires an ``extra_settings`` object. The
    ``sd_module`` has to expose ``WasapiSettings``; if it doesn't (e.g.
    a minimal fake), we fall back to a no-extra-settings open, which is
    fine for tests because they exercise the classification and
    analysis paths, not WASAPI exclusivity negotiation.
    """
    kwargs: dict[str, Any] = {
        "device": device_index,
        "samplerate": combo.sample_rate,
        "channels": combo.channels,
        "dtype": _FORMAT_TO_SD_DTYPE[combo.sample_format],
        "blocksize": combo.frames_per_buffer,
        "callback": callback,
    }

    if combo.exclusive and hasattr(sd, "WasapiSettings"):
        with contextlib.suppress(Exception):
            kwargs["extra_settings"] = sd.WasapiSettings(exclusive=True)

    return sd.InputStream(**kwargs)


def _load_sounddevice() -> SoundDeviceModule:
    """Lazy import for production callers.

    Kept out of the module prelude so the probe module can be imported
    in environments without PortAudio (e.g. the importer used by the
    dashboard to render typed responses) and the import failure only
    surfaces if someone actually tries to run a probe.
    """
    import sounddevice as sd

    return sd


def _analyse_rms(
    blocks: list[npt.NDArray[Any]],
    combo: Combo,
) -> float:
    """Compute dBFS over the post-warmup concatenation of ``blocks``."""
    import numpy as np

    if not blocks:
        return float("-inf")

    # Downmix multichannel blocks to mono for RMS — VAD does the same.
    mono_blocks: list[npt.NDArray[Any]] = []
    for b in blocks:
        if b.ndim == 2:
            mono_blocks.append(b.mean(axis=1))
        else:
            mono_blocks.append(b)

    flat = np.concatenate(mono_blocks)
    warmup = _warmup_samples(combo)
    if flat.size <= warmup:
        return float("-inf")
    tail = flat[warmup:]
    scale = _format_scale(combo.sample_format)
    return _compute_rms_db(tail, scale)


def _analyse_vad(
    blocks: list[npt.NDArray[Any]],
    *,
    combo: Combo,
    vad: SileroVAD,
    frame_normalizer_factory: Callable[[int, int, str], FrameNormalizer] | None,
) -> tuple[float, float]:
    """Run the post-warmup audio through the resampler + VAD.

    Returns ``(max_prob, mean_prob)``. Both are ``0.0`` when no full
    16 kHz / 512-sample window could be assembled from the captured
    audio (warmup-sized probe or empty stream).
    """
    import numpy as np

    if not blocks:
        return 0.0, 0.0

    factory = frame_normalizer_factory or _default_frame_normalizer_factory
    normalizer = factory(combo.sample_rate, combo.channels, combo.sample_format)

    probabilities: list[float] = []
    warmup_remaining = _warmup_samples(combo)

    for block in blocks:
        # Peel off warmup samples per-block so the VAD sees clean audio.
        if warmup_remaining > 0:
            if block.ndim == 1:
                if block.shape[0] <= warmup_remaining:
                    warmup_remaining -= block.shape[0]
                    continue
                block = block[warmup_remaining:]
            else:
                if block.shape[0] <= warmup_remaining:
                    warmup_remaining -= block.shape[0]
                    continue
                block = block[warmup_remaining:, :]
            warmup_remaining = 0

        windows = normalizer.push(block)
        for window in windows:
            if window.shape != (_TARGET_PIPELINE_WINDOW,):
                continue
            event = vad.process_frame(window)
            probabilities.append(float(event.probability))

    if not probabilities:
        return 0.0, 0.0

    arr = np.asarray(probabilities, dtype=np.float32)
    return float(arr.max()), float(arr.mean())


def _default_frame_normalizer_factory(
    source_rate: int,
    source_channels: int,
    source_format: str,
) -> FrameNormalizer:
    return FrameNormalizer(
        source_rate=source_rate,
        source_channels=source_channels,
        source_format=source_format,
    )


def _diagnose_cold(*, callbacks_fired: int) -> Diagnosis:
    """Cold-mode diagnosis table (ADR §4.3)."""
    if callbacks_fired == 0:
        return Diagnosis.NO_SIGNAL
    return Diagnosis.HEALTHY


def _diagnose_warm(
    *,
    rms_db: float,
    vad_max_prob: float,
    callbacks_fired: int,
) -> Diagnosis:
    """Warm-mode diagnosis table (ADR §4.3)."""
    if callbacks_fired == 0:
        return Diagnosis.NO_SIGNAL
    if rms_db < _RMS_DB_NO_SIGNAL_CEILING:
        return Diagnosis.NO_SIGNAL
    if rms_db < _RMS_DB_LOW_SIGNAL_CEILING:
        return Diagnosis.LOW_SIGNAL
    # rms_db ≥ -55 dB: decide on VAD.
    if vad_max_prob >= _VAD_HEALTHY_FLOOR:
        return Diagnosis.HEALTHY
    if vad_max_prob < _VAD_APO_DEGRADED_CEILING:
        return Diagnosis.APO_DEGRADED
    return Diagnosis.VAD_INSENSITIVE


def _combo_tag(combo: Combo) -> str:
    """Short string for log events (avoid full Combo repr in tight loops)."""
    return (
        f"{combo.host_api}|{combo.sample_rate}|{combo.channels}|"
        f"{combo.sample_format}|excl={combo.exclusive}"
    )


__all__ = ["probe"]
