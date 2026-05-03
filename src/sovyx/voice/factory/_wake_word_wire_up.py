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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.engine.errors import MindConfigError, VoiceError
from sovyx.engine.types import MindId
from sovyx.mind.config import load_mind_config
from sovyx.observability.logging import get_logger
from sovyx.voice._phonetic_matcher import PhoneticMatcher
from sovyx.voice._wake_word_resolver import (
    PretrainedModelRegistry,
    WakeWordModelResolver,
    WakeWordResolutionStrategy,
)
from sovyx.voice._wake_word_router import WakeWordRouter

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class WakeWordPerMindStatusEntry:
    """One mind's wake-word health snapshot for the status endpoint.

    Mission ``MISSION-wake-word-ui-2026-05-03.md`` §T1 (D1+D2).
    Computed by :func:`query_per_mind_wake_word_status` via re-run
    resolution + cross-reference with the live router; idempotent
    + stateless (no global cache).

    Field semantics:

    * ``mind_id`` — filesystem mind directory name.
    * ``wake_word`` — the mind's effective wake word (``MindConfig.effective_wake_word``).
    * ``voice_language`` — BCP-47 code threaded into the resolver's
      :class:`PhoneticMatcher` for diacritic / phonetic matching.
    * ``wake_word_enabled`` — what ``mind.yaml`` says (operator's intent).
    * ``runtime_registered`` — whether a detector for this mind is
      currently in the live :class:`WakeWordRouter`. Always ``False``
      when the router is None (no minds opted in OR factory boot
      caught a stale-config VoiceError per v0.28.3 T2).
    * ``model_path`` — resolved ``.onnx`` path on EXACT/PHONETIC
      strategy; ``None`` on NONE strategy or when ``wake_word_enabled``
      is False (resolution skipped to save cost).
    * ``resolution_strategy`` — string-valued discriminated union:
      ``"exact"`` | ``"phonetic"`` | ``"none"``. ``None`` when
      ``wake_word_enabled`` is False (resolution skipped).
    * ``matched_name`` — registry name that matched. For ``EXACT``,
      the ASCII-folded wake word (typically same as the file stem
      lowercase). For ``PHONETIC``, the actual matched-file name
      (e.g., ``"lucia"`` for a wake_word ``"Lúcia"``). ``None`` on
      NONE strategy or when resolution was skipped. Mission
      ``MISSION-v0.29.1-tightening-2026-05-03.md`` §T1: surfaces the
      resolver's matched-name signal that was log-only pre-v0.29.1.
    * ``phoneme_distance`` — Levenshtein-on-phonemes distance for
      PHONETIC matches. ``0`` for EXACT (no phonetic step ran).
      ``None`` on NONE strategy or when resolution was skipped — the
      resolver's internal sentinel ``-1`` is converted at the
      dataclass boundary so the wire format only carries non-negative
      ``int | None`` values.
    * ``last_error`` — operator-facing remediation message when
      ``resolution_strategy == "none"``; ``None`` when healthy or
      when resolution was skipped (disabled mind).

    The dataclass is frozen + slotted to match the existing
    :class:`WakeWordResolution` / :class:`WakeWordRouterEvent`
    ergonomics. Wire-format conversion to JSON happens at the
    pydantic boundary in the dashboard route.
    """

    mind_id: str
    wake_word: str
    voice_language: str
    wake_word_enabled: bool
    runtime_registered: bool
    model_path: Path | None
    resolution_strategy: str | None
    matched_name: str | None
    phoneme_distance: int | None
    last_error: str | None


logger = get_logger(__name__)


