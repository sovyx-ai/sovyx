"""Voice pipeline factory -- instantiate all components for hot-enable.

Creates SileroVAD, MoonshineSTT, TTS (Piper or Kokoro fallback),
WakeWordDetector, VoicePipeline, and the AudioCaptureTask that feeds
the pipeline in a single async call. All ONNX loads wrapped in
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.model_registry import (
    detect_tts_engine,
    ensure_kokoro_tts,
    ensure_silero_vad,
    get_default_model_dir,
)

_self = sys.modules[__name__]

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.engine.events import EventBus
    from sovyx.voice._capture_task import AudioCaptureTask
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

logger = get_logger(__name__)


class VoiceFactoryError(Exception):
    """Raised when voice pipeline components can't be created."""

    def __init__(self, message: str, missing_models: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.missing_models = missing_models or []


@dataclass(frozen=True)
class VoiceBundle:
    """Result of :func:`create_voice_pipeline`.

    Callers own both objects — the pipeline must be registered in the
    service registry and the capture task must be started to actually
    listen to the microphone.
    """

    pipeline: VoicePipeline
    capture_task: AudioCaptureTask


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

    # ── 1. SileroVAD (auto-download) ──────────────────────────
    logger.info("voice_factory_creating_vad")
    vad_path = await ensure_silero_vad(models_dir)
    vad = await asyncio.to_thread(lambda: _self._create_vad(vad_path))

    # ── 2. MoonshineSTT (auto-download via HF Hub) ───────────
    logger.info("voice_factory_creating_stt", language=language)
    stt = _self._create_stt(language)
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
        tts = await asyncio.to_thread(lambda: _self._create_piper_tts(models_dir))
    elif tts_engine == "kokoro":
        await ensure_kokoro_tts(models_dir)
        tts = await asyncio.to_thread(
            lambda: _self._create_kokoro_tts(models_dir, voice_id=voice_id, language=language),
        )
    else:
        msg = "No TTS engine available. Install piper-tts or kokoro-onnx."
        raise VoiceFactoryError(
            msg,
            missing_models=[
                {"name": "piper-tts or kokoro-onnx", "install_command": "pip install piper-tts"},
            ],
        )

    # ── 4. WakeWord (optional — skip if model absent) ────────
    wake = await asyncio.to_thread(_self._create_wake_word_stub)

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

    # The bypass callback needs to call ``capture_task.request_exclusive_restart()``
    # but capture_task is built after the pipeline. Use a one-slot holder
    # that the closure reads at invocation time (late binding) rather than
    # a setter API on VoicePipeline — keeps the public surface small and
    # the dependency direction clean (pipeline does not know about
    # AudioCaptureTask type).
    capture_holder: dict[str, AudioCaptureTask] = {}

    async def _bypass_callback() -> None:
        task = capture_holder.get("task")
        if task is None:
            logger.debug("voice_apo_bypass_callback_no_capture_task")
            return
        result = await task.request_exclusive_restart()
        # v0.20.2 / Bug C — surface the verdict so auto-bypass is not
        # silently considered "done" when WASAPI downgraded to shared.
        if not result.engaged:
            logger.warning(
                "voice_apo_bypass_not_engaged",
                verdict=result.verdict.value,
                host_api=result.host_api,
                device=result.device,
                detail=result.detail,
            )

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
        on_capture_bypass_requested=_bypass_callback,
        voice_clarity_active=voice_clarity_active,
        auto_bypass_enabled=tuning.voice_clarity_autofix,
        auto_bypass_threshold=tuning.deaf_warnings_before_exclusive_retry,
        self_feedback_gate=self_feedback_gate,
    )

    capture_task = AudioCaptureTask(
        pipeline,
        input_device=effective_index,
        host_api_name=effective_host_api,
    )
    capture_holder["task"] = capture_task

    await pipeline.start()

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
    )
    _emit_capture_apo_detection(resolved_name=resolved.name if resolved is not None else None)
    return VoiceBundle(pipeline=pipeline, capture_task=capture_task)


