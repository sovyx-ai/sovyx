"""Voice pipeline status helpers for the dashboard.

Provides functions used by ``/api/voice/status`` and ``/api/voice/models``
to expose the current state of the voice pipeline and available models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry
    from sovyx.voice.auto_select import ModelSelection

logger = get_logger(__name__)


# LIVE-2 Phase 3 (P0-1) — voice-subsystem health vocabulary (SSoT).
#
# String-valued + intentionally OPEN: the frontend maps known values to a
# tone and falls back to a neutral rendering for anything it doesn't
# recognise, so a future value can land here without breaking an older
# bundle (avoids the closed-enum drift flagged for composite_severity).
#
# The cardinal rule this vocabulary enforces: registration ALONE never
# yields ``healthy``. A subsystem is set to ``unknown`` the moment it is
# confirmed registered, then refined ONLY by a real readiness signal read
# off the live engine. If the resolve or the signal read fails, it stays
# ``unknown`` — a registered-but-broken engine must never render green.
VOICE_HEALTH_HEALTHY = "healthy"
VOICE_HEALTH_DEGRADED = "degraded"
VOICE_HEALTH_FAILED = "failed"
VOICE_HEALTH_UNAVAILABLE = "unavailable"
VOICE_HEALTH_UNKNOWN = "unknown"
VOICE_HEALTH_VALUES: frozenset[str] = frozenset(
    {
        VOICE_HEALTH_HEALTHY,
        VOICE_HEALTH_DEGRADED,
        VOICE_HEALTH_FAILED,
        VOICE_HEALTH_UNAVAILABLE,
        VOICE_HEALTH_UNKNOWN,
    },
)


def _stt_health_from_state_name(state_name: str) -> str:
    """Map a :class:`sovyx.voice.stt.STTState` name to a health value.

    READY / TRANSCRIBING are usable (healthy); UNINITIALIZED is registered
    but not yet ready (degraded); CLOSED is terminal (failed). An
    unrecognised state name yields ``unknown`` rather than assuming healthy.
    """
    name = state_name.strip().lower()
    if name in ("ready", "transcribing"):
        return VOICE_HEALTH_HEALTHY
    if name == "uninitialized":
        return VOICE_HEALTH_DEGRADED
    if name == "closed":
        return VOICE_HEALTH_FAILED
    return VOICE_HEALTH_UNKNOWN


async def get_voice_status(registry: ServiceRegistry) -> dict[str, Any]:
    """Collect voice pipeline status from the registry.

    Returns a JSON-serializable dict with pipeline state, active models,
    wake word config, and Wyoming connection status.

    Falls back gracefully if services are not registered.
    """
    status: dict[str, Any] = {
        "pipeline": {
            "running": False,
            "state": "not_configured",
            "latency_ms": None,
        },
        "capture": {
            "running": False,
            "input_device": None,
            "host_api": None,
            "sample_rate": None,
            "frames_delivered": 0,
            "last_rms_db": None,
            # Mission H2 §T2.10 (ADR-D15) — platform metadata from the
            # last bypass-coordinator dispatch. None on pristine boots
            # + on every status snapshot before the first dispatch
            # fires; populated lazily as the bypass coordinator emits
            # ``voice.capture_integrity.bypass*`` events. The producer
            # at :mod:`sovyx.voice.pipeline._capture_integrity_emit` is
            # the canonical write site; this snapshot reads the latest
            # observed values via the registry's status_snapshot path.
            "last_bypass_event_platform": None,
            "last_bypass_event_family": None,
        },
        "stt": {
            "engine": None,
            "model": None,
            "state": None,
            # LIVE-2 P0-1 — default "unavailable" (not registered). Refined
            # to a real value below only when the engine is registered.
            "health": VOICE_HEALTH_UNAVAILABLE,
        },
        "tts": {
            "engine": None,
            "model": None,
            "initialized": False,
            "health": VOICE_HEALTH_UNAVAILABLE,
        },
        "wake_word": {
            "enabled": False,
            "phrase": None,
            "health": VOICE_HEALTH_UNAVAILABLE,
        },
        "vad": {
            "enabled": False,
            "health": VOICE_HEALTH_UNAVAILABLE,
        },
        "wyoming": {
            # LIVE-2 P1-10: default not-configured (server not registered).
            # The dashboard hides the card unless ``configured`` is True.
            "configured": False,
            "connected": False,
            "endpoint": None,
        },
        "hardware": {
            "tier": None,
            "ram_mb": None,
        },
        # Mission C3 §T2.8 — surfaces the failover ladder terminal
        # state to the dashboard so the UI can render an actionable
        # banner. Default-empty (degraded=False) on pre-ladder code
        # paths + on healthy pipelines.
        # Mission C4 §T1.5 / §T1.7 — composite-axes + composite-severity
        # fields populated from the cross-axis EngineDegradedStore
        # below. ack_* fields remain None in Phase 1 (Phase 3 ships
        # the operator_acks SQLite persistence + write site).
        "degraded": {
            "degraded": False,
            "reason": None,
            "candidates_tried": 0,
            "candidates_unreachable": [],
            "last_ladder_complete_monotonic": None,
            "composite_axes": [],
            "composite_severity": None,
            "composite_max_severity": None,
            "ack_at_monotonic": None,
            "ack_ttl_sec": None,
            "ack_operator_id": None,
            "last_resurfaced_monotonic": None,
        },
    }

    # Mission C4 §T1.7 + Mission D.1 §D-P0-1 — populate composite axes,
    # composite_severity (count-tier OR hybrid per the
    # composite_severity_by_max knob), AND the additive
    # composite_max_severity field. Best-effort: a store unavailability
    # cannot block the status endpoint (legacy clients depend on it for
    # the non-degraded fields).
    try:
        from sovyx.dashboard.routes.engine_degraded import (
            _compute_composite_severity,
            _compute_composite_severity_hybrid,
            _max_per_axis_severity,
        )
        from sovyx.engine._degraded_store import get_default_degraded_store

        degraded_store = get_default_degraded_store()
        entries = degraded_store.snapshot()
        distinct_axes = sorted({e.axis for e in entries})
        max_per_axis = _max_per_axis_severity(entries)
        status["degraded"]["composite_axes"] = distinct_axes
        status["degraded"]["composite_max_severity"] = max_per_axis

        # Knob lookup is best-effort via the registry's EngineConfig
        # handle when present (D.1-a LENIENT default: False → count-tier
        # path matches pre-D.1 behavior).
        by_max = False
        try:
            from sovyx.engine.config import EngineConfig

            if registry.is_registered(EngineConfig):
                engine_config = await registry.resolve(EngineConfig)
                by_max = bool(
                    engine_config.tuning.dashboard.composite_severity_by_max,
                )
        except Exception:  # noqa: BLE001 — knob lookup must never raise
            logger.debug("c4_voice_status_composite_knob_lookup_failed")

        if by_max:
            status["degraded"]["composite_severity"] = _compute_composite_severity_hybrid(
                len(distinct_axes),
                max_per_axis,
            )
        else:
            status["degraded"]["composite_severity"] = _compute_composite_severity(
                len(distinct_axes),
            )
    except Exception:  # noqa: BLE001 — observability only
        logger.debug("c4_voice_status_composite_axes_failed")

    # Capture (must run, or the pipeline is silent even if "started")
    #
    # ``status_snapshot`` is the same payload the capture heartbeat logs
    # emit — exposing it here lets the dashboard's VU-meter reuse the
    # identical signal the operator sees in logs, so "panel shows audio,
    # logs show silence" divergence is impossible.
    try:
        from sovyx.voice._capture_task import AudioCaptureTask

        if registry.is_registered(AudioCaptureTask):
            capture = await registry.resolve(AudioCaptureTask)
            snapshot = capture.status_snapshot()
            status["capture"].update(snapshot)
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_capture_failed")

    # Pipeline — "running" requires BOTH pipeline started and capture alive.
    # A pipeline with no mic feeding it is silent, and reporting "Running"
    # in that case is a lie (the exact bug that motivated the wizard rewrite).
    try:
        from sovyx.voice.pipeline import VoicePipeline

        if registry.is_registered(VoicePipeline):
            pipeline = await registry.resolve(VoicePipeline)
            status["pipeline"]["running"] = pipeline.is_running and status["capture"]["running"]
            status["pipeline"]["state"] = pipeline.state.name.lower()
            # LIVE-2 P1-3 — surface the most-recent STT-decode latency the
            # orchestrator now persists. ``None`` (the default) until the
            # first utterance completes; the isinstance guard keeps a non-
            # numeric stub (older pipeline / test mock) from leaking.
            last_latency = getattr(pipeline, "last_stt_latency_ms", None)
            if isinstance(last_latency, (int, float)):
                status["pipeline"]["latency_ms"] = round(float(last_latency), 1)
            # Mission C3 §T2.8 — populate the degraded-mode marker from
            # the pipeline's ladder-state flags. Both flags are
            # default-False (anti-pattern #35 sentinel); a pre-ladder
            # pipeline yields ``degraded=False`` unchanged.
            ladder_exhausted = bool(
                getattr(pipeline, "_failover_ladder_exhausted", False),
            )
            if ladder_exhausted:
                status["degraded"]["degraded"] = True
                status["degraded"]["reason"] = "failover_ladder_exhausted"
            # Best-effort read of the underlying RuntimeFailoverState
            # (held by the factory closure scope). The pipeline does
            # not directly own the state, but the most recent values
            # are mirrored onto pipeline-level attributes when the
            # ladder completes.
            unreachable = getattr(
                pipeline,
                "_failover_last_candidates_unreachable",
                None,
            )
            if isinstance(unreachable, list):
                status["degraded"]["candidates_unreachable"] = list(unreachable)
                status["degraded"]["candidates_tried"] = len(unreachable)
            last_complete = getattr(
                pipeline,
                "_failover_last_ladder_complete_monotonic",
                None,
            )
            if isinstance(last_complete, (int, float)) and last_complete > 0.0:
                status["degraded"]["last_ladder_complete_monotonic"] = float(last_complete)
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_pipeline_failed")

    # STT
    try:
        from sovyx.voice.stt import STTEngine

        if registry.is_registered(STTEngine):
            # LIVE-2 P0-1: registered != healthy. Start at "unknown"; a
            # resolve/read failure below leaves it there rather than
            # falling back to the optimistic default.
            status["stt"]["health"] = VOICE_HEALTH_UNKNOWN
            stt = await registry.resolve(STTEngine)  # type: ignore[type-abstract]
            status["stt"]["engine"] = type(stt).__name__
            if hasattr(stt, "config"):
                cfg = stt.config
                # LIVE-2 P1-1: the configured model identity lives on
                # ``MoonshineConfig.model_size`` (tiny/small/medium). The
                # prior ``model_name`` read targeted a field that never
                # existed on any STT config, so ``model`` was always None
                # even with STT fully running.
                status["stt"]["model"] = getattr(cfg, "model_size", None)
            if hasattr(stt, "state"):
                status["stt"]["state"] = stt.state.name.lower()
                status["stt"]["health"] = _stt_health_from_state_name(
                    stt.state.name,
                )
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_stt_failed")

    # TTS
    try:
        from sovyx.voice.tts_piper import TTSEngine

        if registry.is_registered(TTSEngine):
            # LIVE-2 P0-1: registered != healthy (see STT block).
            status["tts"]["health"] = VOICE_HEALTH_UNKNOWN
            tts = await registry.resolve(TTSEngine)  # type: ignore[type-abstract]
            status["tts"]["engine"] = type(tts).__name__
            if hasattr(tts, "config"):
                cfg = tts.config
                # LIVE-2 P1-2: both ``PiperConfig`` and ``KokoroConfig``
                # carry the configured model identity on ``voice`` (e.g.
                # ``en_US-lessac-medium`` / ``af_bella``). The prior
                # ``model_path`` read targeted a field that exists on
                # neither config — only as an init-time local — so
                # ``model`` was always None even with TTS initialized.
                model = getattr(cfg, "voice", None)
                status["tts"]["model"] = str(model) if model is not None else None
            if hasattr(tts, "is_initialized"):
                initialized = bool(tts.is_initialized)
                status["tts"]["initialized"] = initialized
                # An initialized TTS engine is usable; registered-but-not-
                # initialized is present-but-not-ready (degraded), never green.
                status["tts"]["health"] = (
                    VOICE_HEALTH_HEALTHY if initialized else VOICE_HEALTH_DEGRADED
                )
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_tts_failed")

    # Wake Word
    try:
        from sovyx.voice.wake_word import WakeWordDetector

        if registry.is_registered(WakeWordDetector):
            # ``enabled`` is the registration/config fact and is set BEFORE
            # resolve so a resolve failure still reports the subsystem as
            # configured. LIVE-2 P0-1: health starts "unknown" and only
            # becomes healthy from a real signal below — never from the
            # ``enabled`` flag alone.
            status["wake_word"]["enabled"] = True
            status["wake_word"]["health"] = VOICE_HEALTH_UNKNOWN
            ww = await registry.resolve(WakeWordDetector)
            if hasattr(ww, "config"):
                cfg = ww.config
                status["wake_word"]["phrase"] = getattr(cfg, "wake_phrase", None)
            # The wake-word detector exposes no runtime-failure signal
            # (unlike VAD's ``is_session_unrecoverable``). A readable FSM
            # ``state`` confirms a constructed detector — construction
            # loads + validates the ONNX session synchronously, so a
            # registered detector with a live state is usable. We never
            # assert healthy purely from registration: if the state can't
            # be read (or resolve raised above), health stays "unknown".
            if hasattr(ww, "state"):
                status["wake_word"]["health"] = VOICE_HEALTH_HEALTHY
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_wake_word_failed")

    # VAD
    try:
        from sovyx.voice.vad import SileroVAD

        if registry.is_registered(SileroVAD):
            # ``enabled`` is the registration fact (set before resolve, see
            # wake-word block). LIVE-2 P0-1: health starts "unknown" and is
            # only refined to healthy from a real session signal below.
            status["vad"]["enabled"] = True
            status["vad"]["health"] = VOICE_HEALTH_UNKNOWN
            vad = await registry.resolve(SileroVAD)
            # SileroVAD loads + smoke-probes its ONNX session at
            # construction, so a registered instance starts usable. The
            # real runtime-failure signal is ``is_session_unrecoverable``
            # (terminal) with ``corruption_count`` as a soft-degradation
            # gauge — read both rather than assuming health from presence.
            if getattr(vad, "is_session_unrecoverable", False):
                status["vad"]["health"] = VOICE_HEALTH_FAILED
            elif getattr(vad, "corruption_count", 0) > 0:
                status["vad"]["health"] = VOICE_HEALTH_DEGRADED
            else:
                status["vad"]["health"] = VOICE_HEALTH_HEALTHY
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_vad_failed")

    # Wyoming
    try:
        from sovyx.voice.wyoming import SovyxWyomingServer

        if registry.is_registered(SovyxWyomingServer):
            # LIVE-2 P1-10: ``configured`` is the registration fact — the
            # dashboard hides the Wyoming card entirely when it's False so
            # a permanently-"Disconnected" card (the server is never wired
            # in the default daemon) never misleads the operator.
            status["wyoming"]["configured"] = True
            wyoming = await registry.resolve(SovyxWyomingServer)
            # LIVE-2 P1-10 bug 1: the server's liveness property is
            # ``running``, not ``is_running`` — the prior read always
            # returned the False default even on a live server.
            status["wyoming"]["connected"] = bool(getattr(wyoming, "running", False))
            if hasattr(wyoming, "config"):
                cfg = wyoming.config
                # LIVE-2 P1-10 bug 2: ``WyomingConfig`` has no ``endpoint``
                # field — it binds ``host``:``port``. Compose the endpoint
                # from those so the row shows a real address when present.
                host = getattr(cfg, "host", None)
                port = getattr(cfg, "port", None)
                if host is not None and port is not None:
                    status["wyoming"]["endpoint"] = f"{host}:{port}"
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_wyoming_failed")

    # Hardware tier
    try:
        from sovyx.voice.auto_select import VoiceModelAutoSelector

        if registry.is_registered(VoiceModelAutoSelector):
            selector = await registry.resolve(VoiceModelAutoSelector)
            profile = selector.profile
            if profile is not None:
                status["hardware"]["tier"] = profile.tier.name
                status["hardware"]["ram_mb"] = profile.ram_mb
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_hardware_failed")

    # v1.3 §4.6 L6 — boot preflight warnings (always-present list; empty
    # on non-Linux, on Linux without ``amixer``, or when the factory's
    # step 9 check passed). Consumed by the dashboard voice page to
    # render the LinuxMicGainCard banner without a second round-trip to
    # /api/voice/linux-mixer-diagnostics — see the L5a × L6 cell of the
    # plan interaction matrix. ``BootPreflightWarningsStore`` is
    # registered by ``enable_voice``; its absence here (pipeline never
    # enabled, or registry was bypassed in tests) degrades to the empty
    # default rather than surfacing a fake error.
    status["preflight_warnings"] = []
    try:
        from sovyx.voice.health import BootPreflightWarningsStore

        if registry.is_registered(BootPreflightWarningsStore):
            store = await registry.resolve(BootPreflightWarningsStore)
            status["preflight_warnings"] = store.snapshot()
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_preflight_warnings_failed")

    return status


async def get_voice_models(registry: ServiceRegistry) -> dict[str, Any]:
    """List available voice models grouped by role (STT, TTS).

    Uses :class:`~sovyx.voice.auto_select.VoiceModelAutoSelector`
    if registered, otherwise returns the default model matrix.
    """
    from sovyx.voice.auto_select import (
        HardwareProfile,
        HardwareTier,
        VoiceModelAutoSelector,
        select_models,
    )

    result: dict[str, Any] = {
        "detected_tier": None,
        "active": None,
        "available_tiers": {},
    }

    # Detected / active
    try:
        if registry.is_registered(VoiceModelAutoSelector):
            selector = await registry.resolve(VoiceModelAutoSelector)
            profile = selector.profile
            selection = selector.selection
            if profile is not None:
                result["detected_tier"] = profile.tier.name
            if selection is not None:
                result["active"] = _selection_to_dict(selection)
    except Exception:  # noqa: BLE001
        logger.debug("voice_models_active_failed")

    # All tiers
    for tier in HardwareTier:
        fake = HardwareProfile(
            tier=tier,
            ram_mb=8192,
            cpu_cores=4,
            has_gpu=tier in {HardwareTier.DESKTOP_GPU, HardwareTier.CLOUD},
            gpu_vram_mb=8192 if tier in {HardwareTier.DESKTOP_GPU, HardwareTier.CLOUD} else 0,
        )
        sel = select_models(fake)
        result["available_tiers"][tier.name] = _selection_to_dict(sel)

    return result


def _selection_to_dict(sel: ModelSelection) -> dict[str, Any]:
    """Convert ModelSelection to a JSON-friendly dict.

    ENGINES-2 (AP #48 — honest labeling): the additive ``available``
    map flags, per surfaced role, whether the named model has real
    runtime backing at HEAD (:data:`sovyx.voice.auto_select.
    RUNTIME_AVAILABLE_MODELS`). The auto-select matrix is a roadmap
    document — entries like ``parakeet-tdt-*`` / ``qwen3-tts-*`` have
    no engine, no download entry, and must render as roadmap in the
    dashboard, never as available selections.
    """
    from sovyx.voice.auto_select import is_model_runtime_available

    roles: dict[str, str] = {
        "stt_primary": sel.stt_primary,
        "stt_streaming": sel.stt_streaming,
        "tts_primary": sel.tts_primary,
        "tts_quality": sel.tts_quality,
        "wake": sel.wake,
        "vad": sel.vad,
    }
    return {
        **roles,
        "available": {role: is_model_runtime_available(model) for role, model in roles.items()},
    }
