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

    from sovyx.engine.events import EventBus
    from sovyx.voice._capture_task import AudioCaptureTask
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
    language: str = "en",
    voice_id: str = "",
    wake_word_enabled: bool = False,
    mind_id: str = "default",
    input_device: int | str | None = None,
    output_device: int | str | None = None,  # noqa: ARG001 — reserved for future TTS routing
) -> VoiceBundle:
    """Create a fully initialized VoicePipeline with all components.

    All ONNX model loads are wrapped in ``asyncio.to_thread`` to avoid
    blocking the event loop.

    Args:
        event_bus: System event bus for voice events.
        on_perception: Callback when speech is transcribed.
        model_dir: Override model cache directory.
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
            microphone capture task. ``None`` = OS default.
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
    stt = await asyncio.to_thread(lambda: _self._create_stt(language))

    # ── 3. TTS (Piper > Kokoro > error) ──────────────────────
    tts_engine = detect_tts_engine()
    logger.info("voice_factory_creating_tts", engine=tts_engine)
    if tts_engine == "piper":
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

    # ── 6. Capture task (not started yet) ────────────────────
    from sovyx.voice._capture_task import AudioCaptureTask

    capture_task = AudioCaptureTask(pipeline, input_device=input_device)

    await pipeline.start()

    logger.info(
        "voice_pipeline_created",
        stt="moonshine",
        tts=tts_engine,
        vad="silero-v5",
        mind_id=mind_id,
        input_device=input_device if input_device is not None else "default",
    )
    return VoiceBundle(pipeline=pipeline, capture_task=capture_task)


# ── Component factories (sync — called via to_thread) ────────────────


def _create_vad(model_path: Path) -> Any:  # noqa: ANN401
    from sovyx.voice.vad import SileroVAD

    return SileroVAD(model_path=model_path)


def _create_stt(language: str) -> Any:  # noqa: ANN401, ARG001
    from sovyx.voice.stt import MoonshineSTT

    engine = MoonshineSTT()
    # MoonshineSTT.initialize() downloads model on first call
    # We call it here so the download happens during factory, not first use
    return engine


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

    if resolved_voice is None:
        canonical = voice_catalog.normalize_language(language)
        recommended = voice_catalog.recommended_voice(canonical)
        if recommended is not None:
            resolved_voice = recommended.id
            resolved_language = recommended.language

    if resolved_voice is not None and resolved_language is not None:
        config = KokoroConfig(voice=resolved_voice, language=resolved_language)
        return KokoroTTS(model_dir=model_dir / "kokoro", config=config)

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
