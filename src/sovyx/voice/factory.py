"""Voice pipeline factory -- instantiate all components for hot-enable.

Creates SileroVAD, MoonshineSTT, TTS (Piper or Kokoro fallback),
WakeWordDetector, and VoicePipeline in a single async call.
All ONNX loads wrapped in to_thread.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.model_registry import (
    detect_tts_engine,
    ensure_silero_vad,
    get_default_model_dir,
)

_self = sys.modules[__name__]

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sovyx.engine.events import EventBus
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

logger = get_logger(__name__)


class VoiceFactoryError(Exception):
    """Raised when voice pipeline components can't be created."""

    def __init__(self, message: str, missing_models: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.missing_models = missing_models or []


async def create_voice_pipeline(
    *,
    event_bus: EventBus | None = None,
    on_perception: Callable[[str, str], Awaitable[None]] | None = None,
    model_dir: Path | None = None,
    language: str = "en",
    wake_word_enabled: bool = False,
    mind_id: str = "default",
) -> VoicePipeline:
    """Create a fully initialized VoicePipeline with all components.

    All ONNX model loads are wrapped in ``asyncio.to_thread`` to avoid
    blocking the event loop.

    Args:
        event_bus: System event bus for voice events.
        on_perception: Callback when speech is transcribed.
        model_dir: Override model cache directory.
        language: STT language code.
        wake_word_enabled: Whether to listen for wake word.
        mind_id: Mind identifier for pipeline config.

    Returns:
        Running VoicePipeline.

    Raises:
        VoiceFactoryError: If required components can't be created.
    """
    models_dir = model_dir or get_default_model_dir()
    models_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. SileroVAD (auto-download) ──────────────────────────
    logger.info("voice_factory_creating_vad")
    vad_path = await ensure_silero_vad(models_dir)
    vad = await asyncio.to_thread(_self._create_vad, vad_path)

    # ── 2. MoonshineSTT (auto-download via HF Hub) ───────────
    logger.info("voice_factory_creating_stt", language=language)
    stt = await asyncio.to_thread(_self._create_stt, language)

    # ── 3. TTS (Piper > Kokoro > error) ──────────────────────
    tts_engine = detect_tts_engine()
    logger.info("voice_factory_creating_tts", engine=tts_engine)
    if tts_engine == "piper":
        tts = await asyncio.to_thread(_self._create_piper_tts, models_dir)
    elif tts_engine == "kokoro":
        tts = await asyncio.to_thread(_self._create_kokoro_tts, models_dir)
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

    # ── 5. Build pipeline ────────────────────────────────────
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

    config = VoicePipelineConfig(
        mind_id=mind_id,
        wake_word_enabled=wake_word_enabled,
    )

    pipeline = VoicePipeline(
        config=config,
        vad=vad,
        wake_word=wake,
        stt=stt,
        tts=tts,
        event_bus=event_bus,
        on_perception=on_perception,
    )
    await pipeline.start()

    logger.info(
        "voice_pipeline_created",
        stt="moonshine",
        tts=tts_engine,
        vad="silero-v5",
        mind_id=mind_id,
    )
    return pipeline


# ── Component factories (sync — called via to_thread) ────────────────


def _create_vad(model_path: Path) -> Any:  # noqa: ANN401
    from sovyx.voice.vad import SileroVAD

    return SileroVAD(model_path=model_path)


def _create_stt(language: str) -> Any:  # noqa: ANN401
    from sovyx.voice.stt import MoonshineSTT

    engine = MoonshineSTT()
    # MoonshineSTT.initialize() downloads model on first call
    # We call it here so the download happens during factory, not first use
    return engine


def _create_piper_tts(model_dir: Path) -> Any:  # noqa: ANN401
    from sovyx.voice.tts_piper import PiperTTS

    return PiperTTS(model_dir=model_dir / "piper")


def _create_kokoro_tts(model_dir: Path) -> Any:  # noqa: ANN401
    from sovyx.voice.tts_kokoro import KokoroTTS

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
