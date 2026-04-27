"""Playback-side factory helpers — VAD + TTS instantiators.

Split from the legacy ``factory.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T03.

Synchronous component constructors invoked from the factory's async
control flow via ``asyncio.to_thread`` — keeps each
ONNX-session-allocating step trivially test-patchable while the
async model load stays on the orchestrator's path.

Re-exported via the package ``__init__`` for tests that import
``_create_kokoro_tts`` directly (legacy back-compat).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path


logger = get_logger(__name__)


__all__ = ["_create_kokoro_tts", "_create_piper_tts", "_create_vad"]


def _create_vad(model_path: Path) -> Any:  # noqa: ANN401
    from sovyx.voice.vad import SileroVAD

    return SileroVAD(model_path=model_path)


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
