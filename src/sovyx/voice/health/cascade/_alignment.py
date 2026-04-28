"""Cascade alignment helpers: pinned overrides, store fast-path, L2.5.

Split from the legacy ``cascade.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T02.

Pre-cascade lookup helpers that align the cascade with persisted state
(:class:`~sovyx.voice.health.capture_overrides.CaptureOverrides` pinned
combos, :class:`~sovyx.voice.health.combo_store.ComboStore` fast-path
hits) plus the L2.5 mixer-sanity invocation helper that runs once per
cascade pass on Linux.

These are internal helpers consumed by :mod:`._executor` — the cascade
package does not re-export them.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health.contract import CandidateEndpoint, CandidateSource

if TYPE_CHECKING:
    from sovyx.voice.health._mixer_sanity import MixerSanitySetup
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import Combo


logger = get_logger(__name__)


__all__ = ["_lookup_override", "_lookup_store", "_run_mixer_sanity"]


async def _run_mixer_sanity(
    *,
    mixer_sanity: MixerSanitySetup,
    endpoint_guid: str,
    device_index: int,
    device_friendly_name: str,
    combo_store: ComboStore | None,
    capture_overrides: CaptureOverrides | None,
    tuning: _VoiceTuning | None = None,
) -> None:
    """Invoke L2.5 ``check_and_maybe_heal`` for this endpoint.

    Fire-and-forget from the cascade's perspective: the outcome is
    logged + telemetry'd internally (via the ``_mixer_sanity`` module),
    but we return no value — the cascade continues with its platform
    walk regardless. L2.5 heals the ALSA mixer state; the platform
    cascade still picks the PortAudio combo.

    Builds a minimal :class:`CandidateEndpoint` on the fly so the
    orchestrator has an endpoint identity to key telemetry on. Full
    candidate metadata (source, preference_rank, canonical_name)
    isn't needed for L2.5 — it operates on mixer state, not endpoint
    enumeration.

    Any unexpected error inside L2.5 is swallowed (already logged by
    the orchestrator) so a misbehaving KB or probe cannot abort the
    cascade — invariant P6 applied at the integration boundary.
    """
    from sovyx.voice.device_enum import (
        DeviceKind,  # noqa: PLC0415 — lazy; only Linux path needs it
    )
    from sovyx.voice.health._mixer_sanity import (
        check_and_maybe_heal,  # noqa: PLC0415 — lazy to avoid Linux import cost on Windows cold-start
    )

    endpoint = CandidateEndpoint(
        device_index=device_index,
        host_api_name="ALSA",
        kind=DeviceKind.HARDWARE,
        canonical_name=device_friendly_name or f"endpoint-{endpoint_guid}",
        friendly_name=device_friendly_name or f"endpoint-{endpoint_guid}",
        source=CandidateSource.USER_PREFERRED,
        preference_rank=0,
        endpoint_guid=endpoint_guid,
    )
    # Paranoid-QA CRITICAL #8: use the caller's tuning when
    # provided — discarding it here would silently ignore every
    # SOVYX_TUNING__VOICE__LINUX_MIXER_SANITY_* env override and
    # violate anti-pattern #17 ("Hardcoded tuning constants").
    effective_tuning = tuning if tuning is not None else _VoiceTuning()
    try:
        result = await check_and_maybe_heal(
            endpoint,
            mixer_sanity.hw,
            kb_lookup=mixer_sanity.kb_lookup,
            role_resolver=mixer_sanity.role_resolver,
            validation_probe_fn=mixer_sanity.validation_probe_fn,
            tuning=effective_tuning,
            mixer_probe_fn=mixer_sanity.mixer_probe_fn,
            mixer_apply_fn=mixer_sanity.mixer_apply_fn,
            mixer_restore_fn=mixer_sanity.mixer_restore_fn,
            persist_fn=mixer_sanity.persist_fn,
            telemetry=mixer_sanity.telemetry,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
            half_heal_wal_path=mixer_sanity.half_heal_wal_path,
        )
    except asyncio.CancelledError:
        # Paranoid-QA CRITICAL #1: cancel propagates past the cascade
        # integration layer — the cascade itself decides whether to
        # swallow or re-raise.
        raise
    except Exception as exc:  # noqa: BLE001 — Exception-only post-QA
        logger.warning(
            "voice_cascade_mixer_sanity_unexpected",
            endpoint=endpoint_guid,
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return
    logger.info(
        "voice_cascade_mixer_sanity_outcome",
        endpoint=endpoint_guid,
        decision=result.decision.value,
        matched_profile=result.matched_kb_profile,
        score=round(result.kb_match_score, 3),
        regime=result.regime,
        apply_duration_ms=result.apply_duration_ms,
        validation_passed=result.validation_passed,
        error=result.error,
    )


def _lookup_override(
    overrides: CaptureOverrides | None,
    endpoint_guid: str,
    platform_key: str,
) -> Combo | None:
    if overrides is None:
        return None
    try:
        combo = overrides.get(endpoint_guid)
    except Exception:  # noqa: BLE001 — cascade must fall through on any store-side failure (ADR I4)
        logger.warning(
            "voice_cascade_pinned_lookup_failed",
            endpoint=endpoint_guid,
            exc_info=True,
        )
        return None
    if combo is None:
        return None
    # Sanity: reject an override that isn't valid for this platform.
    if combo.platform_key and combo.platform_key != platform_key:
        logger.warning(
            "voice_cascade_pinned_platform_mismatch",
            endpoint=endpoint_guid,
            combo_platform=combo.platform_key,
            runtime_platform=platform_key,
        )
        return None
    return combo


def _lookup_store(
    combo_store: ComboStore | None,
    endpoint_guid: str,
) -> Combo | None:
    if combo_store is None:
        return None
    try:
        entry = combo_store.get(endpoint_guid)
    except Exception:  # noqa: BLE001 — cascade must fall through on any store-side failure (ADR I4)
        logger.warning(
            "voice_cascade_store_lookup_failed",
            endpoint=endpoint_guid,
            exc_info=True,
        )
        return None
    if entry is None:
        return None
    if combo_store.needs_revalidation(endpoint_guid):
        logger.info(
            "voice_cascade_store_needs_revalidation",
            endpoint=endpoint_guid,
        )
    return entry.winning_combo
