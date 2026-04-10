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
        "stt": {
            "engine": None,
            "model": None,
            "state": None,
        },
        "tts": {
            "engine": None,
            "model": None,
            "initialized": False,
        },
        "wake_word": {
            "enabled": False,
            "phrase": None,
        },
        "vad": {
            "enabled": False,
        },
        "wyoming": {
            "connected": False,
            "endpoint": None,
        },
        "hardware": {
            "tier": None,
            "ram_mb": None,
        },
    }

    # Pipeline
    try:
        from sovyx.voice.pipeline import VoicePipeline

        if registry.is_registered(VoicePipeline):
            pipeline = await registry.resolve(VoicePipeline)
            status["pipeline"]["running"] = pipeline.is_running
            status["pipeline"]["state"] = pipeline.state.name.lower()
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_pipeline_failed")

    # STT
    try:
        from sovyx.voice.stt import STTEngine

        if registry.is_registered(STTEngine):
            stt = await registry.resolve(STTEngine)  # type: ignore[type-abstract]
            status["stt"]["engine"] = type(stt).__name__
            if hasattr(stt, "config"):
                cfg = stt.config
                status["stt"]["model"] = getattr(cfg, "model_name", None)
            if hasattr(stt, "state"):
                status["stt"]["state"] = stt.state.name.lower()
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_stt_failed")

    # TTS
    try:
        from sovyx.voice.tts_piper import TTSEngine

        if registry.is_registered(TTSEngine):
            tts = await registry.resolve(TTSEngine)  # type: ignore[type-abstract]
            status["tts"]["engine"] = type(tts).__name__
            if hasattr(tts, "config"):
                cfg = tts.config
                model = getattr(cfg, "model_path", None)
                status["tts"]["model"] = str(model) if model is not None else None
            if hasattr(tts, "is_initialized"):
                status["tts"]["initialized"] = tts.is_initialized
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_tts_failed")

    # Wake Word
    try:
        from sovyx.voice.wake_word import WakeWordDetector

        if registry.is_registered(WakeWordDetector):
            ww = await registry.resolve(WakeWordDetector)
            status["wake_word"]["enabled"] = True
            if hasattr(ww, "config"):
                cfg = ww.config
                status["wake_word"]["phrase"] = getattr(cfg, "wake_phrase", None)
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_wake_word_failed")

    # VAD
    try:
        from sovyx.voice.vad import SileroVAD

        if registry.is_registered(SileroVAD):
            status["vad"]["enabled"] = True
    except Exception:  # noqa: BLE001
        logger.debug("voice_status_vad_failed")

    # Wyoming
    try:
        from sovyx.voice.wyoming import SovyxWyomingServer

        if registry.is_registered(SovyxWyomingServer):
            wyoming = await registry.resolve(SovyxWyomingServer)
            status["wyoming"]["connected"] = getattr(wyoming, "is_running", False)
            if hasattr(wyoming, "config"):
                cfg = wyoming.config
                status["wyoming"]["endpoint"] = getattr(cfg, "endpoint", None)
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


def _selection_to_dict(sel: ModelSelection) -> dict[str, str]:
    """Convert ModelSelection to a JSON-friendly dict."""
    return {
        "stt_primary": sel.stt_primary,
        "stt_streaming": sel.stt_streaming,
        "tts_primary": sel.tts_primary,
        "tts_quality": sel.tts_quality,
        "wake": sel.wake,
        "vad": sel.vad,
    }