async def _run_vchl_boot_cascade(
    *,
    resolved: DeviceEntry | None,
    data_dir: Path | None,
    tuning: VoiceTuningConfig,
    allow_inoperative_capture: bool = False,
) -> DeviceEntry | None:
    """Run the VCHL cold cascade once at boot to populate :class:`ComboStore`.

    ADR §5.11 + §4.4.7. The cascade is a migration side-effect (populates
    :class:`ComboStore`) *and* a fail-over signal: when the cold cascade
    diagnoses the endpoint as :attr:`Diagnosis.KERNEL_INVALIDATED`, the
    cascade short-circuits with ``source="quarantined"`` and we fail-over
    to the next-best capture endpoint via
    :func:`select_alternative_endpoint`, then re-run the cascade once
    against the alternative.

    v0.20.2 / Bug D — on the FINAL cascade result (after any §4.4.7
    fail-over re-run) the helper classifies the outcome via
    :func:`classify_cascade_boot_result`:

    * ``HEALTHY`` / ``DEGRADED`` → return the driving :class:`DeviceEntry`.
    * ``INOPERATIVE`` → raise :class:`CaptureInoperativeError` so the
      factory never constructs an :class:`AudioCaptureTask` that would
      then fall through to MME shared and boot silently deaf. The
      ``allow_inoperative_capture`` escape hatch keeps the legacy path
      available for tests and for operators who explicitly want the
      pre-v0.20.2 behaviour.

    Returns the :class:`DeviceEntry` that should drive the pipeline — the
    same one passed in for the happy path, or the fail-over device when
    the original was quarantined. Returns ``None`` only when ``resolved``
    was ``None`` to begin with (headless CI, broken audio stack) so the
    legacy opener surfaces the error path naturally.

    Silent on any unexpected failure — a corrupt :class:`ComboStore`,
    a transient ``OSError`` on ``data_dir``, or a probe-side bug must
    never block the voice pipeline from starting. The
    :class:`CaptureInoperativeError` path is the ONE exception: it is an
    intentional, structured signal that no viable microphone exists, and
    must propagate unless explicitly suppressed.
    """
    if resolved is None:
        logger.debug("voice_boot_cascade_skipped_no_resolved_device")
        return None

    effective_data_dir = data_dir
    if effective_data_dir is None:
        try:
            from sovyx.engine.config import EngineConfig

            effective_data_dir = EngineConfig().data_dir
        except Exception:  # noqa: BLE001 — config failure must not block boot
            logger.debug("voice_boot_cascade_data_dir_unavailable", exc_info=True)
            return resolved

    from sovyx.voice._capture_task import CaptureInoperativeError
    from sovyx.voice.health._factory_integration import (
        CascadeBootVerdict,
        classify_cascade_boot_result,
    )

    driving_device = resolved
    final_result = None
    try:
        from sovyx.voice._apo_detector import detect_capture_apos
        from sovyx.voice.health import current_platform_key
        from sovyx.voice.health._factory_integration import (
            derive_endpoint_guid,
            run_boot_cascade,
            select_alternative_endpoint,
        )
        from sovyx.voice.health._metrics import record_kernel_invalidated_event

        apo_reports = await asyncio.to_thread(detect_capture_apos)
        result = await run_boot_cascade(
            resolved=resolved,
            data_dir=effective_data_dir,
            tuning=tuning,
            apo_reports=apo_reports,
        )
        final_result = result
        # §4.4.7 fail-over — the original endpoint is in kernel-invalidated
        # state; the quarantine entry is already recorded inside the
        # cascade. Pick an alternative endpoint and re-run once.
        if (
            result is not None
            and result.source == "quarantined"
            and tuning.kernel_invalidated_failover_enabled
        ):
            original_guid = derive_endpoint_guid(resolved, apo_reports=apo_reports)
            alternative = select_alternative_endpoint(
                kind="input",
                apo_reports=apo_reports,
                exclude_endpoint_guids=(original_guid,),
            )
            if alternative is None:
                logger.error(
                    "voice_boot_cascade_no_alternative_endpoint",
                    quarantined_endpoint=original_guid,
                    quarantined_friendly_name=resolved.name,
                )
                # final_result retains source="quarantined" → INOPERATIVE
                # verdict below will raise CaptureInoperativeError.
            else:
                logger.warning(
                    "voice_boot_cascade_failover",
                    from_endpoint=original_guid,
                    from_friendly_name=resolved.name,
                    to_friendly_name=alternative.name,
                    to_host_api=alternative.host_api_name,
                )
                record_kernel_invalidated_event(
                    platform=current_platform_key(),
                    host_api=alternative.host_api_name or "unknown",
                    action="failover",
                )
                alt_result = await run_boot_cascade(
                    resolved=alternative,
                    data_dir=effective_data_dir,
                    tuning=tuning,
                    apo_reports=apo_reports,
                )
                driving_device = alternative
                final_result = alt_result
    except Exception:  # noqa: BLE001 — cascade-side faults must never block voice enablement
        logger.warning("voice_boot_cascade_dispatch_failed", exc_info=True)
        return resolved

    outcome = classify_cascade_boot_result(final_result)
    if outcome.verdict is CascadeBootVerdict.INOPERATIVE and not allow_inoperative_capture:
        logger.error(
            "voice_boot_cascade_inoperative",
            reason=outcome.reason,
            attempts=outcome.attempts,
            device=driving_device.index,
            host_api=driving_device.host_api_name,
            friendly_name=driving_device.name,
        )
        msg = (
            f"Voice capture cascade declared endpoint inoperative "
            f"(reason={outcome.reason}, attempts={outcome.attempts}). "
            "No viable microphone path exists; refusing to boot a deaf pipeline."
        )
        raise CaptureInoperativeError(
            msg,
            device=driving_device.index,
            host_api=driving_device.host_api_name,
            reason=outcome.reason,
            attempts=outcome.attempts,
        )
    return driving_device