def build_wake_word_router_for_enabled_minds(
    *,
    data_dir: Path,
    phonetic_max_distance: int = 3,
    phonetic_fallback_enabled: bool = True,
) -> WakeWordRouter | None:
    """Return a :class:`WakeWordRouter` populated for enabled minds, or ``None``.

    Args:
        data_dir: Sovyx data directory. Each mind lives at
            ``<data_dir>/<mind_id>/mind.yaml``. The pretrained ONNX
            pool lives at ``<data_dir>/wake_word_models/pretrained/``.
        phonetic_max_distance: Maximum Levenshtein distance for
            phonetic matching. Mirrors
            ``EngineConfig.tuning.voice.wake_word_phonetic_max_distance``.
        phonetic_fallback_enabled: Kill-switch for the per-mind
            :class:`PhoneticMatcher`. When ``True`` (default), the
            resolver consults espeak-ng for diacritic / phonetic
            matches against the pretrained pool ("Lúcia" → matches
            ``lucia.onnx``); when ``False``, falls back to EXACT-only.
            Auto-degrades to EXACT-only when espeak-ng is not on PATH
            (Windows hosts without manual install) — zero behavioral
            risk vs the v0.28.2 hardcoded-None contract on those
            hosts. Mirrors
            ``EngineConfig.tuning.voice.wake_word_phonetic_fallback_enabled``.

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

    router = WakeWordRouter()
    for mind_id, wake_word, language in enabled_minds:
        # T3 of MISSION-pre-wake-word-ui-hardening (2026-05-03):
        # build a per-mind matcher because espeak-ng phoneme
        # generation is language-specific. Auto-detect via
        # ``enabled=None`` — when espeak-ng is not on PATH,
        # ``is_available=False`` and the resolver gracefully degrades
        # to EXACT-only (bit-exact v0.28.2 behaviour on Windows hosts
        # without espeak-ng manually installed). Kill-switch
        # ``phonetic_fallback_enabled=False`` lets operators force
        # EXACT-only even when espeak-ng IS present (compliance
        # / strict-naming environments).
        matcher = (
            PhoneticMatcher(language=language, enabled=None) if phonetic_fallback_enabled else None
        )
        resolver = WakeWordModelResolver(
            registry=registry,
            phonetic_matcher=matcher,
            max_phoneme_distance=phonetic_max_distance,
        )
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
                "voice.language": language,
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


def resolve_wake_word_model_for_mind(
    *,
    data_dir: Path,
    wake_word: str,
    voice_language: str = "en",
    phonetic_max_distance: int = 3,
    phonetic_fallback_enabled: bool = True,
) -> Path:
    """Resolve a single mind's wake word to a pretrained ONNX path.

    Mission ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T3 —
    the dashboard's wake-word toggle endpoint hot-applies the
    "enabled=True" case by resolving the operator's wake word against
    the pretrained pool and calling
    :meth:`VoicePipeline.register_mind_wake_word`. This helper
    encapsulates the resolution so the endpoint mirrors T1's
    refuse-to-start contract: NONE strategy raises with a clear
    remediation message instead of silently failing.

    Symmetry note (T3 of pre-wake-word-ui-hardening, 2026-05-03):
    keeps the same phonetic matcher contract as
    :func:`build_wake_word_router_for_enabled_minds` so boot-time
    and dashboard hot-apply produce identical resolution outcomes
    for the same wake-word + language inputs. Asymmetry would be
    operator-visible drift ("toggle works at boot but fails in
    dashboard, or vice-versa").

    Args:
        data_dir: Sovyx data directory; pretrained pool is
            ``<data_dir>/wake_word_models/pretrained/``.
        wake_word: The mind's effective wake word (typically
            ``MindConfig.effective_wake_word``).
        voice_language: BCP-47 language code for espeak-ng phoneme
            generation when phonetic fallback is enabled. Defaults to
            ``"en"`` to match :class:`MindConfig.voice_language`'s
            default.
        phonetic_max_distance: Maximum Levenshtein distance for
            phonetic matching.
        phonetic_fallback_enabled: Kill-switch for the
            :class:`PhoneticMatcher`. ``True`` default + auto-detect
            via ``espeak-ng on PATH`` semantics. See
            :func:`build_wake_word_router_for_enabled_minds` for
            full discussion.

    Returns:
        Resolved ``.onnx`` path on the EXACT or PHONETIC strategy.

    Raises:
        VoiceError: When the resolver returns NONE — no model in the
            pool matches this wake word. Message mirrors the
            multi-mind builder's refuse-to-start text so operators get
            consistent diagnostics from both surfaces.
    """
    pretrained_dir = data_dir / "wake_word_models" / "pretrained"
    registry = PretrainedModelRegistry(models_dir=pretrained_dir)
    matcher = (
        PhoneticMatcher(language=voice_language, enabled=None)
        if phonetic_fallback_enabled
        else None
    )
    resolver = WakeWordModelResolver(
        registry=registry,
        phonetic_matcher=matcher,
        max_phoneme_distance=phonetic_max_distance,
    )
    resolution = resolver.resolve(wake_word)
    if resolution.strategy is WakeWordResolutionStrategy.NONE or resolution.model_path is None:
        msg = (
            f"No ONNX model resolved for wake word '{wake_word}' in "
            f"{pretrained_dir}. Remediation: train via `sovyx voice "
            f"train-wake-word` (Phase 8 / T8.13), or drop "
            f"<wake_word>.onnx into the pretrained pool. STT-fallback "
            f"is deferred to v0.28.3 "
            f"(mission `MISSION-wake-word-stt-fallback-2026-05-XX`)."
        )
        raise VoiceError(msg)
    return resolution.model_path


def _enumerate_all_minds(data_dir: Path) -> list[tuple[str, str, str, bool]]:
    """Yield ``(mind_id, effective_wake_word, voice_language, wake_word_enabled)``
    per mind on disk (regardless of opt-in).

    The dashboard's per-mind status surface (T1 of
    ``MISSION-wake-word-ui-2026-05-03.md``) needs ALL minds — disabled
    minds are still rendered so operators can toggle them ON without
    leaving the page. The boot-time builder
    (:func:`build_wake_word_router_for_enabled_minds`) only consumes
    the enabled subset via :func:`_enumerate_enabled_minds` (a thin
    filter wrapper around this function).

    Filesystem enumeration (NOT MindManager.get_active_minds()): R1
    of the wake-word-runtime-wireup mission established that
    MindManager is a thin registration sink and
    ``get_active_minds()`` returns only currently-loaded minds, which
    is not the same set as "minds-on-disk".

    Best-effort: a malformed ``mind.yaml`` is logged and skipped
    (`MindConfigError`), so one bad mind does not block the daemon
    from starting voice for the rest.
    """
    minds: list[tuple[str, str, str, bool]] = []
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
        # voice_language defaults to "en" inside MindConfig (validated
        # by pydantic). Reading via attribute access is safe — the
        # field is required-with-default.
        language = config.voice_language or "en"
        minds.append(
            (
                entry.name,
                config.effective_wake_word,
                language,
                bool(config.wake_word_enabled),
            )
        )
    return minds


def _enumerate_enabled_minds(data_dir: Path) -> list[tuple[str, str, str]]:
    """Yield ``(mind_id, effective_wake_word, voice_language)`` per enabled mind.

    Thin wrapper around :func:`_enumerate_all_minds` — filters to
    ``wake_word_enabled=True`` minds and drops the trailing flag
    so the existing boot-time call sites
    (:func:`build_wake_word_router_for_enabled_minds`) keep their
    3-tuple return contract from v0.28.3 T3.
    """
    return [
        (mind_id, wake_word, language)
        for mind_id, wake_word, language, enabled in _enumerate_all_minds(data_dir)
        if enabled
    ]


def query_per_mind_wake_word_status(
    *,
    data_dir: Path,
    router: WakeWordRouter | None,
    phonetic_max_distance: int = 3,
    phonetic_fallback_enabled: bool = True,
) -> list[WakeWordPerMindStatusEntry]:
    """Compute per-mind wake-word health for the dashboard status endpoint.

    Mission ``MISSION-wake-word-ui-2026-05-03.md`` §T1 (D1).

    Idempotent + stateless: enumerates filesystem, re-runs the
    resolver per ``wake_word_enabled=True`` mind (~5 ms each), and
    cross-references with the live :class:`WakeWordRouter`. No global
    state, no cache, no boot-time eager fetch. The resolver is the
    SAME code path the boot helper uses, so dashboard-time and
    boot-time outcomes are bit-exact for the same inputs — operators
    get "if it's broken on the dashboard, it'll be broken at boot"
    parity (the v0.28.3 T2 silent-degrade observability gap closed).

    Resolution is SKIPPED for disabled minds (``wake_word_enabled=False``)
    — they appear in the result list with ``model_path=None`` and
    ``resolution_strategy=None`` so the dashboard can render them as
    "OFF, click to enable". This saves the ~5 ms resolver cost per
    disabled mind.

    Args:
        data_dir: Sovyx data directory.
        router: Live :class:`WakeWordRouter` from
            :class:`VoicePipeline`. ``None`` when the voice subsystem
            isn't running OR when the v0.28.3 T2 boot tolerance
            caught a stale-config VoiceError. Either way, every entry
            has ``runtime_registered=False``.
        phonetic_max_distance: Mirrors the same boot-time parameter.
        phonetic_fallback_enabled: Mirrors the same boot-time
            parameter (kill-switch via
            ``EngineConfig.tuning.voice.wake_word_phonetic_fallback_enabled``).

    Returns:
        One :class:`WakeWordPerMindStatusEntry` per mind on disk
        (enabled + disabled). Empty list when ``data_dir`` is missing
        OR has no mind directories.
    """
    if not data_dir.is_dir():
        return []

    pretrained_dir = data_dir / "wake_word_models" / "pretrained"
    pool_registry = PretrainedModelRegistry(models_dir=pretrained_dir)
    registered_minds = set(router.registered_minds) if router is not None else set()

    entries: list[WakeWordPerMindStatusEntry] = []
    for mind_id, wake_word, language, wake_word_enabled in _enumerate_all_minds(data_dir):
        # Skip resolution for disabled minds — they render as OFF cards
        # in the dashboard, no resolution needed (saves ~5ms each).
        if not wake_word_enabled:
            entries.append(
                WakeWordPerMindStatusEntry(
                    mind_id=mind_id,
                    wake_word=wake_word,
                    voice_language=language,
                    wake_word_enabled=False,
                    runtime_registered=False,
                    model_path=None,
                    resolution_strategy=None,
                    matched_name=None,
                    phoneme_distance=None,
                    last_error=None,
                )
            )
            continue

        # Enabled mind — re-run resolution to get current strategy.
        # Per-mind matcher (espeak-ng phonemes are language-specific);
        # mirrors the boot-time builder's per-mind matcher pattern.
        matcher = (
            PhoneticMatcher(language=language, enabled=None) if phonetic_fallback_enabled else None
        )
        resolver = WakeWordModelResolver(
            registry=pool_registry,
            phonetic_matcher=matcher,
            max_phoneme_distance=phonetic_max_distance,
        )
        resolution = resolver.resolve(wake_word)

        if resolution.strategy is WakeWordResolutionStrategy.NONE:
            # Dashboard renders ``last_error`` directly to operators.
            # Mirror the boot helper's full remediation text so the
            # error message is identical between surfaces.
            last_error = (
                f"No ONNX model resolved for wake word '{wake_word}' in "
                f"{pretrained_dir}. Remediation: train via `sovyx voice "
                f"train-wake-word --mind {mind_id}` (Phase 8 / T8.13), "
                f"drop <wake_word>.onnx into the pretrained pool, or set "
                f"wake_word_enabled: false in mind.yaml. STT-fallback for "
                f"this case is deferred (mission "
                f"`MISSION-wake-word-stt-fallback-2026-05-XX`)."
            )
            entries.append(
                WakeWordPerMindStatusEntry(
                    mind_id=mind_id,
                    wake_word=wake_word,
                    voice_language=language,
                    wake_word_enabled=True,
                    runtime_registered=mind_id in registered_minds,
                    model_path=None,
                    resolution_strategy="none",
                    matched_name=None,
                    phoneme_distance=None,
                    last_error=last_error,
                )
            )
            continue

        # EXACT or PHONETIC — model_path is non-None. Convert the
        # resolver's ``-1`` sentinel for ``phoneme_distance`` to ``None``
        # at this boundary so the wire format only carries non-negative
        # int | None values (avoids leaking a sentinel into the
        # frontend's ``z.number().int().nonnegative().nullable()`` schema).
        phoneme_distance: int | None = (
            resolution.phoneme_distance if resolution.phoneme_distance >= 0 else None
        )
        entries.append(
            WakeWordPerMindStatusEntry(
                mind_id=mind_id,
                wake_word=wake_word,
                voice_language=language,
                wake_word_enabled=True,
                runtime_registered=mind_id in registered_minds,
                model_path=resolution.model_path,
                resolution_strategy=resolution.strategy.value,
                matched_name=resolution.matched_name or None,
                phoneme_distance=phoneme_distance,
                last_error=None,
            )
        )

    return entries
