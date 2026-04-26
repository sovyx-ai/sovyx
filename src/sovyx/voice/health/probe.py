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
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice.health._metrics import (
    record_cold_silence_rejected,
    record_probe_result,
    record_start_time_error,
)
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

_COLD_STRICT_VALIDATION_ENABLED = _VoiceTuning().probe_cold_strict_validation_enabled
"""Voice Windows Paranoid Mission Furo W-1 — gate the strict-RMS path
in :func:`_diagnose_cold`.

When ``False`` (legacy v0.23.x behaviour, foundation-phase default in
v0.24.0) the cold-probe accepts any combo with at least one callback
even when ``rms_db < _RMS_DB_NO_SIGNAL_CEILING`` — which is exactly
what lets a Microsoft Voice Clarity APO destroy the signal upstream of
PortAudio yet have the silent combo persist as the winning ComboStore
entry, replicating the failure deterministically on every boot.

When ``True`` (default-flip planned for v0.25.0) silent cold probes
return :attr:`Diagnosis.NO_SIGNAL` so the cascade advances to the next
combo and the silent winner never persists.

Lenient mode (``False``) still emits a structured
``voice.probe.cold_silence_rejected{mode=lenient_passthrough}`` event
so operators can calibrate the rejection rate before flipping the flag.
"""

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
#
# AUDCLNT_E_DEVICE_IN_USE / 0x8889000a / -2004287478 belongs to the
# busy family: another process (or our own voice-test session) is
# holding the endpoint in exclusive mode. Recovery is wait-and-retry
# or close the competing owner — NOT the §4.4.7 fail-over path
# (quarantining a busy device would falsely mark healthy hardware).
_DEVICE_BUSY_KEYWORDS = (
    "device unavailable",
    "busy",
    "exclusive",
    "in use",
    "audclnt_e_device_in_use",
    "0x8889000a",
    "-2004287478",
)
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
# Kernel-invalidated IAudioClient state — see ADR §4.4.7 + the
# forensic report in ``docs-internal/voice-capture-kernel-invalidated.md``.
# PortAudio surfaces this as ``paInvalidDevice`` (-9996) because
# ``IAudioClient::Initialize`` returns one of the AUDCLNT_E_DEVICE_*
# HRESULTs, and sounddevice re-wraps that as "Invalid device". The PnP
# layer still reports the endpoint as healthy (ConfigManagerErrorCode=0),
# so this is *not* a hot-unplug — it's a stuck audio engine that no
# user-mode call can revive. Cure is physical (replug / reboot). We
# match text, hex and signed-decimal forms so we're resilient to
# sounddevice message format drift.
_KERNEL_INVALIDATED_KEYWORDS = (
    "invalid device",
    "paerrorcode -9996",
    "pa_invalid_device",
    "audclnt_e_device_invalidated",
    "0x88890004",
    "-2004287484",
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

    Ordering rationale — kernel_invalidated checked *after* the
    format-mismatch set so an "invalid sample rate" message (which
    contains the token ``"invalid"``) doesn't false-positive as a
    kernel invalidation. The ``_KERNEL_INVALIDATED_KEYWORDS`` strings
    are narrower than their format counterparts; none of them overlap
    with the format-mismatch tokens, but the priority still matters
    if a future message gains a compound phrase.
    """
    msg = str(exc).lower()
    if any(keyword in msg for keyword in _PERMISSION_KEYWORDS):
        return Diagnosis.PERMISSION_DENIED
    if any(keyword in msg for keyword in _DEVICE_BUSY_KEYWORDS):
        return Diagnosis.DEVICE_BUSY
    if any(keyword in msg for keyword in _FORMAT_MISMATCH_KEYWORDS):
        return Diagnosis.FORMAT_MISMATCH
    if any(keyword in msg for keyword in _KERNEL_INVALIDATED_KEYWORDS):
        return Diagnosis.KERNEL_INVALIDATED
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
    start_time_error: BaseException | None = None
    try:
        try:
            await asyncio.to_thread(stream.start)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — translate into Diagnosis (ADR §4.4.7)
            # Stream.start() can raise PortAudio errors AFTER a successful
            # open — notably AUDCLNT_E_DEVICE_INVALIDATED (kernel-side
            # IAudioClient stuck) and AUDCLNT_E_DEVICE_IN_USE (another
            # owner holds the device). Before this branch the exception
            # propagated up to cascade._try_combo and became a generic
            # DRIVER_ERROR, disarming the §4.4.7 fail-over. Classifying
            # here restores the Diagnosis → fail-over chain.
            start_time_error = exc
        else:
            await asyncio.sleep(duration_ms / 1000.0)
    finally:
        # stop() / close() must not mask a probe-time exception.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.stop)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)

    if start_time_error is not None:
        diagnosis = _classify_open_error(start_time_error)
        logger.info(
            "voice_probe_start_failed",
            mode=str(mode),
            combo=_combo_tag(combo),
            diagnosis=str(diagnosis),
            error=str(start_time_error),
        )
        record_start_time_error(
            diagnosis=diagnosis,
            host_api=combo.host_api,
            platform=sys.platform,
        )
        return ProbeResult(
            diagnosis=diagnosis,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=int((time.monotonic() - wall_start) * 1000),
            error=str(start_time_error),
        )

    elapsed_ms = int((time.monotonic() - wall_start) * 1000)

    with blocks_lock:
        collected = list(blocks)

    rms_db = _analyse_rms(collected, combo)

    if mode is ProbeMode.COLD:
        diagnosis = _diagnose_cold(
            callbacks_fired=callbacks_fired,
            rms_db=rms_db,
            combo=combo,
        )
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

    **Host-API routing contract (VLX-001 / ADR `voice-linux-cascade-
    candidate-set` §2.1):**

    Host-API selection happens in two places, with a clear split of
    responsibility:

    1. **Cross-endpoint** — the :func:`~sovyx.voice.health.cascade.run_cascade_for_candidates`
       caller chooses which :class:`~sovyx.voice.health.contract.CandidateEndpoint`
       (and thus which ``device_index``) to probe. On Linux this is how
       the cascade reaches ``pipewire`` / ``default`` virtual PCMs even
       though PortAudio exposes them under the same ``"ALSA"``
       host-API label as a bare ``hw:X,Y`` node — the candidate builder
       resolves each virtual to its own PortAudio index, and this
       function honours that index verbatim.
    2. **Intra-endpoint** — ``combo.host_api`` + ``extra_settings``
       (WASAPI only) drive within-endpoint variants. The cascade table
       still enumerates per-host-API combos so Windows can distinguish
       exclusive vs. shared; on Linux every non-WASAPI combo resolves
       to ``extra_settings=None`` below, which is exactly right because
       PortAudio's Linux build has no ``AlsaSettings`` equivalent —
       the ``device_index`` alone dictates the kernel path.

    Before the ``voice-linux-cascade-root-fix`` refactor, ``run_cascade``
    pinned a single ``device_index`` across every combo in the Linux
    cascade table, making the ``"JACK"`` / ``"PipeWire"`` /
    ``"PulseAudio"`` labels cosmetic — every probe went through ALSA
    direct against the same ``hw:X,Y`` node. The candidate-set cascade
    in T3 fixed that structurally; this function's contract is the
    downstream half of the story.

    Mirrors :func:`sovyx.voice._stream_opener._build_wasapi_settings` so
    both the capture-task opener and the health probe honour the full
    ``(auto_convert, exclusive)`` combo surface. Before this was fixed
    the probe only applied ``exclusive`` — a probe of
    ``Combo(auto_convert=True, exclusive=False)`` silently opened
    without the auto-convert flag, which made the cascade's WASAPI
    auto-convert combos indistinguishable from the default shared open
    and polluted telemetry with false-positive HEALTHY rows.

    The ``sd_module`` has to expose ``WasapiSettings`` for any Windows
    WASAPI combo to take effect; if it doesn't (e.g. a minimal test
    fake), we fall back to a no-extra-settings open so non-WASAPI
    diagnosis paths still exercise the classification + analysis code.
    """
    kwargs: dict[str, Any] = {
        "device": device_index,
        "samplerate": combo.sample_rate,
        "channels": combo.channels,
        "dtype": _FORMAT_TO_SD_DTYPE[combo.sample_format],
        "blocksize": combo.frames_per_buffer,
        "callback": callback,
    }

    extra = _build_probe_wasapi_settings(sd, combo)
    if extra is not None:
        kwargs["extra_settings"] = extra

    # T1 — Forensic-grade observability: record every kwarg finally passed
    # to ``sd.InputStream``. When a probe misbehaves this event is the
    # single source of truth for "what the OS saw", independent of which
    # combo variant produced the call.
    logger.info(
        "voice_probe_stream_open_params",
        device=device_index,
        samplerate=combo.sample_rate,
        channels=combo.channels,
        dtype=kwargs["dtype"],
        blocksize=combo.frames_per_buffer,
        host_api=combo.host_api,
        exclusive=combo.exclusive,
        auto_convert=combo.auto_convert,
        extra_settings_applied=extra is not None,
        extra_settings_type=type(extra).__name__ if extra is not None else None,
    )

    return sd.InputStream(**kwargs)


def _build_probe_wasapi_settings(
    sd: SoundDeviceModule,
    combo: Combo,
) -> Any | None:  # noqa: ANN401 — WasapiSettings instance, lazily typed
    """Return a :class:`sd.WasapiSettings` matching ``combo`` or ``None``.

    Returns ``None`` when:
        * the combo is on a non-WASAPI host API (the host-API guard
          is defensive — the cascade table never emits a non-WASAPI
          combo with either flag set, but a hand-built test combo
          could),
        * the combo asks for neither ``auto_convert`` nor ``exclusive``
          (plain shared-mode open — no extra settings required),
        * the sounddevice build doesn't expose ``WasapiSettings``
          (older PortAudio wheels), or
        * the constructor rejects the kwarg set (``TypeError`` on
          wheels that don't know ``exclusive=``).

    Keeping this helper local to the probe avoids coupling the VCHL
    layer to the capture-task opener's internal ``_Combo`` dataclass.
    """
    if combo.host_api not in {"WASAPI", "Windows WASAPI"}:
        return None
    if not combo.auto_convert and not combo.exclusive:
        return None
    cls = getattr(sd, "WasapiSettings", None)
    if cls is None:
        return None
    kwargs: dict[str, Any] = {}
    if combo.auto_convert:
        kwargs["auto_convert"] = True
    if combo.exclusive:
        kwargs["exclusive"] = True
    try:
        return cls(**kwargs)
    except TypeError:
        return None


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


def _diagnose_cold(
    *,
    callbacks_fired: int,
    rms_db: float,
    combo: Combo,
    vad_max_prob: float | None = None,
) -> Diagnosis:
    """Cold-mode diagnosis (ADR §4.3 — amended by Voice Windows
    Paranoid Mission Furo W-1).

    The cold probe runs without the VAD attached, so the diagnosis is a
    function of how many audio callbacks the driver delivered and the
    energy of the captured signal:

    * ``callbacks_fired == 0``        →  :attr:`Diagnosis.NO_SIGNAL`
    * silent (``rms_db < _RMS_DB_NO_SIGNAL_CEILING``):

      * strict mode (post-fix, ``_COLD_STRICT_VALIDATION_ENABLED=True``)
        → :attr:`Diagnosis.NO_SIGNAL` and emit
        ``voice.probe.cold_silence_rejected{mode=strict_reject}``.
      * lenient mode (legacy v0.23.x, foundation-phase default in
        v0.24.0) → :attr:`Diagnosis.HEALTHY` (preserves prior
        acceptance) and emit
        ``voice.probe.cold_silence_rejected{mode=lenient_passthrough}``
        for telemetry-only calibration.

    * any other case → :attr:`Diagnosis.HEALTHY`.

    The ``vad_max_prob`` keyword is accepted but ignored on the cold
    path — the cold probe never runs the VAD (probe.py call site
    explicitly skips it). The kwarg keeps the signature symmetric with
    :func:`_diagnose_warm` so future refactoring can collapse the
    branches without touching call sites.

    Reuses ``probe_rms_db_no_signal`` (default −70 dBFS) — a level that
    is 4 LSB at int16, well below the ambient room floor (−55 to −45
    dBFS on typical desktops).
    """
    if callbacks_fired == 0:
        return Diagnosis.NO_SIGNAL

    if rms_db >= _RMS_DB_NO_SIGNAL_CEILING:
        return Diagnosis.HEALTHY

    # Silent cold probe — Voice Clarity-style upstream destruction
    # leaves callbacks firing while PCM is exact zero. Strict mode
    # rejects; lenient mode keeps legacy acceptance for one minor cycle
    # but still surfaces telemetry so operators can validate the rate
    # before flipping the flag.
    if _COLD_STRICT_VALIDATION_ENABLED:
        logger.warning(
            "voice.probe.cold_silence_rejected",
            mode="strict_reject",
            rms_db=rms_db,
            callbacks_fired=callbacks_fired,
            host_api=combo.host_api,
            sample_rate=combo.sample_rate,
            channels=combo.channels,
            sample_format=combo.sample_format,
            exclusive=combo.exclusive,
        )
        record_cold_silence_rejected(mode="strict_reject", host_api=combo.host_api)
        return Diagnosis.NO_SIGNAL

    logger.warning(
        "voice.probe.cold_silence_rejected",
        mode="lenient_passthrough",
        rms_db=rms_db,
        callbacks_fired=callbacks_fired,
        host_api=combo.host_api,
        sample_rate=combo.sample_rate,
        channels=combo.channels,
        sample_format=combo.sample_format,
        exclusive=combo.exclusive,
    )
    record_cold_silence_rejected(mode="lenient_passthrough", host_api=combo.host_api)
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
