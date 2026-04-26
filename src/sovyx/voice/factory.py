"""Voice pipeline factory -- instantiate all components for hot-enable.

Creates SileroVAD, MoonshineSTT, TTS (Piper or Kokoro fallback),
WakeWordDetector, VoicePipeline, and the AudioCaptureTask that feeds
the pipeline in a single async call. All ONNX loads wrapped in
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass, field
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
    from sovyx.voice.health.bypass import PlatformBypassStrategy
    from sovyx.voice.health.contract import BypassOutcome
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

logger = get_logger(__name__)


class VoiceFactoryError(Exception):
    """Raised when voice pipeline components can't be created."""

    def __init__(self, message: str, missing_models: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.missing_models = missing_models or []


class VoicePermissionError(VoiceFactoryError):
    """Raised when the OS denies Sovyx microphone access (band-aid
    #34 wire-up).

    Subclasses :class:`VoiceFactoryError` so callers catching the
    base class still handle it; specific catches can use this class
    to surface the OS-specific remediation hint to the dashboard
    without scraping the message string.

    Attributes:
        remediation_hint: Operator-actionable message ready to render
            verbatim in the dashboard error banner. Naming the exact
            Settings path (Win 10/11) or System Preferences pane (macOS)
            so the operator doesn't have to hunt.
        platform_status: Raw verdict from
            :func:`~sovyx.voice.health._mic_permission.check_microphone_permission`
            (``"granted"`` / ``"denied"`` / ``"unknown"``).
    """

    def __init__(
        self,
        message: str,
        *,
        remediation_hint: str = "",
        platform_status: str = "",
    ) -> None:
        super().__init__(message)
        self.remediation_hint = remediation_hint
        self.platform_status = platform_status


# ── Preflight wire-ups (opt-in factory gates) ────────────────────


def _maybe_check_mic_permission() -> None:
    """Band-aid #34 wire-up: probe OS mic permission before pipeline
    creation. Opt-in via
    :attr:`VoiceTuningConfig.voice_check_mic_permission_enabled`.
    Default OFF — operators opt in once they've validated the gate
    on their hardware.

    Raises:
        VoicePermissionError: When the gate is enabled AND the probe
            returns DENIED. Carries the OS-specific remediation hint
            so the dashboard can surface it verbatim.

    Never raises on UNKNOWN — the OS-level probe couldn't decide,
    and the cascade's own deaf-detection covers the residual case.
    Never raises on non-Windows when the gate is enabled — Linux
    has no OS gate, macOS UNKNOWN is the deferred MA2 case.
    """
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_check_mic_permission_enabled:
        return

    try:
        from sovyx.voice.health._mic_permission import (
            MicPermissionStatus,
            check_microphone_permission,
        )

        report = check_microphone_permission()
    except Exception as exc:  # noqa: BLE001 — preflight gate isolation
        # The probe itself crashed (registry permission anomaly,
        # unexpected platform behaviour). Log structured WARN but
        # do NOT block pipeline creation — the gate is best-effort
        # observability, not a hard requirement.
        logger.warning(
            "voice.factory.mic_permission_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return

    if report.status is MicPermissionStatus.DENIED:
        msg = (
            "Microphone access is denied at the OS level — capture would "
            "produce all-zero frames forever. " + report.remediation_hint
        )
        logger.error(
            "voice.factory.mic_permission_denied",
            **{
                "voice.platform_status": report.status.value,
                "voice.machine_value": report.machine_value or "",
                "voice.user_value": report.user_value or "",
                "voice.action_required": report.remediation_hint,
            },
        )
        raise VoicePermissionError(
            msg,
            remediation_hint=report.remediation_hint,
            platform_status=report.status.value,
        )

    # GRANTED + UNKNOWN both proceed. UNKNOWN logs a structured note
    # so dashboards can show "couldn't determine permission state"
    # alongside subsequent capture telemetry.
    if report.status is MicPermissionStatus.UNKNOWN:
        logger.info(
            "voice.factory.mic_permission_unknown",
            **{"voice.notes": list(report.notes)},
        )


async def _maybe_check_llm_reachable(router: object | None = None) -> None:
    """Band-aid #28 wire-up: probe LLM router reachability before
    pipeline creation. Opt-in via
    :attr:`VoiceTuningConfig.voice_check_llm_reachable_enabled`.
    Default OFF.

    Args:
        router: Pre-resolved LLM router. ``None`` = skip the gate
            silently (the factory can't resolve the router itself
            because the registry isn't a global; callers that have
            access to the registry pass the resolved router).

    On FAIL: logs structured WARN but does NOT raise. Reasoning: the
    LLM might be a process that takes a few seconds to come up after
    Sovyx boots (Ollama in particular has a documented warm-up
    window). Blocking the entire voice pipeline on it would surface
    as "voice broken" rather than "LLM not yet ready" — worse UX
    than letting voice come up + degrading gracefully when the LLM
    is queried."""
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_check_llm_reachable_enabled:
        return
    if router is None:
        logger.info(
            "voice.factory.llm_check_skipped_no_router",
            reason="caller did not provide a router; gate skipped",
        )
        return

    try:
        from sovyx.voice.health.preflight import check_llm_reachable

        check = check_llm_reachable(router=router)
        passed, hint, details = await check()
    except Exception as exc:  # noqa: BLE001 — preflight gate isolation
        logger.warning(
            "voice.factory.llm_check_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return

    if not passed:
        # Log WARN — operators see the LLM is not ready but voice
        # still comes up. The next user utterance will hit the
        # router's own retry / failover path.
        logger.warning(
            "voice.factory.llm_unreachable_at_startup",
            **{
                "voice.action_required": hint,
                "voice.details": details,
            },
        )


def _maybe_log_pipewire_status() -> None:
    """F3 wire-up: read-only PipeWire detection on Linux startup.
    Opt-in via
    :attr:`VoiceTuningConfig.voice_pipewire_detection_enabled` (default
    True — pure observability, never mutates state)."""
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_pipewire_detection_enabled:
        return
    try:
        from sovyx.voice.health._pipewire import detect_pipewire

        report = detect_pipewire()
    except Exception as exc:  # noqa: BLE001 — observability only
        logger.warning(
            "voice.factory.pipewire_detection_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    logger.info(
        "voice.factory.pipewire_status",
        **{
            "voice.pipewire_status": report.status.value,
            "voice.pipewire_socket_present": report.socket_present,
            "voice.pipewire_pactl_available": report.pactl_available,
            "voice.pipewire_server_name": report.server_name or "",
            "voice.pipewire_echo_cancel_loaded": report.echo_cancel_loaded,
            "voice.pipewire_modules_count": len(report.modules_loaded),
            "voice.pipewire_notes": list(report.notes),
        },
    )


def _maybe_log_alsa_ucm_status(card_id: str = "0") -> None:
    """F4 wire-up: read-only ALSA UCM detection on Linux startup.
    Opt-in via
    :attr:`VoiceTuningConfig.voice_alsa_ucm_detection_enabled` (default
    True). ``card_id`` defaults to ``"0"`` because most laptops have
    the codec at index 0; future revisions can wire a per-device
    lookup once the cascade has resolved the active card."""
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_alsa_ucm_detection_enabled:
        return
    try:
        from sovyx.voice.health._alsa_ucm import detect_ucm

        report = detect_ucm(card_id)
    except Exception as exc:  # noqa: BLE001 — observability only
        logger.warning(
            "voice.factory.alsa_ucm_detection_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    logger.info(
        "voice.factory.alsa_ucm_status",
        **{
            "voice.ucm_status": report.status.value,
            "voice.ucm_card_id": report.card_id,
            "voice.ucm_alsaucm_available": report.alsaucm_available,
            "voice.ucm_verbs": list(report.verbs),
            "voice.ucm_active_verb": report.active_verb or "",
            "voice.ucm_notes": list(report.notes),
        },
    )


async def _maybe_log_recent_audio_etw_events() -> None:
    """WI1 wire-up (Step 4): query Windows audio ETW operational
    channels at boot and log structured ``voice.windows.etw_events``
    records.

    Opt-in via
    :attr:`VoiceTuningConfig.voice_probe_windows_etw_events_enabled`
    (default OFF — the probe spawns three ``wevtutil.exe``
    subprocesses with 5 s timeouts each, up to 15 s of additional
    cold-boot latency on busy Windows hosts).

    Capability dispatch: gated by
    :data:`Capability.ETW_AUDIO_PROVIDER`. The probe internally
    requires Windows + ``wevtutil`` on PATH. Locked-down enterprise
    images that strip the binary skip cleanly.

    Failure isolation: subprocess / parse failures are absorbed into
    per-channel notes by :func:`query_audio_etw_events` itself; this
    wrapper additionally catches any unexpected exception so a buggy
    probe never blocks pipeline boot.
    """
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_probe_windows_etw_events_enabled:
        return
    from sovyx.voice.health._capabilities import (  # noqa: PLC0415 — local import keeps factory cold-start lean
        Capability,
        get_default_resolver,
    )

    resolver = get_default_resolver()
    if not resolver.has(Capability.ETW_AUDIO_PROVIDER):
        logger.info(
            "voice.factory.etw_probe_skipped_capability_absent",
            **{
                "voice.capability": Capability.ETW_AUDIO_PROVIDER.value,
                "voice.platform": sys.platform,
            },
        )
        return
    try:
        from sovyx.voice.health._windows_etw import query_audio_etw_events

        results = await asyncio.to_thread(query_audio_etw_events)
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.etw_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    for result in results:
        if result.events:
            logger.info(
                "voice.windows.etw_events",
                **{
                    "voice.channel": result.channel,
                    "voice.event_count": len(result.events),
                    "voice.lookback_seconds": result.lookback_seconds,
                    "voice.first_event_provider": result.events[0].provider,
                    "voice.first_event_id": result.events[0].event_id,
                    "voice.first_event_level": result.events[0].level.value,
                },
            )
        if result.notes:
            logger.debug(
                "voice.windows.etw_query_notes",
                **{
                    "voice.channel": result.channel,
                    "voice.notes": list(result.notes),
                },
            )


async def _maybe_start_audio_service_watchdog() -> object | None:
    """WI2 wire-up: instantiate and start the Windows audio-service
    watchdog when opt-in. Returns the watchdog instance (so the
    caller can stop it on voice-disable) or ``None`` when disabled,
    on non-Windows, or when instantiation failed.

    Default OFF via
    :attr:`VoiceTuningConfig.voice_audio_service_watchdog_enabled`.
    Operators opt in when they've observed audio-service-related
    failures and want the rolling 30 s sc.exe poll surfacing service
    state transitions in real time.

    Failure isolation: instantiation / start failures log WARN but
    NEVER prevent pipeline creation — the watchdog is supplementary
    observability, not a hard requirement."""
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_audio_service_watchdog_enabled:
        return None
    # X1 Phase 3 (mission §9.1.2 spirit): capability dispatch replaces
    # the raw ``sys.platform != "win32"`` gate. The
    # :data:`Capability.AUDIOSRV_QUERY` probe validates BOTH that we
    # are on Windows AND that ``sc.exe`` is on PATH — locked-down
    # enterprise images that strip ``sc.exe`` get a clean fall-back
    # (the watchdog skips activation; the legacy no-watchdog path
    # is what every pre-WI2 release shipped, so degradation is safe).
    from sovyx.voice.health._capabilities import (  # noqa: PLC0415 — local import keeps factory cold-start lean
        Capability,
        get_default_resolver,
    )

    resolver = get_default_resolver()
    if not resolver.has(Capability.AUDIOSRV_QUERY):
        # Opt-in but capability absent — log INFO so operators see the
        # mismatch instead of silently failing to instantiate. Includes
        # ``platform`` for parity with the legacy log shape (dashboards
        # already filter on this field).
        logger.info(
            "voice.factory.audio_service_watchdog_skipped_capability_absent",
            **{
                "voice.capability": Capability.AUDIOSRV_QUERY.value,
                "voice.platform": sys.platform,
            },
        )
        return None
    try:
        from sovyx.voice.health._windows_audio_service import AudioServiceWatchdog

        watchdog = AudioServiceWatchdog()
        await watchdog.start()
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.audio_service_watchdog_start_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    logger.info(
        "voice.factory.audio_service_watchdog_started",
        interval_s=watchdog._interval_s,  # noqa: SLF001 — telemetry only
    )
    return watchdog


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


async def _run_boot_preflight(
    *,
    tuning: VoiceTuningConfig,
) -> list[dict[str, object]]:
    """Run the boot-time subset of :mod:`voice.health.preflight` (v1.3 §4.6 L6).

    The factory runs step 9 (Linux ALSA mixer sanity) at every pipeline
    boot so a saturated codec default surfaces before the user ever
    hits "Enable voice" — the v0.21.2 incident established that users
    who already left the dashboard by the time the first deaf
    heartbeat fires never see the diagnostic. Non-Linux hosts pass
    cheaply (the check short-circuits via ``sys.platform``); Linux
    hosts without ``amixer`` also pass (missing tooling is logged but
    not escalated to a boot warning).

    The factory itself is platform-neutral — every step the cold
    cascade touches runs on every OS. We intentionally *do not* run
    the step 1/2/3/4/5/6/7/8 checks here because most of them require
    running subprocess / ONNX sessions that duplicate work the main
    factory is already doing in parallel. Step 9 is the surgical
    addition dossier ``SVX-VOICE-LINUX-20260422`` calls for, and no
    more.

    Returns a list of warning dicts — each includes ``code``,
    ``severity``, ``hint``, and a ``details`` mapping. The
    ``severity`` tag is always ``"warning"`` at this stage; if a
    future check surfaces a hard failure the factory will raise
    instead.
    """
    from sovyx.voice.health import (
        PreflightStepSpec,
        check_linux_mixer_sanity,
        default_step_names,
        run_preflight,
    )

    names = default_step_names()
    step9_name, step9_code = names[9]
    specs = [
        PreflightStepSpec(
            step=9,
            name=step9_name,
            code=step9_code,
            check=check_linux_mixer_sanity(tuning=tuning),
        ),
    ]
    report = await run_preflight(steps=specs, stop_on_first_failure=False)
    warnings: list[dict[str, object]] = []
    for step in report.steps:
        if step.passed:
            continue
        warnings.append(
            {
                "code": step.code.value,
                "severity": "warning",
                "hint": step.hint,
                "details": dict(step.details),
            },
        )
    return warnings


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
    vad = await asyncio.to_thread(lambda: _self._create_vad(vad_path))
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

    capture_task = AudioCaptureTask(
        pipeline,
        input_device=effective_index,
        host_api_name=effective_host_api,
        endpoint_guid=resolved_endpoint_guid,
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
    probe_vad = await asyncio.to_thread(lambda: _self._create_vad(vad_path))
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
        logger.info(
            "voice_preflight_marker_written",
            count=len(boot_warnings),
            codes=[w.get("code") for w in boot_warnings],
        )
    else:
        with contextlib.suppress(OSError):
            clear_preflight_warnings_file(data_dir=data_dir)
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
        import sys as _sys

        from sovyx.voice._apo_detector import detect_capture_apos
        from sovyx.voice.device_enum import enumerate_devices
        from sovyx.voice.health import current_platform_key
        from sovyx.voice.health._candidate_builder import build_capture_candidates
        from sovyx.voice.health._factory_integration import (
            derive_endpoint_guid,
            run_boot_cascade_for_candidates,
            select_alternative_endpoint,
        )
        from sovyx.voice.health._metrics import record_kernel_invalidated_event

        apo_reports = await asyncio.to_thread(detect_capture_apos)
        all_devices = await asyncio.to_thread(enumerate_devices)
        candidates = build_capture_candidates(
            resolved=resolved,
            all_devices=all_devices,
            platform_key=_sys.platform,
            apo_reports=apo_reports,
        )
        result = await run_boot_cascade_for_candidates(
            candidates=candidates,
            data_dir=effective_data_dir,
            tuning=tuning,
        )
        final_result = result
        # When the cascade-candidate-set produced a winner whose
        # device_index differs from ``resolved`` (the user-preferred one
        # was busy and a session-manager virtual won), rebind
        # ``driving_device`` so the :class:`AudioCaptureTask` opens the
        # device that actually passed the probe. The user's ``mind.yaml``
        # is NOT mutated — their preference is preserved as rank-0
        # candidate for future boots (ADR §2.6).
        if result is not None and result.winning_candidate is not None:
            winner = result.winning_candidate
            for dev in all_devices:
                if dev.index == winner.device_index and dev.host_api_name == winner.host_api_name:
                    driving_device = dev
                    break
        # §4.4.7 fail-over — all candidates ended up in kernel-invalidated
        # quarantine. Pick an alternative endpoint outside the current
        # canonical-name family and re-run the candidate-set cascade.
        if (
            result is not None
            and result.source in {"quarantined", "none"}
            and result.winning_combo is None
            and tuning.kernel_invalidated_failover_enabled
        ):
            excluded_guids = tuple(c.endpoint_guid for c in candidates)
            original_guid = derive_endpoint_guid(resolved, apo_reports=apo_reports)
            alternative = select_alternative_endpoint(
                kind="input",
                apo_reports=apo_reports,
                exclude_endpoint_guids=(original_guid, *excluded_guids),
                exclude_physical_device_ids=(resolved.canonical_name,),
            )
            if alternative is None:
                logger.error(
                    "voice_boot_cascade_no_alternative_endpoint",
                    quarantined_endpoint=original_guid,
                    quarantined_friendly_name=resolved.name,
                    tried_candidates=len(candidates),
                )
                # final_result retains source="none"/"quarantined" → INOPERATIVE
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
                alt_candidates = build_capture_candidates(
                    resolved=alternative,
                    all_devices=all_devices,
                    platform_key=_sys.platform,
                    apo_reports=apo_reports,
                )
                alt_result = await run_boot_cascade_for_candidates(
                    candidates=alt_candidates,
                    data_dir=effective_data_dir,
                    tuning=tuning,
                )
                driving_device = alternative
                final_result = alt_result
                if alt_result is not None and alt_result.winning_candidate is not None:
                    winner = alt_result.winning_candidate
                    for dev in all_devices:
                        if (
                            dev.index == winner.device_index
                            and dev.host_api_name == winner.host_api_name
                        ):
                            driving_device = dev
                            break
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

    # Dotted-namespace telemetry (IMPL-OBSERVABILITY-001 §3.6). The
    # scan event always fires once per pipeline boot — even with zero
    # endpoints — so the dashboard knows the detector ran. Per-endpoint
    # detail is folded into voice.endpoints so the timeline can render
    # the full chain without a follow-up RPC.
    voice_clarity_global = any(rep.voice_clarity_active for rep in reports)
    logger.info(
        "audio.apo.scan",
        **{
            "voice.endpoint_count": len(reports),
            "voice.active_endpoint_id": active.endpoint_id if active else None,
            "voice.active_endpoint_name": active.endpoint_name if active else None,
            "voice.resolved_name": resolved_name,
            "voice.voice_clarity_global": voice_clarity_global,
            "voice.endpoints": [
                {
                    "endpoint_id": rep.endpoint_id,
                    "endpoint_name": rep.endpoint_name,
                    "device_interface_name": rep.device_interface_name,
                    "enumerator": rep.enumerator,
                    "fx_binding_count": rep.fx_binding_count,
                    "known_apos": list(rep.known_apos),
                    "raw_clsids": list(rep.raw_clsids),
                    "voice_clarity_active": rep.voice_clarity_active,
                }
                for rep in reports
            ],
        },
    )
    logger.info(
        "audio.apo.voice_clarity_detected",
        **{
            "voice.detected": voice_clarity_global,
            "voice.active_endpoint_detected": bool(active and active.voice_clarity_active),
            "voice.active_endpoint_name": active.endpoint_name if active else None,
        },
    )


def _emit_linux_capture_apo_detection(*, resolved_name: str | None) -> None:
    """Log ``voice_linux_apo_*`` once per pipeline boot on Linux.

    Peer of :func:`_emit_capture_apo_detection` — same contract
    (best-effort, never raises, no-op on non-Linux) but runs the
    PulseAudio / PipeWire subprocess detector instead of the
    Windows MMDevices registry walk. Structured log surface
    mirrors the Windows path so the dashboard can render Linux
    echo-cancel / noise-suppression with the same timeline widget
    it uses for Windows Voice Clarity.

    Three log records emitted:

    * ``voice_linux_apo_detected`` (INFO, user-visible) — fires
      when the detector surfaced at least one named filter
      (module-echo-cancel, rnnoise, etc.). Carries the dominant
      session manager and deduplicated friendly labels.
    * ``audio.apo.scan.linux`` (INFO, dotted-namespace telemetry)
      — always fires so the dashboard knows the scan ran. Payload
      mirrors Windows ``audio.apo.scan`` with a ``voice.platform``
      discriminator so SLO queries can union the two topics.
    * ``audio.apo.echo_cancel_detected`` (INFO) — one bit for
      dashboards that want a single "any mic-chain processing
      active?" signal across Windows + Linux.

    Non-Linux platforms: the detector returns ``[]`` and this
    function emits nothing (the scan event is gated on platform
    to keep Windows telemetry clean of Linux zero-activity noise).
    """
    if sys.platform != "linux":
        return

    from sovyx.voice._apo_detector_linux import detect_capture_apos_linux

    try:
        reports = detect_capture_apos_linux()
    except Exception:  # noqa: BLE001 — detector must never break startup
        logger.debug("voice_linux_apo_detection_failed", exc_info=True)
        return

    # LinuxApoReport is session-wide — at most one report is
    # returned today. The list wrapper mirrors the Windows API so
    # tests and downstream consumers share a shape.
    report = reports[0] if reports else None

    if report is not None and report.known_apos:
        logger.info(
            "voice_linux_apo_detected",
            session_manager=report.session_manager,
            known_apos=report.known_apos,
            echo_cancel_active=report.echo_cancel_active,
            resolved_name=resolved_name,
        )

    echo_cancel_global = bool(report is not None and report.echo_cancel_active)
    logger.info(
        "audio.apo.scan.linux",
        **{
            "voice.platform": "linux",
            "voice.session_manager": report.session_manager if report else "unknown",
            "voice.echo_cancel_global": echo_cancel_global,
            "voice.resolved_name": resolved_name,
            "voice.known_apos": list(report.known_apos) if report else [],
            "voice.raw_entries": list(report.raw_entries) if report else [],
        },
    )
    logger.info(
        "audio.apo.echo_cancel_detected",
        **{
            "voice.detected": echo_cancel_global,
            "voice.platform": "linux",
            "voice.session_manager": report.session_manager if report else "unknown",
        },
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


# ── Phase 1 bypass-strategy wiring ───────────────────────────────────


def _resolve_platform_key() -> str:
    """Normalise ``sys.platform`` into the three-valued Phase 1 bucket.

    Mirrors :func:`sovyx.voice.health.contract._platform_key` + the
    existing :func:`sovyx.voice.health.current_platform_key` helper but
    is scoped to the factory so the strategy list can be selected
    without importing from inside a ``TYPE_CHECKING`` guard. Unknown
    POSIX-likes fall into the ``"linux"`` bucket, which in Phase 1
    means "no bypass strategies installed" (coordinator quarantines on
    exhaustion and the factory fails over to the next endpoint).
    """
    plat = sys.platform
    if plat.startswith("win"):
        return "win32"
    if plat == "darwin":
        return "darwin"
    return "linux"


def _build_bypass_strategies(platform_key: str) -> list[PlatformBypassStrategy]:
    """Return the platform-filtered bypass strategy list.

    Order within a platform is the order the coordinator tries them;
    a strategy whose ``probe_eligibility`` reports
    ``applicable=False`` is skipped without counting toward the
    ``bypass_strategy_max_attempts`` budget.

    * **win32** —
      :class:`~sovyx.voice.health.bypass.WindowsWASAPIExclusiveBypass`.
      The definitive fix for the Windows Voice Clarity /
      ``VocaEffectPack`` regression (CLAUDE.md anti-pattern #21).
    * **linux** —
      :class:`~sovyx.voice.health.bypass.LinuxALSAMixerResetBypass`
      first (mandatory, default-on, non-destructive: mutates the
      ALSA mixer in-place and reverts on teardown), then
      :class:`~sovyx.voice.health.bypass.LinuxPipeWireDirectBypass`
      (opt-in via
      :attr:`VoiceTuningConfig.linux_pipewire_direct_bypass_enabled`;
      tears down the capture stream and reopens against the ALSA
      kernel device, bypassing the session manager). A disabled
      opt-in strategy stays in the list but reports
      ``applicable=False`` so dashboards see the intent without
      paying apply cost.
    * **darwin** — empty until Phase 4 ships
      :class:`MacOSVPIODisable`.
    """
    if platform_key == "win32":
        from sovyx.voice.health.bypass import WindowsWASAPIExclusiveBypass

        return [WindowsWASAPIExclusiveBypass()]
    if platform_key == "linux":
        from sovyx.voice.health.bypass import (
            LinuxALSAMixerResetBypass,
            LinuxPipeWireDirectBypass,
            LinuxSessionManagerEscapeBypass,
        )

        # Strategy order matters: cheapest + most specific first.
        #
        # 1. ``LinuxALSAMixerResetBypass`` — mandatory, default-on.
        #    Resets saturated pre-ADC gain; no stream teardown. Covers
        #    the common "boost control driven to 100% by the desktop"
        #    pathology. First because it is the lowest-cost check.
        # 2. ``LinuxSessionManagerEscapeBypass`` — VLX-003/VLX-004.
        #    Moves the capture from a pinned ``hw:X,Y`` to a session-
        #    manager virtual when another desktop app grabbed the
        #    hardware. Second because the reopen cost is higher than
        #    a mixer-reset but lower than the hw-direct bypass below.
        # 3. ``LinuxPipeWireDirectBypass`` — opt-in.
        #    Inverse direction: goes from session-manager to hw:
        #    direct. Only fires when the user has explicitly set
        #    ``linux_pipewire_direct_bypass_enabled=True`` because
        #    engaging the bypass steals the device from every other
        #    desktop app.
        return [
            LinuxALSAMixerResetBypass(),
            LinuxSessionManagerEscapeBypass(),
            LinuxPipeWireDirectBypass(),
        ]
    return []
