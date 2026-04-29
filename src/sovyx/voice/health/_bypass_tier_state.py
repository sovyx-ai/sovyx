"""In-memory bypass-tier state mirror for synchronous dashboard reads.

OpenTelemetry counters fire at every ``record_tier*_*`` and
``record_bypass_strategy_verdict`` call site (see :mod:`._metrics`). The OTel
SDK does not expose ``.read()`` on counter handles — they are a write-only
sink for exporters. The dashboard endpoint
``GET /api/voice/bypass-tier-status`` (Voice Windows Paranoid Mission §B)
needs a *synchronous* read that does not depend on a Prometheus / OTLP
collector being deployed alongside the daemon. This module mirrors the
counter state as plain integers alongside the OTel writes.

Single source of truth — every ``record_*`` helper in :mod:`._metrics` calls
into the matching ``mark_*`` function here BEFORE firing the OTel counter,
so the mirror cannot drift unless the helper itself is bypassed.
``current_bypass_tier`` is intentionally LEFT ``None`` for v0.26.0 — it
requires coordinator-side engaged-tier tracking that lives in
:class:`CaptureIntegrityCoordinator` and is staged for v0.27.0 per the
master mission rollout matrix (don't bundle "foundation + 5 call-site
adoptions" in one commit — staged adoption rule).

Tier → strategy mapping (Voice Windows Paranoid Mission ADR
``ADR-voice-bypass-tier-system.md``):

* Tier 1 RAW + Communications → ``win.raw_communications`` strategy
* Tier 2 host-API rotate-then-exclusive → ``win.host_api_rotate_then_exclusive``
* Tier 3 WASAPI exclusive → ``win.wasapi_exclusive``

"Succeeded" is tier-specific:

* Tier 1: ``RawCommunicationsRestartVerdict`` value ``"raw_engaged"``.
* Tier 2: ``HostApiRotateVerdict.rotated_success`` AND
  ``ExclusiveRestartVerdict`` value indicating exclusive engaged
  (the strategy's combined verdict ``"rotated_then_exclusive_engaged"``).
* Tier 3: ``BypassVerdict.APPLIED_HEALTHY`` from the coordinator-level
  ``record_bypass_strategy_verdict`` — Tier 3 has no tier-specific helper
  pair, only the coordinator hook.

The lock is :class:`threading.Lock` (not asyncio) because OTel counter
``.add()`` calls happen on whatever thread the strategy runs on — the
coordinator runs on the asyncio loop thread, but a future strategy may
fire from a portaudio callback thread. Reads from the dashboard route
are also lock-protected for a deterministic snapshot.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass


@dataclass
class BypassTierSnapshot:
    """Plain-value snapshot of the bypass-tier state.

    Field shape mirrors :class:`VoiceBypassTierStatusResponseSchema` in
    ``dashboard/src/types/schemas.ts``. ``current_bypass_tier=None``
    until coordinator-side engaged-tier tracking lands.
    """

    current_bypass_tier: int | None = None
    tier1_raw_attempted: int = 0
    tier1_raw_succeeded: int = 0
    tier2_host_api_rotate_attempted: int = 0
    tier2_host_api_rotate_succeeded: int = 0
    tier3_wasapi_exclusive_attempted: int = 0
    tier3_wasapi_exclusive_succeeded: int = 0


_TIER1_RAW_SUCCESS_VERDICT = "raw_engaged"
_TIER2_COMBINED_SUCCESS_VERDICT = "rotated_then_exclusive_engaged"
_TIER3_STRATEGY_NAME = "win.wasapi_exclusive"
_TIER3_SUCCESS_VERDICT = "applied_healthy"


_state = BypassTierSnapshot()
_lock = threading.Lock()


def mark_tier1_raw_attempted() -> None:
    """Increment the Tier 1 RAW attempt counter."""
    with _lock:
        _state.tier1_raw_attempted += 1


def mark_tier1_raw_outcome(verdict: str) -> None:
    """Increment the Tier 1 RAW success counter when verdict is success."""
    if verdict != _TIER1_RAW_SUCCESS_VERDICT:
        return
    with _lock:
        _state.tier1_raw_succeeded += 1


def mark_tier2_host_api_rotate_attempted() -> None:
    """Increment the Tier 2 host-API rotate attempt counter."""
    with _lock:
        _state.tier2_host_api_rotate_attempted += 1


def mark_tier2_host_api_rotate_outcome(*, phase_a_verdict: str, phase_b_verdict: str) -> None:
    """Increment Tier 2 success when both phases reach exclusive-engaged.

    The strategy's combined verdict is ``"rotated_then_exclusive_engaged"``
    when ``phase_a_verdict == "rotated_success"`` AND ``phase_b_verdict``
    is the exclusive-engaged terminal value. We accept either the explicit
    combined token or the conjunction of the two phase verdicts to stay
    robust against the strategy emitting either form.
    """
    combined = f"{phase_a_verdict}+{phase_b_verdict}"
    if (
        combined == "rotated_success+exclusive_engaged"
        or phase_b_verdict == _TIER2_COMBINED_SUCCESS_VERDICT
    ):
        with _lock:
            _state.tier2_host_api_rotate_succeeded += 1


def mark_strategy_verdict(*, strategy: str, verdict: str) -> None:
    """Increment Tier 3 attempt / success counters from coordinator hook.

    Filtered to ``strategy == "win.wasapi_exclusive"`` because Tier 1 + 2
    fire their own tier-specific helpers (``record_tier1_raw_*`` /
    ``record_tier2_host_api_rotate_*``). Tier 3 has no equivalent helper,
    so the coordinator-level verdict hook is the single source of truth.

    The ``not_applicable`` verdict signals an eligibility rejection, NOT
    an attempt, so we skip it (matches the semantics of
    ``record_tier1_raw_attempted`` which fires only after eligibility
    passes).
    """
    if strategy != _TIER3_STRATEGY_NAME:
        return
    if verdict == "not_applicable":
        return
    with _lock:
        _state.tier3_wasapi_exclusive_attempted += 1
        if verdict == _TIER3_SUCCESS_VERDICT:
            _state.tier3_wasapi_exclusive_succeeded += 1


def snapshot() -> dict[str, int | None]:
    """Return a plain-dict snapshot for dashboard JSON serialisation.

    Lock-protected so the dashboard observes a consistent view even if
    a strategy fires concurrently. Returned dict matches the wire shape
    of :class:`VoiceBypassTierStatusResponseSchema`.
    """
    with _lock:
        return asdict(_state)


def reset_for_tests() -> None:
    """Reset the global state — test-only helper.

    Production code never resets the counters; they accumulate over the
    lifetime of the daemon process. Tests use this to isolate state
    between cases.
    """
    global _state  # noqa: PLW0603 — module-level state mirror is intentional
    with _lock:
        _state = BypassTierSnapshot()


__all__ = [
    "BypassTierSnapshot",
    "mark_strategy_verdict",
    "mark_tier1_raw_attempted",
    "mark_tier1_raw_outcome",
    "mark_tier2_host_api_rotate_attempted",
    "mark_tier2_host_api_rotate_outcome",
    "reset_for_tests",
    "snapshot",
]
