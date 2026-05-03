"""Build a :class:`WakeWordRouter` for every wake-word-enabled mind.

Mission: ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T1.

Background
----------
Pre-v0.28.2, the voice factory ALWAYS created a no-op wake-word stub
(``_create_wake_word_stub`` → :class:`_NoOpWakeWord`) that always
returned ``detected=False``, and never passed ``wake_word_router=`` to
:class:`~sovyx.voice.pipeline._orchestrator.VoicePipeline`. Operators
who set ``MindConfig.wake_word_enabled=True`` got ZERO runtime effect
— the toggle was non-functional. This helper closes that gap.

Contract
--------
The helper enumerates ``data_dir.iterdir()`` for ``mind.yaml`` files
(filesystem-as-source-of-truth — see R1 amendment in the mission
spec; we deliberately do NOT call ``MindManager.get_active_minds()``
because that returns only currently-loaded minds, not enabled-on-disk
minds), filters to those with ``wake_word_enabled=True``, resolves
each mind's wake word against the pretrained ONNX pool via
:class:`WakeWordModelResolver`, and registers a detector per mind
on a fresh :class:`WakeWordRouter`.

Returns ``None`` when zero minds have ``wake_word_enabled=True`` —
this is the backward-compat path: the orchestrator falls through to
its single-mind / no-router code path, bit-exact match v0.28.1
behaviour for operators who have not opted in.

NONE strategy is rejected
-------------------------
When the resolver returns :attr:`WakeWordResolutionStrategy.NONE`
(no ONNX model for this wake word), the helper raises
:class:`VoiceError` with a clear remediation message. The STT-fallback
path that would handle this case is DEFERRED to v0.28.3 per the
mission's D3 amendment (R3 surfaced 3 verified blockers in the
adapter contract). Refusing-to-start beats silent failure: an
operator who flips ``wake_word_enabled=True`` for a mind with no
trained model needs to know immediately, not three months from now
when they wonder why "Hey Lúcia" never fires.

Backward-compat
---------------
* Zero minds with ``wake_word_enabled=True`` on disk → returns
  ``None``. Factory passes ``wake_word_router=None`` to VoicePipeline,
  bit-exact match v0.28.1.
* ``data_dir`` is ``None`` or does not exist → returns ``None``.
* A mind directory with malformed ``mind.yaml`` → logged and skipped
  (best-effort enumeration; one bad mind doesn't take down the daemon).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.errors import MindConfigError, VoiceError
from sovyx.engine.types import MindId
from sovyx.mind.config import load_mind_config
from sovyx.observability.logging import get_logger
from sovyx.voice._wake_word_resolver import (
    PretrainedModelRegistry,
    WakeWordModelResolver,
    WakeWordResolutionStrategy,
)
from sovyx.voice._wake_word_router import WakeWordRouter

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


def build_wake_word_router_for_enabled_minds(
    *,
    data_dir: Path,
    phonetic_max_distance: int = 3,
) -> WakeWordRouter | None:
    """Return a :class:`WakeWordRouter` populated for enabled minds, or ``None``.

    Args:
        data_dir: Sovyx data directory. Each mind lives at
            ``<data_dir>/<mind_id>/mind.yaml``. The pretrained ONNX
            pool lives at ``<data_dir>/wake_word_models/pretrained/``.
        phonetic_max_distance: Maximum Levenshtein distance for
            phonetic matching. Mirrors
            ``EngineConfig.tuning.voice.wake_word_phonetic_max_distance``.

    Returns:
        A populated :class:`WakeWordRouter` when at least one mind on
        disk has ``wake_word_enabled=True`` AND its wake word resolves
        to a pretrained ONNX model. ``None`` when zero minds opt in
        (backward-compat path, bit-exact v0.28.1 behaviour).

    Raises:
        VoiceError: When a mind has ``wake_word_enabled=True`` but the
            resolver returns ``NONE`` (no ONNX model). The STT-fallback
            path for this case is deferred to v0.28.3 per the mission's
            D3 amendment.
    """
    if not data_dir.is_dir():
        logger.debug(
            "voice.factory.wake_word_wire_up.no_data_dir",
            **{"voice.data_dir": str(data_dir)},
        )
        return None

    enabled_minds = _enumerate_enabled_minds(data_dir)
    if not enabled_minds:
        logger.debug(
            "voice.factory.wake_word_wire_up.no_enabled_minds",
            **{"voice.data_dir": str(data_dir)},
        )
        return None

    pretrained_dir = data_dir / "wake_word_models" / "pretrained"
    registry = PretrainedModelRegistry(models_dir=pretrained_dir)
    # Phonetic matcher is deliberately omitted (None) at this site:
    # espeak-ng is a system binary that may not be present on the
    # operator's host (especially Windows). The resolver gracefully
    # downgrades to EXACT-only when matcher=None — operators who want
    # PHONETIC fallback install espeak-ng and the runtime path can be
    # plumbed through in a follow-up task once we surface a tuning
    # toggle for it. Unblocking T1 with EXACT-only is correct: T8.13
    # custom training writes the ONNX with the operator's exact
    # filename, so EXACT always hits for trained wake words.
    resolver = WakeWordModelResolver(
        registry=registry,
        phonetic_matcher=None,
        max_phoneme_distance=phonetic_max_distance,
    )

    router = WakeWordRouter()
    for mind_id, wake_word in enabled_minds:
        resolution = resolver.resolve(wake_word)
        if resolution.strategy is WakeWordResolutionStrategy.NONE:
            msg = (
                f"Mind '{mind_id}' has wake_word_enabled=True but no "
                f"ONNX model resolved for wake word '{wake_word}'. "
                f"Pretrained pool: {pretrained_dir}. Remediation: "
                f"(a) train via `sovyx voice train-wake-word --mind "
                f"{mind_id}` (Phase 8 / T8.13), (b) drop "
                f"<wake_word>.onnx into the pretrained pool, or "
                f"(c) set wake_word_enabled: false in mind.yaml. "
                f"STT-fallback for this case is deferred to v0.28.3 "
                f"(mission `MISSION-wake-word-stt-fallback-2026-05-XX`)."
            )
            raise VoiceError(msg)

        # EXACT or PHONETIC — model_path is guaranteed non-None.
        if resolution.model_path is None:  # pragma: no cover — defensive
            continue
        router.register_mind(MindId(mind_id), model_path=resolution.model_path)
        logger.info(
            "voice.factory.wake_word_wire_up.mind_registered",
            **{
                "voice.mind_id": str(mind_id),
                "voice.wake_word": wake_word,
                "voice.resolution_strategy": resolution.strategy.value,
                "voice.matched_name": resolution.matched_name,
                "voice.model_path": str(resolution.model_path),
            },
        )

    if router.is_empty:
        # All enabled minds skipped (defensive None paths only — should
        # not happen in practice because NONE raises above).
        return None

    logger.info(
        "voice.factory.wake_word_wire_up.router_built",
        **{
            "voice.registered_count": len(router.registered_minds),
            "voice.registered_minds": list(router.registered_minds),
        },
    )
    return router


def _enumerate_enabled_minds(data_dir: Path) -> list[tuple[str, str]]:
    """Yield ``(mind_id, effective_wake_word)`` for every enabled mind on disk.

    Filesystem enumeration (NOT MindManager.get_active_minds()): R1
    of the mission established that MindManager is a thin
    registration sink and ``get_active_minds()`` returns only
    currently-loaded minds, which is not the same set as
    "minds-with-wake-word-enabled-on-disk".

    Best-effort: a malformed ``mind.yaml`` is logged and skipped
    (`MindConfigError`), so one bad mind does not block the daemon
    from starting voice for the rest.
    """
    enabled: list[tuple[str, str]] = []
    for entry in sorted(data_dir.iterdir()):
        if not entry.is_dir():
            continue
        mind_yaml = entry / "mind.yaml"
        if not mind_yaml.is_file():
            continue
        try:
            config = load_mind_config(mind_yaml)
        except MindConfigError as exc:
            logger.warning(
                "voice.factory.wake_word_wire_up.mind_yaml_skipped",
                **{
                    "voice.mind_dir": str(entry),
                    "voice.error": str(exc),
                },
            )
            continue
        if not config.wake_word_enabled:
            continue
        enabled.append((entry.name, config.effective_wake_word))
    return enabled
