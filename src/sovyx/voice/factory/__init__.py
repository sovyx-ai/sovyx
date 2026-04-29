"""Voice pipeline factory -- instantiate all components for hot-enable.

Creates SileroVAD, MoonshineSTT, TTS (Piper or Kokoro fallback),
WakeWordDetector, VoicePipeline, and the AudioCaptureTask that feeds
the pipeline in a single async call. All ONNX loads wrapped in
``asyncio.to_thread``.

Module layout (split per CLAUDE.md anti-pattern #16 — see
``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T03):

* :mod:`._capture` — platform key + bypass strategy list builder.
* :mod:`._playback` — VAD + Piper / Kokoro TTS instantiators.
* :mod:`._validate` — exception types, preflight gates,
  :func:`_run_vchl_boot_cascade`, STT + wake-word constructors.
* :mod:`._diagnostics` — read-only platform probes (PipeWire / UCM /
  macOS / ETW / audio service watchdog) + Windows + Linux APO emit.

The :func:`create_voice_pipeline` orchestrator + :class:`VoiceBundle`
remain at the package root so the v0.23.x import contract
(``from sovyx.voice.factory import create_voice_pipeline,
VoiceBundle``) and existing test patches at the package level survive
unchanged.

Public-by-history private helpers re-exported for tests / external
callers (legacy back-compat) are listed in ``__all__`` below.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.factory._capture import (
    _build_bypass_strategies,
    _resolve_platform_key,
)
from sovyx.voice.factory._diagnostics import (
    _emit_capture_apo_detection,
    _emit_linux_capture_apo_detection,
    _maybe_log_alsa_ucm_status,
    _maybe_log_macos_diagnostics,
    _maybe_log_pipewire_status,
    _maybe_log_recent_audio_etw_events,
    _maybe_start_audio_service_watchdog,
)
from sovyx.voice.factory._playback import (
    _create_kokoro_tts,
    _create_piper_tts,
    _create_vad,
)
from sovyx.voice.factory._validate import (
    VoiceFactoryError,
    VoicePermissionError,
    _create_stt,
    _create_wake_word_stub,
    _detect_voice_clarity_active,
    _maybe_check_llm_reachable,
    _maybe_check_mic_permission,
    _run_boot_preflight,
    _run_vchl_boot_cascade,
)
from sovyx.voice.model_registry import (
    detect_tts_engine,
    ensure_kokoro_tts,
    ensure_silero_vad,
    get_default_model_dir,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.engine.events import EventBus
    from sovyx.voice._aec import AecProcessor
    from sovyx.voice._capture_task import AudioCaptureTask
    from sovyx.voice._double_talk_detector import DoubleTalkDetector
    from sovyx.voice._noise_suppression import NoiseSuppressor
    from sovyx.voice._render_pcm_buffer import RenderPcmBuffer
    from sovyx.voice.health.contract import BypassOutcome
    from sovyx.voice.pipeline._orchestrator import VoicePipeline


logger = get_logger(__name__)


__all__ = [
    "VoiceBundle",
    "VoiceFactoryError",
    "VoicePermissionError",
    "_build_aec_wiring",
    "_build_double_talk_detector",
    "_build_noise_suppressor",
    "_create_kokoro_tts",
    "_create_piper_tts",
    "_create_stt",
    "_create_vad",
    "_create_wake_word_stub",
    "_detect_voice_clarity_active",
    "_maybe_check_llm_reachable",
    "_maybe_check_mic_permission",
    "_maybe_log_alsa_ucm_status",
    "_maybe_log_macos_diagnostics",
    "_maybe_log_pipewire_status",
    "_maybe_log_recent_audio_etw_events",
    "_maybe_start_audio_service_watchdog",
    "create_voice_pipeline",
    "logger",
]


@dataclass(frozen=True)
class VoiceBundle:
    """Result of :func:`create_voice_pipeline`.

    Callers own both objects — the pipeline must be registered in the
    service registry and the capture task must be started to actually
    listen to the microphone.

    v1.3 §4.6 L6 introduced ``boot_preflight_warnings``: a tuple of
    warning dicts produced by the step 9 ALSA-mixer preflight run
    during boot. The default empty tuple preserves backward
    compatibility for every call site that does not consume the field.
    Dashboard callers pump the warnings into
    :class:`sovyx.voice.health.BootPreflightWarningsStore` registered
    on the :class:`ServiceRegistry`; CLI callers read the
    filesystem-persisted counterpart written in parallel by the
    factory (see :func:`write_preflight_warnings_file`).

    WI2 wire-up: when
    ``VoiceTuningConfig.voice_audio_service_watchdog_enabled`` is
    True (Windows only), the factory instantiates an
    :class:`~sovyx.voice.health._windows_audio_service.AudioServiceWatchdog`
    and starts it before returning. The bundle owns the watchdog
    object so callers MUST call ``await bundle.audio_service_watchdog.stop()``
    on voice-disable to release the polling task. ``None`` when the
    watchdog is disabled, on non-Windows, or when instantiation
    failed (logged as WARN; default-on the non-windows path is the
    "feature not applicable" path, not an error).
    """

    pipeline: VoicePipeline
    capture_task: AudioCaptureTask
    boot_preflight_warnings: tuple[dict[str, object], ...] = field(default_factory=tuple)
    audio_service_watchdog: object = None  # AudioServiceWatchdog | None


def _build_noise_suppressor(
    tuning: VoiceTuningConfig,
) -> NoiseSuppressor | None:
    """Build the NS processor when its tuning flag is on.

    Phase 4 / T4.13 wire-up. Returns ``None`` when
    ``voice_noise_suppression_enabled=False`` (foundation default
    per ``feedback_staged_adoption``) or when the engine is
    explicitly ``"off"`` — in either case the FrameNormalizer's NS
    stage stays in the bit-exact passthrough branch.

    When active, returns a concrete
    :class:`~sovyx.voice._noise_suppression.SpectralGatingSuppressor`
    pinned to the FrameNormalizer's 16 kHz / 512-sample invariants.
    """
    if (
        not tuning.voice_noise_suppression_enabled
        or tuning.voice_noise_suppression_engine == "off"
    ):
        return None

    from sovyx.voice._noise_suppression import build_frame_normalizer_noise_suppressor

    suppressor = build_frame_normalizer_noise_suppressor(
        enabled=True,
        engine=tuning.voice_noise_suppression_engine,
        floor_db=tuning.voice_noise_suppression_floor_db,
        attenuation_db=tuning.voice_noise_suppression_attenuation_db,
    )
    logger.info(
        "voice.ns.wired",
        **{
            "voice.ns.engine": tuning.voice_noise_suppression_engine,
            "voice.ns.floor_db": tuning.voice_noise_suppression_floor_db,
            "voice.ns.attenuation_db": tuning.voice_noise_suppression_attenuation_db,
        },
    )
    return suppressor


def _build_double_talk_detector(
    tuning: VoiceTuningConfig,
) -> DoubleTalkDetector | None:
    """Build the double-talk detector when its tuning flag is on.

    Phase 4 / T4.9 — observability-only foundation. Returns ``None``
    when ``voice_double_talk_detection_enabled=False`` (foundation
    default per ``feedback_staged_adoption``); operators flip after
    pilot validation calibrates the NCC threshold for their hardware.

    Note: the detector only fires from the FrameNormalizer's AEC
    stage, so passing it through has no effect when AEC itself is
    disabled. Callers don't need to gate on
    ``voice_aec_enabled`` — the FrameNormalizer's
    ``_apply_aec_to_window`` only runs when ``self._aec is not None``.
    """
    if not tuning.voice_double_talk_detection_enabled:
        return None

    from sovyx.voice._double_talk_detector import DoubleTalkDetector

    return DoubleTalkDetector(threshold=tuning.voice_double_talk_ncc_threshold)


def _build_aec_wiring(
    tuning: VoiceTuningConfig,
) -> tuple[RenderPcmBuffer | None, AecProcessor | None]:
    """Build the AEC reference buffer + processor from tuning config.

    Phase 4 / T4.4.e — single decision point for whether the
    AEC plumbing is active. Returns a ``(buffer, processor)`` pair
    that the factory threads through both the orchestrator
    (``pipeline.set_render_buffer(buffer)``) and the capture task
    (``AudioCaptureTask(..., aec=processor, render_provider=buffer)``).

    Activation matrix:

    * ``voice_aec_enabled=False`` (foundation default per
      ``feedback_staged_adoption``) → returns ``(None, None)``;
      the FrameNormalizer + AudioOutputQueue stay in the pre-AEC
      passthrough path bit-exactly.
    * ``voice_aec_enabled=True`` AND ``voice_aec_engine != "off"`` →
      constructs a fresh :class:`RenderPcmBuffer` (default 2 s ring)
      and the configured AEC engine via
      :func:`build_frame_normalizer_aec`. The same buffer instance
      registers on both ends — the playback path feeds it (via
      :class:`RenderPcmSink`) and the capture path reads it (via
      :class:`RenderPcmProvider`).
    * ``voice_aec_enabled=True`` AND ``voice_aec_engine="off"`` →
      degenerate config, treated identically to disabled. We avoid
      allocating the buffer when the engine cannot consume it.

    Args:
        tuning: Active :class:`VoiceTuningConfig`. Read fields:
            ``voice_aec_enabled``, ``voice_aec_engine``,
            ``voice_aec_filter_length_ms``.

    Returns:
        ``(render_buffer, aec_processor)``. Both are ``None`` when
        AEC is not active. When active, ``render_buffer`` implements
        both :class:`RenderPcmSink` (write) and
        :class:`RenderPcmProvider` (read) Protocols.
    """
    from sovyx.voice._aec import build_frame_normalizer_aec

    if not tuning.voice_aec_enabled or tuning.voice_aec_engine == "off":
        return None, None

    from sovyx.voice._render_pcm_buffer import RenderPcmBuffer

    render_buffer = RenderPcmBuffer()
    aec_processor = build_frame_normalizer_aec(
        enabled=True,
        engine=tuning.voice_aec_engine,
        filter_length_ms=tuning.voice_aec_filter_length_ms,
    )
    logger.info(
        "voice.aec.wired",
        **{
            "voice.aec.engine": tuning.voice_aec_engine,
            "voice.aec.filter_length_ms": tuning.voice_aec_filter_length_ms,
            "voice.aec.buffer_capacity_samples": render_buffer.capacity_samples,
        },
    )
    return render_buffer, aec_processor


async def create_voice_pipeline(
    *,
    event_bus: EventBus | None = None,
    on_perception: Callable[[str, str], Awaitable[None]] | None = None,
    model_dir: Path | None = None,
    data_dir: Path | None = None,
    language: str = "en",
    voice_id: str = "",
    wake_word_enabled: bool = False,
    mind_id: str = "default",
    input_device: int | str | None = None,
    input_device_name: str | None = None,
    input_device_host_api: str | None = None,
    output_device: int | str | None = None,  # noqa: ARG001 — reserved for future TTS routing
    allow_inoperative_capture: bool = False,
) -> VoiceBundle:
    """Create a fully initialized VoicePipeline with all components.

    All ONNX model loads are wrapped in ``asyncio.to_thread`` to avoid
    blocking the event loop.

    Args:
        event_bus: System event bus for voice events.
        on_perception: Callback when speech is transcribed.
        model_dir: Override model cache directory.
        data_dir: Sovyx data directory used for the VCHL :class:`ComboStore`
            and :class:`CaptureOverrides` files (``<data_dir>/voice/``).
            ``None`` falls back to :attr:`EngineConfig.data_dir`. The
            Sprint 1 boot cascade runs only when this directory resolves
            and the device cleanly enumerated — otherwise the legacy
            opener path drives capture unchanged (ADR §5.11).
        language: STT language code (doubles as the TTS language hint
            when ``voice_id`` is unset — the catalog's recommended voice
            for this language is used).
        voice_id: Kokoro voice id from the catalog (e.g. ``pf_dora``,
            ``af_heart``). When empty, the recommended voice for
            ``language`` is chosen — the catalog is the source of
            truth for the language/voice mapping, so the prefix of the
            resolved voice always matches the spoken language.
        wake_word_enabled: Whether to listen for wake word.
        mind_id: Mind identifier for pipeline config.
        input_device: PortAudio input device index/name for the
            microphone capture task. ``None`` = OS default. Used as the
            legacy/fallback key when ``input_device_name`` is unset.
        input_device_name: Stable device name (e.g. ``"Microfone (Razer
            BlackShark V2 Pro)"``). Preferred over ``input_device``
            because PortAudio indices shift between reboots / USB
            replugs, whereas names do not.
        input_device_host_api: Host API label (``"Windows WASAPI"`` …).
            Used to pick the best variant when the same device is
            exposed by multiple host APIs — see
            :mod:`sovyx.voice.device_enum` for the "MME silent zeros"
            failure mode this guards against.
        output_device: Reserved for TTS playback routing. Persisted
            via ``mind.yaml`` for future use.

    Returns:
        A :class:`VoiceBundle` with the pipeline (already started) and
        the capture task (not yet started — caller starts it after
        registering both in the service registry).

    Raises:
        VoiceFactoryError: If required components can't be created.
    """
    models_dir = model_dir or get_default_model_dir()
    models_dir.mkdir(parents=True, exist_ok=True)

    # ── 0. Preflight gates + observability (band-aid #34 + #28 +
    #      F3 + F4 wire-ups) ─────────────────────────────────
    # Mic + LLM gates default OFF (opt-in for safety). PipeWire +
    # UCM observability default ON (read-only, never mutates state).
    _maybe_check_mic_permission()
    await _maybe_check_llm_reachable()
    _maybe_log_pipewire_status()
    _maybe_log_alsa_ucm_status()
    await _maybe_log_recent_audio_etw_events()
    await _maybe_log_macos_diagnostics()
    # Mission §9.1.1 / Gap 1b — boot-time deprecation surface for the
    # four ``linux_mixer_*_fraction`` knobs scheduled for removal in
    # v0.24.0. A stock install with no overrides emits nothing; an
    # operator who set a non-default value via YAML or env gets ONE
    # structured WARN per non-default knob so they have a full minor-
    # version cycle to migrate to the L2.5 KB-driven preset cascade.
    from sovyx.engine.config import warn_on_deprecated_mixer_overrides

    warn_on_deprecated_mixer_overrides()

    # Ring 1 (Hardware/OS Isolation): capability dispatch + APO bypass +
    # PipeWire/UCM detection + KB profile loader + AGC2 fallback +
    # Windows audio service watchdog + macOS HAL detector. Ring marker
    # fires after the boot-time observability probes so operators get a
    # single structured signal that the OS-isolation layer is initialised.
    logger.info(
        "voice.ring_1.initialized",
        **{"voice.ring": 1, "voice.ring_name": "hardware_os_isolation"},
    )

    # ── 1. SileroVAD (auto-download) ──────────────────────────
    logger.info("voice_factory_creating_vad")
    vad_path = await ensure_silero_vad(models_dir)
    vad = await asyncio.to_thread(lambda: _create_vad(vad_path))
    # Ring 3 (Decision Ensemble): VAD with NaN guard + Schmitt hysteresis +
    # LSTM reset path. The ensemble layer (Silero + future LiveKit EOU)
    # is the third defense ring after capabilities (Ring 1) and signal
    # integrity (Ring 2 — instantiated below alongside the capture task).
    logger.info(
        "voice.ring_3.initialized",
        **{"voice.ring": 3, "voice.ring_name": "decision_ensemble"},
    )

    # ── 2. MoonshineSTT (auto-download via HF Hub) ───────────
    logger.info("voice_factory_creating_stt", language=language)
    stt = _create_stt(language)
    # The constructor only allocates the engine struct; the ONNX session +
    # HF Hub download happen in initialize(). Calling it here guarantees
    # STTState.READY before the pipeline starts consuming speech events —
    # otherwise the first VAD-triggered transcribe() raises
    # ``RuntimeError("STT not initialized")`` and the utterance is lost.
    await stt.initialize()
    if getattr(stt, "state", None) is not None:
        from sovyx.voice.stt import STTState

        if stt.state != STTState.READY:
            msg = (
                "MoonshineSTT.initialize() returned but state is "
                f"{stt.state!r} — expected STTState.READY."
            )
            raise VoiceFactoryError(msg)
    # Ring 4 (Decode Validation): STT with hallucination stoplist +
    # logprob reject + compression-ratio guard + S1/S2 timeout taxonomy.
    logger.info(
        "voice.ring_4.initialized",
        **{"voice.ring": 4, "voice.ring_name": "decode_validation"},
    )

    # ── 3. TTS (Piper > Kokoro > error) ──────────────────────
    tts_engine = detect_tts_engine()
    logger.info("voice_factory_creating_tts", engine=tts_engine)
    if tts_engine == "piper":
        if voice_id:
            # Piper voices are baked into the ONNX model file — a per-call
            # voice_id has no effect, so the wizard's pick silently dies.
            # Log it loudly so operators see the mismatch in telemetry.
            logger.warning(
                "piper_ignores_voice_id",
                voice_id=voice_id,
                reason="piper has fixed voices per model; install kokoro-onnx for catalog voices",
            )
        tts = await asyncio.to_thread(lambda: _create_piper_tts(models_dir))
    elif tts_engine == "kokoro":
        await ensure_kokoro_tts(models_dir)
        tts = await asyncio.to_thread(
            lambda: _create_kokoro_tts(models_dir, voice_id=voice_id, language=language),
        )
    else:
        msg = "No TTS engine available. Install piper-tts or kokoro-onnx."
        raise VoiceFactoryError(
            msg,
            missing_models=[
                {"name": "piper-tts or kokoro-onnx", "install_command": "pip install piper-tts"},
            ],
        )
    # Ring 5 (Output Safety): TTS with atomic cancellation chain +
    # output-energy validation + bounded queue + filler bank. The
    # cancellation chain itself is wired in pipeline._orchestrator;
    # this ring marker fires once the synthesiser is ready.
    logger.info(
        "voice.ring_5.initialized",
        **{
            "voice.ring": 5,
            "voice.ring_name": "output_safety",
            "voice.tts_engine": tts_engine,
        },
    )

    # ── 4. WakeWord (optional — skip if model absent) ────────
    wake = await asyncio.to_thread(_create_wake_word_stub)

    # ── 5. Resolve device + detect capture APOs BEFORE the pipeline ──
    # The detector result (``voice_clarity_active``) is threaded into
    # the orchestrator so the deaf-warning path can decide whether to
    # auto-trigger WASAPI exclusive mode. Resolving the device first
    # lets the detector match by canonical device name.
    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._capture_task import AudioCaptureTask
    from sovyx.voice.device_enum import resolve_device
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

    resolved = await asyncio.to_thread(
        lambda: resolve_device(
            requested_index=input_device,
            requested_name=input_device_name,
            requested_host_api=input_device_host_api,
            kind="input",
        ),
    )
    effective_index: int | str | None = input_device
    effective_host_api: str | None = input_device_host_api
    if resolved is not None:
        effective_index = resolved.index
        effective_host_api = resolved.host_api_name

    tuning = VoiceTuningConfig()

    # ── 5b. VCHL boot cascade (§5.11 migration). Populates the
    # ComboStore on first boot so later boots hit the fast path.
    # Cascade winner is not used to drive AudioCaptureTask this
    # sprint — the legacy opener still owns the capture stream.
    #
    # §4.4.7 — when the cold cascade detects a kernel-invalidated
    # endpoint, the helper picks an alternative ``DeviceEntry`` and
    # re-runs the cascade so the pipeline can boot on a viable mic.
    # The returned ``resolved`` reflects that fail-over.
    #
    # §4.4.7 / Bug D (v0.20.2) — when the final cascade verdict is
    # INOPERATIVE the helper raises :class:`CaptureInoperativeError`
    # before the AudioCaptureTask is constructed, so the caller gets
    # a structured "no viable microphone" error instead of a silently
    # deaf pipeline booted through the legacy MME fallback.
    resolved = await _run_vchl_boot_cascade(
        resolved=resolved,
        data_dir=data_dir,
        tuning=tuning,
        allow_inoperative_capture=allow_inoperative_capture,
    )
    if resolved is not None:
        effective_index = resolved.index
        effective_host_api = resolved.host_api_name

    # §4.4.7 fail-over may have rebound ``resolved`` to a different mic —
    # the VoiceClarity APO detection must target the device the pipeline
    # will actually capture from, otherwise auto-bypass arms on the wrong
    # endpoint and ``voice_pipeline_created`` disagrees with
    # ``voice_apo_detected``.
    voice_clarity_active = await asyncio.to_thread(
        _detect_voice_clarity_active,
        resolved.name if resolved is not None else None,
    )

    # ── 6. Build pipeline with auto-bypass hooks ──────────────
    config = VoicePipelineConfig(
        mind_id=mind_id,
        wake_word_enabled=wake_word_enabled,
    )

    # §4.1 Phase 1 — the deaf-signal callback delegates to the
    # :class:`CaptureIntegrityCoordinator`. Both the capture task
    # (needed for ring-buffer tap) and the coordinator (needed for
    # ``handle_deaf_signal``) are built *after* the pipeline, so we
    # thread them through one-slot holders that the closures resolve
    # at invocation time. This keeps the VoicePipeline API surface
    # small — it only sees a ``Callable[[], Awaitable[list[BypassOutcome]]]``
    # and doesn't need to know about AudioCaptureTask or the
    # coordinator types.
    from sovyx.voice.health.capture_integrity import (
        CaptureIntegrityCoordinator,
        CaptureIntegrityProbe,
    )

    capture_holder: dict[str, AudioCaptureTask] = {}
    coordinator_holder: dict[str, CaptureIntegrityCoordinator] = {}

    async def _on_deaf_signal() -> list[BypassOutcome]:
        coordinator = coordinator_holder.get("coordinator")
        if coordinator is None:
            logger.debug("voice_deaf_signal_callback_no_coordinator")
            return []
        outcomes = await coordinator.handle_deaf_signal()
        return list(outcomes)

    # §4.4.6 self-feedback ducking — build the gate with a late-bound
    # apply_duck closure that targets whichever capture task ends up in
    # the holder. The capture task exposes
    # :meth:`apply_mic_ducking_db` which forwards to its
    # ``FrameNormalizer`` when present. Before the stream opens the
    # normalizer is None and the forward is a no-op — acceptable because
    # ducking is per-TTS-session, not persistent.
    from sovyx.voice.health import SelfFeedbackGate, SelfFeedbackMode

    def _apply_duck(gain_db: float) -> None:
        task = capture_holder.get("task")
        if task is None:
            return
        task.apply_mic_ducking_db(gain_db)

    self_feedback_gate = SelfFeedbackGate(
        mode=SelfFeedbackMode(tuning.self_feedback_isolation_mode),
        apply_duck=_apply_duck,
        duck_gain_db=tuning.self_feedback_duck_gain_db,
        release_ms=tuning.self_feedback_duck_release_ms,
    )

    pipeline = VoicePipeline(
        config=config,
        vad=vad,
        wake_word=wake,
        stt=stt,
        tts=tts,
        event_bus=event_bus,
        on_perception=on_perception,
        on_deaf_signal=_on_deaf_signal,
        voice_clarity_active=voice_clarity_active,
        auto_bypass_enabled=tuning.voice_clarity_autofix,
        auto_bypass_threshold=tuning.deaf_warnings_before_exclusive_retry,
        self_feedback_gate=self_feedback_gate,
    )

    # Derive the endpoint GUID up-front so the coordinator + bypass
    # strategies have a stable identifier from the first probe onward.
    # ``AudioCaptureTask._ensure_endpoint_guid`` would otherwise populate
    # it lazily at :meth:`start`, but the coordinator can be invoked
    # *before* ``start()`` finishes (orchestrator queues the deaf signal
    # on the first zero-VAD heartbeat), so we bind the GUID here. The
    # value is ``None`` only when ``resolved`` itself is ``None`` (pre-
    # cascade fallback, headless CI).
    resolved_endpoint_guid: str | None = None
    if resolved is not None:
        from sovyx.voice.health._factory_integration import derive_endpoint_guid

        try:
            resolved_endpoint_guid = derive_endpoint_guid(resolved)
        except Exception:  # noqa: BLE001 — GUID derivation must never block boot
            logger.debug("voice_factory_endpoint_guid_derivation_failed", exc_info=True)
            resolved_endpoint_guid = None

    # Phase 4 / T4.4.e — AEC wiring. The helper returns ``(None, None)``
    # when ``voice_aec_enabled=False`` (foundation default per
    # ``feedback_staged_adoption``) so the existing pre-AEC contract is
    # preserved bit-exactly. When enabled, the same RenderPcmBuffer
    # instance bridges the playback path (``set_render_buffer``) and the
    # capture path (``AudioCaptureTask.render_provider``).
    render_buffer, aec_processor = _build_aec_wiring(tuning)
    if render_buffer is not None:
        pipeline.set_render_buffer(render_buffer)
    double_talk_detector = _build_double_talk_detector(tuning)
    noise_suppressor = _build_noise_suppressor(tuning)

    capture_task = AudioCaptureTask(
        pipeline,
        input_device=effective_index,
        host_api_name=effective_host_api,
        endpoint_guid=resolved_endpoint_guid,
        aec=aec_processor,
        render_provider=render_buffer,
        double_talk_detector=double_talk_detector,
        noise_suppressor=noise_suppressor,
    )
    capture_holder["task"] = capture_task
    # Ring 2 (Signal Integrity): RMS-floor watchdog + format-detection
    # probe + saturation feedback + phase-inversion detector + AGC2
    # post-process. The capture task owns the FrameNormalizer that
    # implements every Ring 2 invariant; this marker fires once the
    # task is constructed (the ring is "ready" — the stream opens at
    # capture_task.start() which the caller invokes after registry).
    logger.info(
        "voice.ring_2.initialized",
        **{
            "voice.ring": 2,
            "voice.ring_name": "signal_integrity",
            "voice.endpoint_guid": resolved_endpoint_guid or "",
            "voice.host_api": effective_host_api or "unknown",
        },
    )

    # Build the CaptureIntegrityCoordinator now that ``capture_task``
    # exists. The probe requires its *own* SileroVAD instance — sharing
    # the pipeline's VAD would cross-contaminate LSTM state between
    # live-frame processing and probe inference (cf. CLAUDE.md anti-
    # pattern #14 / §4.1). Strategies are platform-filtered: Phase 1
    # ships only ``WindowsWASAPIExclusiveBypass`` on Windows; Linux +
    # macOS coordinators start empty and the coordinator simply
    # quarantines the endpoint on exhaustion (factory fails over).
    probe_vad = await asyncio.to_thread(lambda: _create_vad(vad_path))
    probe = CaptureIntegrityProbe(vad=probe_vad, tuning=tuning)
    platform_key = _resolve_platform_key()
    strategies = _build_bypass_strategies(platform_key)
    coordinator = CaptureIntegrityCoordinator(
        probe=probe,
        strategies=strategies,
        capture_task=capture_task,
        platform_key=platform_key,
        tuning=tuning,
    )
    coordinator_holder["coordinator"] = coordinator

    await pipeline.start()
    # Ring 6 (Orchestration & Observability): state machine + atomic
    # cancellation chain + per-utterance trace ID + RED+USE metrics +
    # consent ledger + dwell watchdog. Ring marker fires once the
    # pipeline is started (state machine seeded, locks initialised,
    # observers ready to record transitions).
    logger.info(
        "voice.ring_6.initialized",
        **{
            "voice.ring": 6,
            "voice.ring_name": "orchestration_observability",
            "voice.mind_id": mind_id,
            "voice.platform_key": platform_key,
        },
    )

    logger.info(
        "voice_pipeline_created",
        stt="moonshine",
        tts=tts_engine,
        vad="silero-v5",
        mind_id=mind_id,
        input_device=effective_index if effective_index is not None else "default",
        host_api=effective_host_api or "unknown",
        voice_clarity_active=voice_clarity_active,
        auto_bypass_enabled=tuning.voice_clarity_autofix,
        platform_key=platform_key,
        bypass_strategies=[s.name for s in strategies],
    )
    _emit_capture_apo_detection(resolved_name=resolved.name if resolved is not None else None)
    _emit_linux_capture_apo_detection(
        resolved_name=resolved.name if resolved is not None else None,
    )

    # ── 7. v1.3 §4.6 L6 boot preflight step 9 + §4.8 L7 marker file ──
    #
    # Detection-only: we never auto-remediate the mixer at boot because
    # the §4.7.4 rationale is explicit — mutating ALSA controls without
    # a user action violates the consent model L0 was already rebated
    # for. The factory emits a warning channel (via store + marker
    # file) and leaves remediation to the user (dashboard button or
    # ``sovyx doctor voice --fix``). Stale-marker handling mirrors
    # v1.3 §-1C #1 alt (e): a passing preflight cleans any marker
    # written by a prior saturated boot.
    boot_warnings = await _run_boot_preflight(tuning=tuning)
    for warning in boot_warnings:
        logger.warning(
            "voice_pipeline_boot_preflight_warning",
            code=warning.get("code"),
            severity=warning.get("severity"),
            hint=warning.get("hint"),
        )

    from sovyx.voice.health import (
        clear_preflight_warnings_file,
        write_preflight_warnings_file,
    )

    if boot_warnings:
        with contextlib.suppress(OSError):
            write_preflight_warnings_file(warnings=boot_warnings, data_dir=data_dir)
        logger.debug(
            "voice.factory.preflight_marker_write_attempted",
            reason="best-effort persistence of preflight warnings",
        )
        logger.info(
            "voice_preflight_marker_written",
            count=len(boot_warnings),
            codes=[w.get("code") for w in boot_warnings],
        )
    else:
        with contextlib.suppress(OSError):
            clear_preflight_warnings_file(data_dir=data_dir)
        logger.debug(
            "voice.factory.preflight_marker_clear_attempted",
            reason="best-effort cleanup of stale preflight marker",
        )
        logger.info(
            "voice_pipeline_boot_preflight_stale_marker_cleared",
            hint="preflight step 9 passed; any prior saturated-boot marker removed",
        )

    audio_service_watchdog = await _maybe_start_audio_service_watchdog()

    return VoiceBundle(
        pipeline=pipeline,
        capture_task=capture_task,
        boot_preflight_warnings=tuple(boot_warnings),
        audio_service_watchdog=audio_service_watchdog,
    )
