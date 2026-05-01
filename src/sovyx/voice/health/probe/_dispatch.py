"""Top-level probe orchestration — public :func:`probe` entry point,
:func:`_run_probe` core loop, and the sounddevice / WASAPI stream
lifecycle helpers.

This module is the only piece of the probe subpackage that touches
PortAudio / sounddevice. The other submodules (``_classifier`` /
``_cold`` / ``_warm``) are pure-Python diagnosis logic; keeping the
audio I/O concentrated here makes the probe testable with a fake
``sd_module`` injected at the boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import (
    record_probe_result,
    record_start_time_error,
)
from sovyx.voice.health.contract import (
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)
from sovyx.voice.health.probe._classifier import (
    _WARMUP_DISCARD_MS,
)
from sovyx.voice.health.probe._cold import (
    _classify_open_error,
    _diagnose_cold,
)
from sovyx.voice.health.probe._warm import (
    _analyse_rms,
    _analyse_vad,
    _diagnose_warm,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy.typing as npt

    from sovyx.voice._frame_normalizer import FrameNormalizer
    from sovyx.voice.vad import SileroVAD

logger = get_logger(__name__)


# ── Probe duration / timeout defaults ─────────────────────────────


from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning  # noqa: E402

_DEFAULT_COLD_DURATION_MS = _VoiceTuning().probe_cold_duration_ms
"""Cold probe target duration. ADR §4.3."""

_DEFAULT_WARM_DURATION_MS = _VoiceTuning().probe_warm_duration_ms
"""Warm probe target duration. ADR §4.3."""

_HARD_TIMEOUT_S = _VoiceTuning().probe_hard_timeout_s
"""Hard wall-clock ceiling per probe (ADR §4.3)."""

# ``_WARMUP_DISCARD_MS`` is re-exported for back-compat — some
# external callers may import it via ``sovyx.voice.health.probe``.
_ = _WARMUP_DISCARD_MS


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


# ── Public entry point ────────────────────────────────────────────


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
    # T6.6 — track the last-callback timestamp so the diagnose phase
    # can detect a heartbeat that started but went silent mid-probe.
    # ``None`` until the first callback fires; transitions to a
    # ``time.monotonic()`` snapshot inside the callback. Read after
    # the probe duration to compute silence_since_last_callback_ms.
    last_callback_monotonic: float | None = None

    def _callback(
        indata: npt.NDArray[Any],
        _frames: int,
        _time_info: object,
        _status: object,
    ) -> None:
        nonlocal callbacks_fired, last_callback_monotonic
        callbacks_fired += 1
        last_callback_monotonic = time.monotonic()
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
        # T6.5 — pass combo so a rate-only error with auto_convert=False
        # routes to INVALID_SAMPLE_RATE_NO_AUTO_CONVERT instead of the
        # broader FORMAT_MISMATCH diagnosis.
        diagnosis = _classify_open_error(exc, combo=combo)
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
        # T6.5 — pass combo for the same rate-vs-auto_convert routing.
        # T6.8 — context="start" routes permission keywords to
        # PERMISSION_REVOKED_RUNTIME instead of PERMISSION_DENIED.
        # The open already succeeded (we got here past _open_input_stream),
        # so a permission error during stream.start() means the OS
        # revoked permission between open and start.
        diagnosis = _classify_open_error(
            start_time_error,
            combo=combo,
            context="start",
        )
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

    wall_end = time.monotonic()
    elapsed_ms = int((wall_end - wall_start) * 1000)
    # T6.6 — silence-since-last-callback in ms. Computed once here
    # from the monotonic snapshot the callback set + the probe-end
    # timestamp; passed to both _diagnose_cold and _diagnose_warm
    # so the heartbeat-silence branch fires consistently across
    # modes. ``None`` when callbacks_fired == 0 (no callback ever
    # set the snapshot — caller path already handles that via the
    # zero-callback gate).
    silence_after_last_callback_ms: int | None = (
        int((wall_end - last_callback_monotonic) * 1000)
        if last_callback_monotonic is not None
        else None
    )

    with blocks_lock:
        collected = list(blocks)

    rms_db = _analyse_rms(collected, combo)

    if mode is ProbeMode.COLD:
        diagnosis = _diagnose_cold(
            callbacks_fired=callbacks_fired,
            rms_db=rms_db,
            combo=combo,
            elapsed_ms=elapsed_ms,
            silence_after_last_callback_ms=silence_after_last_callback_ms,
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
        elapsed_ms=elapsed_ms,
        silence_after_last_callback_ms=silence_after_last_callback_ms,
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


# ── Stream lifecycle helpers ──────────────────────────────────────


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


def _combo_tag(combo: Combo) -> str:
    """Short string for log events (avoid full Combo repr in tight loops)."""
    return (
        f"{combo.host_api}|{combo.sample_rate}|{combo.channels}|"
        f"{combo.sample_format}|excl={combo.exclusive}"
    )


__all__ = [
    "InputStreamLike",
    "SoundDeviceModule",
    "_DEFAULT_COLD_DURATION_MS",
    "_DEFAULT_WARM_DURATION_MS",
    "_FORMAT_TO_SD_DTYPE",
    "_HARD_TIMEOUT_S",
    "_build_probe_wasapi_settings",
    "_combo_tag",
    "_load_sounddevice",
    "_open_input_stream",
    "_run_probe",
    "probe",
]
