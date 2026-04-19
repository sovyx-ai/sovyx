"""Unified PortAudio stream opener with host-API × rate × channels × auto_convert fallback.

This module is the single source of truth for opening
``sounddevice.InputStream`` / ``sounddevice.OutputStream`` and for
``sounddevice.play`` in sovyx. Historically each caller (production
capture task, setup-wizard meter, wizard TTS test) grew its own retry
logic — diverging shapes, partial coverage, duplicated bugs. See
:mod:`sovyx.voice.device_enum` for the root-cause writeup of the silent
microphone class of bugs on Windows.

The opener implements a pyramid of open attempts:

1. **Host API**: start with the passed :class:`DeviceEntry`, then walk
   its sibling variants sorted by :attr:`VoiceTuningConfig.capture_fallback_host_apis`.
   On Windows that means ``WASAPI → DirectSound → WDM-KS → MME``.
2. **auto_convert (WASAPI only)**: try with ``WasapiSettings(auto_convert=True)``
   first — that lets the WASAPI layer resample + rechannel + retype
   transparently so mismatches with the shared-mode mixer format never
   reach PortAudio. Fall back to ``auto_convert=False`` if the flag
   itself is rejected (older PortAudio builds).
3. **Channels**: try ``1`` (sovyx' default — VAD/STT expect mono), then
   ``device.max_input_channels`` when :attr:`VoiceTuningConfig.capture_allow_channel_upgrade`
   is on. The callback mixes down to mono client-side when channels > 1.
4. **Sample rate**: try the caller's ``target_rate`` first, then the
   device's ``default_samplerate``. Subsequent pipeline stages resample
   as needed; the meter is rate-agnostic.
5. **Post-open validation (optional)**: when ``validate_fn`` is passed,
   the stream is kept open for :attr:`VoiceTuningConfig.capture_validation_seconds`
   and the observed peak RMS must exceed
   :attr:`VoiceTuningConfig.capture_validation_min_rms_db`; otherwise
   the stream is closed and the next pyramid step is tried. This is
   what catches the "opens cleanly but delivers silence" failure mode
   that MME + non-native rates produce on USB headsets.

Every attempt — successful or not — is recorded in :class:`StreamInfo`
/ :class:`StreamOpenError.attempts` so observability layers can surface
exactly which combinations were tried.

Dependency injection
--------------------

``sd_module`` / ``enumerate_fn`` / ``validate_fn`` are explicit params
for tests; production code calls the functions with only the required
arguments and the opener lazy-imports :mod:`sounddevice` and delegates
to :func:`sovyx.voice.device_enum.enumerate_devices`. This keeps CLAUDE.md
anti-pattern #2 (``sys.modules`` stubs) at bay — tests pass a fake
module directly, no global state mutation required.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.device_test._protocol import ErrorCode
from sovyx.voice.device_test._source import (
    AudioSourceError,
    _classify_portaudio_error,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.device_enum import DeviceEntry

logger = get_logger(__name__)


_WASAPI_HOST_API = "Windows WASAPI"


@dataclass(frozen=True, slots=True)
class OpenAttempt:
    """A single ``InputStream``/``OutputStream`` open attempt and its outcome.

    Attributes:
        host_api: Host-API label of the device the attempt targeted.
        device_index: PortAudio device index used.
        sample_rate: Rate passed to the stream constructor.
        channels: Channel count passed to the stream constructor.
        auto_convert: Whether ``WasapiSettings(auto_convert=True)`` was set.
        exclusive: Whether ``WasapiSettings(exclusive=True)`` was set —
            bypasses the Windows audio engine (and therefore every
            system-wide APO such as Voice Clarity, AGC, Voice Isolation).
            Always ``False`` on non-WASAPI host APIs.
        error_code: ``None`` on success, otherwise the classified
            :class:`ErrorCode` for the raised exception.
        error_detail: Raw exception message (best-effort English) or
            silence diagnostic.
    """

    host_api: str
    device_index: int
    sample_rate: int
    channels: int
    auto_convert: bool
    exclusive: bool = False
    error_code: ErrorCode | None = None
    error_detail: str = ""


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """Metadata about the stream that actually opened.

    Emitted by :func:`open_input_stream` so callers (meter, capture
    task, telemetry) all see the *effective* configuration — not the
    originally requested one.

    On ``exclusive_used``: PortAudio's WASAPI implementation does **not**
    silently downgrade an exclusive request to shared mode. If the
    device, the format, or system policy refuses exclusive access,
    ``IAudioClient::Initialize`` returns ``AUDCLNT_E_*`` and the
    sounddevice constructor raises — the opener catches that, records
    the attempt as failed, and tries the next combo. So
    ``exclusive_used=True`` here means PortAudio confirmed exclusive
    engagement at open time; it does not mean "we asked nicely". When
    :func:`_build_wasapi_settings` cannot construct a usable
    :class:`WasapiSettings` (old PortAudio, non-WASAPI host API), the
    value collapses to ``False`` rather than reporting a phantom request.
    """

    host_api: str
    device_index: int
    sample_rate: int
    channels: int
    dtype: str
    auto_convert_used: bool
    exclusive_used: bool = False
    fallback_depth: int = 0
    attempts: tuple[OpenAttempt, ...] = field(default_factory=tuple)


class StreamOpenError(Exception):
    """Raised when every pyramid combination fails to open cleanly.

    ``attempts`` is ordered chronologically so operators can reconstruct
    the exact sequence of (host_api, rate, channels, auto_convert) that
    was tried and which PortAudio error each attempt surfaced.
    """

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        attempts: list[OpenAttempt],
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def open_input_stream(
    *,
    device: DeviceEntry,
    target_rate: int,
    blocksize: int,
    callback: Callable[..., None],
    tuning: VoiceTuningConfig,
    dtype: str = "int16",
    sd_module: Any | None = None,  # noqa: ANN401 — sounddevice module stand-in
    enumerate_fn: Callable[[], list[DeviceEntry]] | None = None,
    validate_fn: Callable[..., Awaitable[float]] | None = None,
) -> tuple[Any, StreamInfo]:
    """Open a PortAudio input stream with host-API × rate × channels fallback.

    Args:
        device: Resolved :class:`DeviceEntry` (call :func:`device_enum.resolve_device`
            at the WebSocket / route edge — this function never takes a
            bare int index).
        target_rate: Preferred sample rate. The opener falls back to
            ``device.default_samplerate`` when this is rejected.
        blocksize: Samples per callback block. Must match downstream
            frame expectations (512 for the pipeline's 16 kHz / 32 ms
            budget).
        callback: PortAudio callback. Called from the audio thread.
            When multichannel fallback fires, the callback still sees
            whatever shape PortAudio delivers — callers are expected to
            mix down (``indata[:, 0]``) if they need mono downstream.
        tuning: :class:`VoiceTuningConfig` — supplies ``capture_wasapi_auto_convert``,
            ``capture_wasapi_exclusive``, ``capture_allow_channel_upgrade``,
            ``capture_fallback_host_apis``.
        dtype: PortAudio dtype string. Defaults to ``"int16"`` (pipeline).
        sd_module: Injected ``sounddevice`` module (tests). ``None`` =
            lazy-import the real module.
        enumerate_fn: Injected device enumerator (tests). ``None`` =
            delegate to :func:`device_enum.enumerate_devices`.
        validate_fn: ``async (stream, device_index) -> peak_rms_dbfs``
            optional post-open validator. When provided and the observed
            peak RMS is below :attr:`VoiceTuningConfig.capture_validation_min_rms_db`,
            the stream is closed and the next pyramid step is tried.

    Returns:
        Tuple of ``(stream, info)``. The stream is already ``start()``-ed.
        Callers own closing it.

    Raises:
        StreamOpenError: Every viable combination failed. ``attempts``
            lists each (host_api, rate, channels, auto_convert, code,
            detail) tuple in chronological order.
    """
    sd = sd_module if sd_module is not None else _import_sounddevice()
    chain = _device_chain(device, enumerate_fn=enumerate_fn, kind="input")

    attempts: list[OpenAttempt] = []
    for depth, entry in enumerate(chain):
        combos = _input_combos(entry=entry, target_rate=target_rate, tuning=tuning)
        for combo in combos:
            stream, attempt = await _try_open_input(
                sd=sd,
                entry=entry,
                combo=combo,
                blocksize=blocksize,
                dtype=dtype,
                callback=callback,
            )
            attempts.append(attempt)
            if stream is None:
                continue

            # Post-open validation (silence detection).
            if validate_fn is not None:
                try:
                    peak_db = await validate_fn(stream, device_index=entry.index)
                except Exception as exc:  # noqa: BLE001 — validator misbehaviour
                    await _close_stream_quiet(stream)
                    attempts.append(
                        OpenAttempt(
                            host_api=entry.host_api_name,
                            device_index=entry.index,
                            sample_rate=combo.sample_rate,
                            channels=combo.channels,
                            auto_convert=combo.auto_convert,
                            error_code=ErrorCode.INTERNAL_ERROR,
                            error_detail=f"validator raised: {exc}",
                        ),
                    )
                    continue
                if peak_db < tuning.capture_validation_min_rms_db:
                    await _close_stream_quiet(stream)
                    attempts.append(
                        OpenAttempt(
                            host_api=entry.host_api_name,
                            device_index=entry.index,
                            sample_rate=combo.sample_rate,
                            channels=combo.channels,
                            auto_convert=combo.auto_convert,
                            error_code=ErrorCode.INTERNAL_ERROR,
                            error_detail=(
                                f"silent stream (peak {peak_db:.1f} dBFS "
                                f"< threshold {tuning.capture_validation_min_rms_db:.1f} dBFS)"
                            ),
                        ),
                    )
                    continue

            # The winning attempt is whatever ``_try_open_input`` just appended —
            # its ``auto_convert`` / ``exclusive`` fields are the *effective*
            # values (accounting for WasapiSettings being dropped on older
            # PortAudio builds), not the combo's nominal request.
            winning = attempts[-1]
            info = StreamInfo(
                host_api=entry.host_api_name,
                device_index=entry.index,
                sample_rate=combo.sample_rate,
                channels=combo.channels,
                dtype=dtype,
                auto_convert_used=winning.auto_convert,
                exclusive_used=winning.exclusive,
                fallback_depth=depth if len(attempts) == 1 else len(attempts) - 1,
                attempts=tuple(attempts),
            )
            logger.info(
                "voice_stream_opened",
                host_api=info.host_api,
                device_index=info.device_index,
                sample_rate=info.sample_rate,
                channels=info.channels,
                auto_convert=info.auto_convert_used,
                exclusive=info.exclusive_used,
                fallback_depth=info.fallback_depth,
                total_attempts=len(attempts),
            )
            _emit_exclusive_mode_events(info, attempts)
            _record_attempts(attempts, kind="input")
            return stream, info

    last = attempts[-1] if attempts else None
    code = last.error_code if last and last.error_code else ErrorCode.INTERNAL_ERROR
    detail = (
        last.error_detail
        if last and last.error_detail
        else "No audio device variant could be opened"
    )
    logger.warning(
        "voice_stream_open_failed",
        attempts=len(attempts),
        final_code=code.value,
        detail=detail,
    )
    _record_attempts(attempts, kind="input")
    raise StreamOpenError(code=code, detail=detail, attempts=attempts)


async def play_audio(
    audio: npt.NDArray[np.int16],
    *,
    source_rate: int,
    device: DeviceEntry,
    tuning: VoiceTuningConfig,
    sd_module: Any | None = None,  # noqa: ANN401
    enumerate_fn: Callable[[], list[DeviceEntry]] | None = None,  # noqa: ARG001
) -> float:
    """Play a one-shot int16 clip to ``device`` and return elapsed ms.

    Uses :func:`blocking_write_play` (``sd.OutputStream.write``, blocking
    path) wrapped in :func:`asyncio.to_thread`. The blocking write path
    is threadpool-safe on every Windows host API — unlike ``sd.play``,
    which relies on a callback engine that needs COM on the calling
    thread and therefore fails on WASAPI when invoked from an
    :class:`asyncio.ThreadPoolExecutor` worker. See
    :func:`blocking_write_play` for the root-cause writeup.

    When the target host API is WASAPI and ``capture_wasapi_auto_convert``
    is on, ``WasapiSettings(auto_convert=True)`` is passed via
    ``extra_settings`` so format mismatches with the Windows mixer are
    resolved at the WASAPI layer.

    Args:
        audio: Mono int16 buffer at ``source_rate``.
        source_rate: Sample rate the buffer was synthesised at.
        device: Resolved :class:`DeviceEntry` (output).
        tuning: :class:`VoiceTuningConfig` — supplies WASAPI knobs.
        sd_module: Injected ``sounddevice`` module (tests).
        enumerate_fn: Accepted for signature symmetry; the output path
            does not walk host APIs (wizard TTS test is a one-shot,
            not a long-lived stream).

    Returns:
        Elapsed playback time in milliseconds.

    Raises:
        StreamOpenError: If playback fails after the auto_convert retry.
    """
    if audio.size == 0:
        return 0.0
    sd = sd_module if sd_module is not None else _import_sounddevice()

    start_monotonic = asyncio.get_running_loop().time()
    extra = _maybe_wasapi_settings(sd, device, tuning)
    attempts: list[OpenAttempt] = []

    try:
        await asyncio.to_thread(
            blocking_write_play,
            sd,
            audio,
            source_rate,
            device=device.index,
            extra_settings=extra,
        )
        attempts.append(
            OpenAttempt(
                host_api=device.host_api_name,
                device_index=device.index,
                sample_rate=source_rate,
                channels=1,
                auto_convert=bool(extra),
            ),
        )
        _record_attempts(attempts, kind="output")
        return (asyncio.get_running_loop().time() - start_monotonic) * 1000
    except Exception as exc:  # noqa: BLE001
        classified = _classify_portaudio_error(exc, kind="output")
        attempts.append(
            OpenAttempt(
                host_api=device.host_api_name,
                device_index=device.index,
                sample_rate=source_rate,
                channels=1,
                auto_convert=bool(extra),
                error_code=classified.code,
                error_detail=classified.detail,
            ),
        )
        # Only rate mismatch is recoverable by client-side resample + retry.
        # AUDCLNT_E_* / DEVICE_NOT_FOUND / PERMISSION_DENIED etc. go up.
        if classified.code != ErrorCode.UNSUPPORTED_SAMPLERATE:
            _record_attempts(attempts, kind="output")
            raise StreamOpenError(
                code=classified.code,
                detail=classified.detail,
                attempts=attempts,
            ) from exc
        native_rate = int(device.default_samplerate)
        if native_rate <= 0 or native_rate == source_rate:
            _record_attempts(attempts, kind="output")
            raise StreamOpenError(
                code=classified.code,
                detail=classified.detail,
                attempts=attempts,
            ) from exc
        logger.info(
            "voice_stream_output_resample_fallback",
            device_index=device.index,
            host_api=device.host_api_name,
            requested_rate=source_rate,
            native_rate=native_rate,
            reason=str(exc),
        )

    resampled = await asyncio.to_thread(_resample_int16, audio, source_rate, native_rate)
    try:
        await asyncio.to_thread(
            blocking_write_play,
            sd,
            resampled,
            native_rate,
            device=device.index,
            extra_settings=extra,
        )
    except Exception as retry_exc:  # noqa: BLE001
        retry_classified = _classify_portaudio_error(retry_exc, kind="output")
        attempts.append(
            OpenAttempt(
                host_api=device.host_api_name,
                device_index=device.index,
                sample_rate=native_rate,
                channels=1,
                auto_convert=bool(extra),
                error_code=retry_classified.code,
                error_detail=retry_classified.detail,
            ),
        )
        _record_attempts(attempts, kind="output")
        raise StreamOpenError(
            code=retry_classified.code,
            detail=retry_classified.detail,
            attempts=attempts,
        ) from retry_exc
    attempts.append(
        OpenAttempt(
            host_api=device.host_api_name,
            device_index=device.index,
            sample_rate=native_rate,
            channels=1,
            auto_convert=bool(extra),
        ),
    )
    _record_attempts(attempts, kind="output")
    return (asyncio.get_running_loop().time() - start_monotonic) * 1000


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Combo:
    sample_rate: int
    channels: int
    auto_convert: bool
    exclusive: bool = False


def _import_sounddevice() -> Any:  # noqa: ANN401
    """Lazy-import the real ``sounddevice`` module or raise a typed error."""
    try:
        import sounddevice as sd
    except OSError as exc:
        raise AudioSourceError(
            ErrorCode.INTERNAL_ERROR,
            f"PortAudio unavailable: {exc}",
        ) from exc
    return sd


def _device_chain(
    starting: DeviceEntry,
    *,
    enumerate_fn: Callable[[], list[DeviceEntry]] | None,
    kind: str,
) -> list[DeviceEntry]:
    """Return the ordered list of device variants to try.

    Starts with ``starting``, then its siblings (same canonical_name)
    in host-API preference order. Sort order comes from whichever
    enumeration function the caller passed — we trust that
    :func:`device_enum.enumerate_devices` already encodes the platform
    preference, which the caller can override via
    :attr:`VoiceTuningConfig.capture_fallback_host_apis`.
    """
    if enumerate_fn is None:
        from sovyx.voice.device_enum import enumerate_devices

        entries = enumerate_devices()
    else:
        entries = enumerate_fn()

    canonical = starting.canonical_name

    def _channels(e: DeviceEntry) -> int:
        return e.max_input_channels if kind == "input" else e.max_output_channels

    siblings = [e for e in entries if e.canonical_name == canonical and _channels(e) > 0]
    if not any(s.index == starting.index for s in siblings):
        siblings = [starting, *siblings]
    rest = [s for s in siblings if s.index != starting.index]
    return [starting, *rest]


def _input_combos(
    *,
    entry: DeviceEntry,
    target_rate: int,
    tuning: VoiceTuningConfig,
) -> list[_Combo]:
    """Enumerate (exclusive, auto_convert, channels, rate) combos for a device.

    Ordering rationale — exclusive *first* when the tuning flag is on:
        Exclusive mode bypasses the Windows audio engine entirely,
        sidestepping every system-wide capture APO (Voice Clarity,
        AGC, AEC, Noise Suppression, Voice Isolation, Voice Focus,
        3rd-party packs such as VocaEffectPack). That is the *whole
        point* of the flag — if a user (or the auto-bypass heuristic)
        turned it on, they want the APO chain out of the way. Trying
        shared mode first would silently keep the problematic chain.

        Inside exclusive, we still iterate auto_convert — the kwarg is
        a no-op under exclusive per the PortAudio/sounddevice docs, but
        enumerating both keeps the state-space contiguous with shared
        mode and simplifies the downstream fallback tracker.

        When all exclusive combos fail (device held by another app,
        driver rejects the requested format in exclusive, or Windows
        policy denies exclusive access), the opener iterates into
        shared combos — preserving the pre-existing behaviour as a
        safety net.
    """
    rates = list(dict.fromkeys([target_rate, entry.default_samplerate]))
    rates = [r for r in rates if r > 0]

    channel_opts: list[int] = [1]
    if tuning.capture_allow_channel_upgrade and entry.max_input_channels > 1:
        channel_opts.append(entry.max_input_channels)
    channel_opts = [c for c in channel_opts if 1 <= c <= max(entry.max_input_channels, 1)]

    is_wasapi = entry.host_api_name == _WASAPI_HOST_API
    ac_opts = [True, False] if is_wasapi and tuning.capture_wasapi_auto_convert else [False]
    excl_opts = [True, False] if is_wasapi and tuning.capture_wasapi_exclusive else [False]

    combos: list[_Combo] = []
    for excl in excl_opts:
        for ac in ac_opts:
            for ch in channel_opts:
                for rate in rates:
                    combos.append(
                        _Combo(
                            sample_rate=rate,
                            channels=ch,
                            auto_convert=ac,
                            exclusive=excl,
                        ),
                    )
    return combos


async def _try_open_input(
    *,
    sd: Any,  # noqa: ANN401
    entry: DeviceEntry,
    combo: _Combo,
    blocksize: int,
    dtype: str,
    callback: Callable[..., None],
) -> tuple[Any, OpenAttempt]:
    """Attempt a single ``sd.InputStream`` construction + ``start()``.

    The ``auto_convert`` / ``exclusive`` flags recorded on the returned
    :class:`OpenAttempt` reflect what was *effectively* applied — not
    what the combo asked for. When :func:`_build_wasapi_settings`
    returns ``None`` (old PortAudio rejecting the kwargs, non-WASAPI
    host API, ...) the effective values collapse to ``False`` so the
    fallback tracker doesn't see a phantom exclusive attempt and the
    ``voice_stream_exclusive_mode_opened`` event does not fire when
    exclusive was never actually engaged.
    """
    kwargs: dict[str, Any] = {
        "samplerate": combo.sample_rate,
        "channels": combo.channels,
        "dtype": dtype,
        "blocksize": blocksize,
        "device": entry.index,
        "callback": callback,
    }
    extra = _build_wasapi_settings(sd, entry, combo)
    if extra is not None:
        kwargs["extra_settings"] = extra

    eff_auto_convert = combo.auto_convert if extra is not None else False
    eff_exclusive = combo.exclusive if extra is not None else False

    try:
        stream = await asyncio.to_thread(lambda: sd.InputStream(**kwargs))
        await asyncio.to_thread(stream.start)
    except Exception as exc:  # noqa: BLE001
        classified = _classify_portaudio_error(exc)
        # ``extra_settings`` itself may be rejected on older PortAudio
        # builds with ``TypeError: unexpected keyword argument``. Retry
        # once without it so the outer loop gets a chance to iterate.
        if (
            (combo.auto_convert or combo.exclusive)
            and isinstance(exc, TypeError)
            and "extra_settings" in str(exc)
        ):
            try:
                kwargs.pop("extra_settings", None)
                stream = await asyncio.to_thread(lambda: sd.InputStream(**kwargs))
                await asyncio.to_thread(stream.start)
            except Exception as retry_exc:  # noqa: BLE001
                classified = _classify_portaudio_error(retry_exc)
                return None, OpenAttempt(
                    host_api=entry.host_api_name,
                    device_index=entry.index,
                    sample_rate=combo.sample_rate,
                    channels=combo.channels,
                    auto_convert=False,
                    exclusive=False,
                    error_code=classified.code,
                    error_detail=classified.detail,
                )
            return stream, OpenAttempt(
                host_api=entry.host_api_name,
                device_index=entry.index,
                sample_rate=combo.sample_rate,
                channels=combo.channels,
                auto_convert=False,
                exclusive=False,
            )
        return None, OpenAttempt(
            host_api=entry.host_api_name,
            device_index=entry.index,
            sample_rate=combo.sample_rate,
            channels=combo.channels,
            auto_convert=eff_auto_convert,
            exclusive=eff_exclusive,
            error_code=classified.code,
            error_detail=classified.detail,
        )
    return stream, OpenAttempt(
        host_api=entry.host_api_name,
        device_index=entry.index,
        sample_rate=combo.sample_rate,
        channels=combo.channels,
        auto_convert=eff_auto_convert,
        exclusive=eff_exclusive,
    )


def _build_wasapi_settings(
    sd: Any,  # noqa: ANN401
    entry: DeviceEntry,
    combo: _Combo,
) -> Any | None:  # noqa: ANN401
    """Return a :class:`WasapiSettings` instance or ``None`` when not applicable.

    The platform guard is defensive: ``_input_combos`` already decides
    which flag permutations to try. This function returns ``None`` when
    we either (a) are not on Windows/WASAPI, (b) the sounddevice build
    does not expose :class:`WasapiSettings` (old PortAudio wheels), (c)
    the combo asks for neither auto_convert nor exclusive (WASAPI
    default, no extra settings needed), or (d) the installed
    :class:`WasapiSettings` rejects the ``exclusive`` kwarg (even older
    PortAudio wheels) — the opener falls through to ``combo.exclusive=False``
    variants on its next iteration.
    """
    if entry.host_api_name != _WASAPI_HOST_API:
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


def _maybe_wasapi_settings(
    sd: Any,  # noqa: ANN401
    device: DeviceEntry,
    tuning: VoiceTuningConfig,
) -> Any | None:  # noqa: ANN401
    """Return ``WasapiSettings(auto_convert=True)`` for the output path when eligible."""
    if device.host_api_name != _WASAPI_HOST_API:
        return None
    if not tuning.capture_wasapi_auto_convert:
        return None
    cls = getattr(sd, "WasapiSettings", None)
    if cls is None:
        return None
    try:
        return cls(auto_convert=True)
    except TypeError:
        return None


def blocking_write_play(
    sd: Any,  # noqa: ANN401
    audio: Any,  # noqa: ANN401
    sample_rate: int,
    *,
    device: int | str | None = None,
    extra_settings: Any | None = None,  # noqa: ANN401
) -> None:
    """Play ``audio`` synchronously via ``sd.OutputStream.write`` — threadpool-safe.

    Why not ``sd.play``:
        ``sd.play(..., blocking=True)`` is implemented on top of a
        callback-based :class:`sd.OutputStream`. On Windows + WASAPI,
        PortAudio's callback engine calls into the IAudioClient COM
        interface from the stream-open path; that call requires COM to
        be initialized (``CoInitializeEx``) on the current thread. The
        Python main thread picks up COM incidentally (via the
        ``sounddevice`` import / ``Pa_Initialize`` chain), but
        :func:`asyncio.to_thread` dispatches into a
        :class:`ThreadPoolExecutor` worker that has **no** COM — so
        ``sd.play`` fails with ``PaErrorCode -9999`` (unanticipated host
        error) and ``GLE=0x490`` (``ERROR_NOT_FOUND``), presenting as a
        KS / WDM-KS proposal failure even though the elected host API
        is WASAPI.

        Opening :class:`sd.OutputStream` without a callback and writing
        the buffer via :meth:`OutputStream.write` goes through
        PortAudio's *blocking* WASAPI path, which handles COM
        transitions internally. The blocking path is threadpool-safe
        on MME, DirectSound, WDM-KS, and WASAPI uniformly.

        Rule of thumb: anywhere the sovyx codebase would call
        ``sd.play(...)`` from within :func:`asyncio.to_thread`, use
        this helper instead. See CLAUDE.md anti-pattern #14 for the
        general "sync CPU-bound in async" rule; this helper is a
        Windows-specific corollary.

    Args:
        sd: The ``sounddevice`` module (injected for tests).
        audio: 1-D (mono) or 2-D ``(frames, channels)`` PCM buffer.
            ``dtype`` must be ``int16`` or ``float32``; the stream
            ``dtype`` is inferred from ``audio.dtype``.
        sample_rate: Stream sample rate in Hz.
        device: Target device index, or ``None`` for the default.
        extra_settings: Host-API-specific settings (e.g.
            :class:`sd.WasapiSettings`). Dropped on ``TypeError`` from
            older PortAudio builds that do not expose the kwarg.
    """
    channels = 1 if audio.ndim == 1 else int(audio.shape[1])
    dtype = "int16" if getattr(audio.dtype, "kind", "") == "i" else "float32"
    kwargs: dict[str, Any] = {
        "samplerate": sample_rate,
        "channels": channels,
        "dtype": dtype,
    }
    if device is not None:
        kwargs["device"] = device
    if extra_settings is not None:
        kwargs["extra_settings"] = extra_settings
    try:
        stream = sd.OutputStream(**kwargs)
    except TypeError as exc:
        # Older PortAudio wheels may reject the ``extra_settings`` kwarg.
        # Retry without it so the caller's auto_convert fallback loop
        # still gets a meaningful attempt.
        if extra_settings is None or "extra_settings" not in str(exc):
            raise
        kwargs.pop("extra_settings", None)
        stream = sd.OutputStream(**kwargs)
    try:
        stream.start()
        stream.write(audio)
    finally:
        with contextlib.suppress(Exception):
            stream.stop()
        with contextlib.suppress(Exception):
            stream.close()


async def _close_stream_quiet(stream: Any) -> None:  # noqa: ANN401
    """Stop+close a stream, swallowing driver quirks during teardown."""
    with contextlib.suppress(Exception):
        await asyncio.to_thread(stream.stop)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(stream.close)


def _resample_int16(
    audio: npt.NDArray[np.int16],
    src_rate: int,
    dst_rate: int,
) -> npt.NDArray[np.int16]:
    """Linear-interpolation resample of a mono int16 buffer.

    Used by :func:`play_audio` when a non-WASAPI device rejects the
    synthesis rate. Linear interpolation is adequate for the wizard's
    one-shot test phrase — quality is not critical.
    """
    import numpy as np  # noqa: PLC0415

    if src_rate == dst_rate or audio.size == 0:
        return audio
    src_len = int(audio.size)
    dst_len = max(1, int(round(src_len * dst_rate / src_rate)))
    x_src = np.linspace(0.0, 1.0, num=src_len, endpoint=False, dtype=np.float64)
    x_dst = np.linspace(0.0, 1.0, num=dst_len, endpoint=False, dtype=np.float64)
    resampled = np.interp(x_dst, x_src, audio.astype(np.float32))
    clipped = np.clip(resampled, -32_768, 32_767)
    return clipped.astype(np.int16)


def _emit_exclusive_mode_events(
    info: StreamInfo,
    attempts: list[OpenAttempt],
) -> None:
    """Surface the three exclusive-mode lifecycle signals.

    - ``voice_stream_exclusive_mode_opened``: the stream that actually
      opened is running in WASAPI exclusive mode. Downstream consumers
      (orchestrator, dashboard) use this to confirm the APO chain has
      been bypassed.

    - ``voice_stream_exclusive_mode_fallback``: at least one exclusive
      attempt failed and the opener dropped into shared mode. ``reason``
      carries the classified error code of the *first* exclusive
      failure — that is the actionable signal operators need
      (``device_busy`` vs ``unsupported_format`` vs ``internal_error``).

    - ``voice_exclusive_mode_disabled_by_policy``: a Windows group
      policy / audio-endpoint setting denied exclusive access
      (``AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED``). This is rarer than
      device-busy and deserves its own event so operators don't chase
      a phantom "other app is holding the mic" diagnosis.
    """
    if info.exclusive_used:
        logger.info(
            "voice_stream_exclusive_mode_opened",
            host_api=info.host_api,
            device_index=info.device_index,
            sample_rate=info.sample_rate,
            channels=info.channels,
            auto_convert=info.auto_convert_used,
        )
        return

    first_exclusive_failure: OpenAttempt | None = None
    for attempt in attempts:
        if attempt.exclusive and attempt.error_code is not None:
            first_exclusive_failure = attempt
            break

    if first_exclusive_failure is None:
        return

    code = first_exclusive_failure.error_code
    reason = code.value if code else "unknown"
    detail_lc = (first_exclusive_failure.error_detail or "").lower()
    policy_denied = "exclusive_mode_not_allowed" in detail_lc

    logger.warning(
        "voice_stream_exclusive_mode_fallback",
        host_api=info.host_api,
        device_index=info.device_index,
        reason=reason,
        policy_denied=policy_denied,
        detail=first_exclusive_failure.error_detail,
    )
    if policy_denied:
        logger.warning(
            "voice_exclusive_mode_disabled_by_policy",
            host_api=info.host_api,
            device_index=info.device_index,
            detail=first_exclusive_failure.error_detail,
        )


def _record_attempts(attempts: list[OpenAttempt], *, kind: str) -> None:
    """Emit one ``voice_stream_open_attempts`` counter increment per attempt.

    Labels are low-cardinality on purpose: ``host_api`` + ``auto_convert``
    + ``kind`` + ``result`` + ``error_code`` (``"none"`` on success).
    ``device_index`` / ``sample_rate`` / ``channels`` are deliberately
    excluded — they'd explode cardinality without answering the "which
    combo lands on live hardware?" question we actually have.
    """
    # Lazy import — observability must not pull metrics at module load
    # time (that breaks test isolation in unit suites that build a
    # registry per-test).
    from sovyx.observability.metrics import get_metrics

    registry = get_metrics()
    counter = getattr(registry, "voice_stream_open_attempts", None)
    if counter is None:
        return
    for attempt in attempts:
        if attempt.error_code is None:
            result = "ok"
            code_label = "none"
        elif "silent stream" in (attempt.error_detail or ""):
            result = "silent"
            code_label = attempt.error_code.value
        else:
            result = "error"
            code_label = attempt.error_code.value
        try:
            counter.add(
                1,
                attributes={
                    "kind": kind,
                    "host_api": attempt.host_api,
                    "auto_convert": str(attempt.auto_convert).lower(),
                    "result": result,
                    "error_code": code_label,
                },
            )
        except Exception:  # noqa: BLE001 — never let metrics break the opener
            logger.debug("voice_stream_metric_emit_failed", exc_info=True)


__all__ = [
    "OpenAttempt",
    "StreamInfo",
    "StreamOpenError",
    "open_input_stream",
    "play_audio",
]
