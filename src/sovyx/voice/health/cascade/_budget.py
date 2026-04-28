"""Cascade tuning constants + budget / quarantine / record-winner helpers.

Split from the legacy ``cascade.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T02.

Owns the cascade tuning defaults sourced from
:class:`~sovyx.engine.config.VoiceTuningConfig` (CLAUDE.md anti-pattern
#17), the per-endpoint lifecycle-lock dict singleton, the
:func:`_quarantine_endpoint` helper used by every probe site to
register §4.4.7 kernel-invalidated endpoints, and the
:func:`_record_winner` helper that persists the cascade winner to
:class:`~sovyx.voice.health.combo_store.ComboStore`.

These are internal helpers consumed by :mod:`._executor` — the cascade
package does not re-export them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine._lock_dict import LRULockDict
from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import record_kernel_invalidated_event

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.voice.health._quarantine import EndpointQuarantine
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import Combo, ProbeResult


logger = get_logger(__name__)


__all__ = [
    "_DEFAULT_ATTEMPT_BUDGET_S",
    "_DEFAULT_LOCKS",
    "_DEFAULT_TOTAL_BUDGET_S",
    "_DEFAULT_WIZARD_TOTAL_BUDGET_S",
    "_LIFECYCLE_LOCK_MAX",
    "_VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT",
    "_default_locks",
    "_quarantine_endpoint",
    "_record_winner",
]


# ── Cascade tuning defaults ─────────────────────────────────────────────
#
# Sourced from :class:`VoiceTuningConfig` so every knob is overridable via
# ``SOVYX_TUNING__VOICE__CASCADE_*`` env vars. CLAUDE.md anti-pattern #17.

_DEFAULT_TOTAL_BUDGET_S = _VoiceTuning().cascade_total_budget_s
"""Total cascade wall-clock budget. ADR §5.6."""

_DEFAULT_ATTEMPT_BUDGET_S = _VoiceTuning().cascade_attempt_budget_s
"""Per-attempt budget passed to the probe's ``hard_timeout_s``. ADR §5.6."""

_DEFAULT_WIZARD_TOTAL_BUDGET_S = _VoiceTuning().cascade_wizard_total_budget_s
"""Wizard user-facing budget. ADR §5.6 — a human is watching."""

_LIFECYCLE_LOCK_MAX = _VoiceTuning().cascade_lifecycle_lock_max
"""Max concurrent endpoints tracked by the lifecycle lock dict."""

_VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT = 3
"""When ``voice_clarity_autofix=False``, skip indices 0..2 (WASAPI exclusive)
and start at attempt 3 (shared best-effort). ADR §5.11/§5.12.

This is a cascade-table index, not a tuning knob — changing it requires
re-ordering the :data:`WINDOWS_CASCADE` tuple. It belongs here, not in
:class:`VoiceTuningConfig`.
"""


_DEFAULT_LOCKS: LRULockDict[str] | None = None


def _default_locks() -> LRULockDict[str]:
    """Lazy singleton for callers that didn't pass a lock dict.

    Created on first use so importing this module in environments that
    don't need cascade locking (tests, doctor CLI sub-commands) doesn't
    allocate anything.
    """
    global _DEFAULT_LOCKS  # noqa: PLW0603 — lazy singleton, not user-mutable state
    if _DEFAULT_LOCKS is None:
        _DEFAULT_LOCKS = LRULockDict(maxsize=_LIFECYCLE_LOCK_MAX)
    return _DEFAULT_LOCKS


def _quarantine_endpoint(
    *,
    quarantine: EndpointQuarantine | None,
    endpoint_guid: str,
    device_friendly_name: str,
    device_interface_name: str,
    host_api: str,
    platform_key: str,
    reason: str,
    physical_device_id: str = "",
) -> bool:
    """Add ``endpoint_guid`` to the §4.4.7 quarantine and emit the L4 metric.

    Returns ``True`` when the endpoint was registered (caller short-circuits
    the cascade and returns ``source="quarantined"``); ``False`` when no
    quarantine store is configured (operator opted out via
    :attr:`VoiceTuningConfig.kernel_invalidated_failover_enabled` ``=False``).

    ``physical_device_id`` is the caller's best canonical-name identity
    for the underlying microphone. When supplied, it is stored on the
    quarantine entry so
    :func:`~sovyx.voice.health._factory_integration.select_alternative_endpoint`
    can reject every host-API alias of the same wedged driver during
    fail-over, preventing the Razer-class kernel-reset failure mode.

    Centralising this lets the cascade's three probe sites — pinned override,
    ComboStore fast path, and platform cascade loop — all register quarantine
    entries through one consistent path so the metric / log surface stays
    uniform.
    """
    if quarantine is None:
        return False
    quarantine.add(
        endpoint_guid=endpoint_guid,
        device_friendly_name=device_friendly_name,
        device_interface_name=device_interface_name,
        host_api=host_api or "unknown",
        reason=reason,
        physical_device_id=physical_device_id,
    )
    record_kernel_invalidated_event(
        platform=platform_key,
        host_api=host_api or "unknown",
        action="quarantine",
    )
    return True


def _record_winner(
    *,
    combo_store: ComboStore | None,
    endpoint_guid: str,
    device_friendly_name: str,
    device_interface_name: str,
    device_class: str,
    endpoint_fxproperties_sha: str,
    detected_apos: Sequence[str],
    combo: Combo,
    probe: ProbeResult,
    cascade_attempts_before_success: int,
) -> None:
    if combo_store is None:
        return
    try:
        combo_store.record_winning(
            endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            device_class=device_class,
            endpoint_fxproperties_sha=endpoint_fxproperties_sha,
            combo=combo,
            probe=probe,
            detected_apos=detected_apos,
            cascade_attempts_before_success=cascade_attempts_before_success,
        )
    except Exception:  # noqa: BLE001 — persisting a win is advisory; don't crash the cascade
        logger.warning(
            "voice_cascade_record_winning_failed",
            endpoint=endpoint_guid,
            exc_info=True,
        )