def _detect_voice_clarity_active(resolved_name: str | None) -> bool:
    """Return True when the active endpoint has a known Voice Clarity APO.

    Small synchronous helper so the async factory can offload the
    registry walk to ``asyncio.to_thread`` without duplicating error
    handling. Never raises — a broken registry read falls back to
    ``False`` (auto-bypass stays opt-in on failure).
    """
    try:
        from sovyx.voice._apo_detector import detect_capture_apos, find_endpoint_report

        report = find_endpoint_report(detect_capture_apos(), device_name=resolved_name)
    except Exception:  # noqa: BLE001 — detector must never break startup
        logger.debug("voice_clarity_detection_failed", exc_info=True)
        return False
    return bool(report is not None and report.voice_clarity_active)


def _emit_capture_apo_detection(*, resolved_name: str | None) -> None:
    """Log ``voice_apo_detected`` once per pipeline boot.

    Non-fatal and best-effort: if the registry walk fails for any
    reason, we swallow the error so a misconfigured Windows install
    never blocks pipeline startup. On non-Windows platforms the
    detector returns an empty list and this function is a no-op.
    """
    from sovyx.voice._apo_detector import detect_capture_apos, find_endpoint_report

    try:
        reports = detect_capture_apos()
    except Exception:  # noqa: BLE001 — detector must never break startup
        logger.debug("voice_apo_detection_failed", exc_info=True)
        return

    active = find_endpoint_report(reports, device_name=resolved_name)
    if active is not None:
        logger.info(
            "voice_apo_detected",
            endpoint=active.endpoint_name,
            enumerator=active.enumerator,
            known_apos=active.known_apos,
            fx_binding_count=active.fx_binding_count,
            voice_clarity_active=active.voice_clarity_active,
        )
    elif reports:
        logger.debug(
            "voice_apo_detected_no_match",
            endpoint_count=len(reports),
            resolved_name=resolved_name,
        )


# ── Component factories (sync — called via to_thread) ────────────────


def _create_vad(model_path: Path) -> Any:  # noqa: ANN401
    from sovyx.voice.vad import SileroVAD

    return SileroVAD(model_path=model_path)


def _create_stt(language: str) -> Any:  # noqa: ANN401, ARG001
    """Construct an uninitialized :class:`MoonshineSTT`.

    The factory calls ``await engine.initialize()`` right after; the
    split exists so :func:`create_voice_pipeline` can keep the sync
    construction trivially test-patchable while the async model load
    stays on the factory's control-flow path.
    """
    from sovyx.voice.stt import MoonshineSTT

    return MoonshineSTT()


def _create_piper_tts(model_dir: Path) -> Any:  # noqa: ANN401
    from sovyx.voice.tts_piper import PiperTTS

    return PiperTTS(model_dir=model_dir / "piper")


def _create_kokoro_tts(
    model_dir: Path,
    *,
    voice_id: str = "",
    language: str = "en",
) -> Any:  # noqa: ANN401
    """Instantiate :class:`KokoroTTS` with a catalog-resolved voice.

    Resolution order:

    1. If ``voice_id`` names a catalog entry, use that voice and trust
       its declared language (voice-prefix wins — a ``pf_dora`` voice
       stays pt-br even if the caller typoed ``language="en"``).
    2. Otherwise, canonicalise ``language`` and pick the recommended
       voice for it from the catalog.
    3. If the language is unsupported, fall back to the hardcoded
       :class:`KokoroConfig` default (``af_bella`` / ``en-us``) — keeps
       the pipeline bootable on exotic languages the catalog doesn't
       cover yet.
    """
    from sovyx.voice import voice_catalog
    from sovyx.voice.tts_kokoro import KokoroConfig, KokoroTTS

    resolved_voice: str | None = None
    resolved_language: str | None = None

    if voice_id:
        info = voice_catalog.voice_info(voice_id)
        if info is not None:
            resolved_voice = info.id
            resolved_language = info.language
        else:
            # A voice_id that isn't in the catalog typically means the
            # catalog was updated without migrating ``mind.yaml`` — surface
            # it so the operator can fix the stale id rather than silently
            # falling back to an English default.
            logger.warning(
                "kokoro_voice_id_not_in_catalog",
                voice_id=voice_id,
                fallback_language=language,
            )

    if resolved_voice is None:
        canonical = voice_catalog.normalize_language(language)
        recommended = voice_catalog.recommended_voice(canonical)
        if recommended is not None:
            resolved_voice = recommended.id
            resolved_language = recommended.language

    if resolved_voice is not None and resolved_language is not None:
        config = KokoroConfig(voice=resolved_voice, language=resolved_language)
        return KokoroTTS(model_dir=model_dir / "kokoro", config=config)

    logger.warning(
        "kokoro_language_not_in_catalog",
        language=language,
        reason="using KokoroTTS hardcoded defaults",
    )
    return KokoroTTS(model_dir=model_dir / "kokoro")


def _create_wake_word_stub() -> Any:  # noqa: ANN401
    """Create a no-op wake word detector.

    The pipeline skips ``wake_word.process_frame`` when
    ``wake_word_enabled=False``, so this stub is never called at runtime.
    It exists only to satisfy the VoicePipeline constructor signature.
    """

    class _NoOpWakeWord:
        def process_frame(self, audio: Any) -> Any:  # noqa: ANN401
            class _Event:
                detected = False

            return _Event()

    return _NoOpWakeWord()
