"""Chaos injection foundation for voice recovery-path validation (TS3).

Mission §6 acceptance gate: "Chaos sweep at 10% fail injection
verifies recovery paths actually fire (circuit breaker opens,
fallback engages, watchdog resets)." Without an injection primitive,
every recovery code path I've built (R1 HystrixGuard, O1 watchdog,
M2 error attribution, T1 cancellation, T2 voice fallback) is
**theoretical** — its only proof is the unit tests that mock the
exact failure modes the recovery handles. Chaos injection is what
proves the codepath actually fires under realistic operating
conditions.

This module ships the **opt-in** injector. It is **disabled by
default in every code path** — call sites that want chaos coverage
import :class:`ChaosInjector`, construct one bound to a stable
``site_id`` (see naming convention below), and consult
``injector.should_inject()`` at the point a real failure could
occur.

Operator-facing surface
=======================

* ``SOVYX_CHAOS__ENABLED`` (bool, default False) — global kill
  switch. Production deployments must keep this False; chaos test
  matrices set it True via the CI configuration.
* ``SOVYX_CHAOS__INJECT_<SITE_ID>_PCT`` (int 0–100, default 0)
  — per-site injection rate. ``SITE_ID`` is the upper-snake-case
  form of the call site identifier (e.g.
  ``SOVYX_CHAOS__INJECT_STT_TIMEOUT_PCT=10`` injects timeouts at
  10% of STT calls).

Site identifiers convention
===========================

Use ``stage_kind`` lowercase snake_case::

    stt_timeout
    tts_zero_energy
    capture_underrun
    vad_corruption
    output_queue_drop

The site_id MUST be added to :data:`_KNOWN_SITES` so the
import-time guard catches typos. Adding a site is a deliberate
chaos-coverage decision; the bounded set keeps the env var
namespace tidy and the CI sweep matrix manageable.

Design decisions
================

* **Deterministic when seeded** — the injector takes an optional
  ``seed`` so tests get reproducible injection sequences. In
  production seed=None falls back to ``random.SystemRandom``,
  which is non-deterministic by design (otherwise an attacker
  who knows the seed could predict failure windows).
* **Per-injector counters** — every ``should_inject()`` call
  increments either ``injected_count`` or ``skipped_count`` so
  tests + soak runs can verify the realised rate matches the
  configured rate within the expected confidence interval.
* **Site allowlist guard** — ``ChaosInjector(site_id="...")``
  raises if site_id isn't in :data:`_KNOWN_SITES`. Catches the
  failure mode where a typo in the site_id silently turns into
  a never-injects no-op.
* **Globally disabled fast-path** — when
  ``SOVYX_CHAOS__ENABLED=False`` (the default), every
  ``should_inject()`` call returns False without consulting the
  RNG or the per-site rate. Hot-path safe; production overhead
  is one boolean check + one method call.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §6
(test strategy: chaos sweep), §3.10 TS3 task; Netflix Chaos
Monkey philosophy (failure-injection as proof-of-recovery);
CLAUDE.md anti-patterns #9 (StrEnum), #11 (loud-fail bounds).
"""

from __future__ import annotations

import os
import random
import threading
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Operator-facing env var contract ───────────────────────────────


_ENABLED_ENV_VAR = "SOVYX_CHAOS__ENABLED"
"""Global kill switch. Default False so production deployments
have zero chaos overhead unless the operator opts in."""

_RATE_ENV_VAR_PREFIX = "SOVYX_CHAOS__INJECT_"
_RATE_ENV_VAR_SUFFIX = "_PCT"
"""Per-site injection rate env var format:
``SOVYX_CHAOS__INJECT_<SITE_ID_UPPER>_PCT=<int 0-100>``."""


# ── Site allowlist (anti-pattern #9 enforcement) ───────────────────


class ChaosSite(StrEnum):
    """Closed-set vocabulary of chaos injection sites.

    Adding a site requires:

    1. Add the enum value here (lowercase snake_case).
    2. Document the site's failure mode in the docstring.
    3. Wire ``should_inject()`` at the corresponding production
       call site so chaos enables the failure mode.

    StrEnum (anti-pattern #9) — stable across xdist + serialises
    to env-var name verbatim (after upper()).
    """

    STT_TIMEOUT = "stt_timeout"
    """Inject TimeoutError into STT.transcribe to verify the S2
    timeout taxonomy + M2 DROP event fire under load."""

    TTS_ZERO_ENERGY = "tts_zero_energy"
    """Inject zero-amplitude audio into TTS synthesis output to
    verify T2 energy-validation fires + M2 DROP event lands +
    Kokoro→Piper fallback engages."""

    CAPTURE_UNDERRUN = "capture_underrun"
    """Inject silent block into capture path to verify the deaf-
    detection coordinator (O2) + watchdog promotion fire."""

    VAD_CORRUPTION = "vad_corruption"
    """Inject NaN probability into VAD output to verify the V1
    NaN/Inf guard fires + LSTM state resets correctly."""

    OUTPUT_QUEUE_DROP = "output_queue_drop"
    """Inject a queue-saturation event into the output mixer to
    verify the M2 USE saturation_pct WARN fires + the
    orchestrator's drain back-pressure activates."""

    CLOUD_STT_NETWORK_FAIL = "cloud_stt_network_fail"
    """Inject network failure into CloudSTT.transcribe to verify
    the R1 HystrixGuard CB opens after failure_threshold
    consecutive failures."""

    PIPELINE_INVALID_TRANSITION = "pipeline_invalid_transition"
    """Inject a forbidden state transition (IDLE → THINKING) to
    verify the O1 PipelineStateMachine WARN fires (lenient mode)
    or KBSignatureError raises (strict mode)."""


_KNOWN_SITES: frozenset[str] = frozenset(s.value for s in ChaosSite)
"""All site_id values that ``ChaosInjector`` accepts. Anything
not in this set is rejected at construction with ValueError —
catches typos that would silently turn into a never-injects
no-op."""


# ── Bound enforcement ─────────────────────────────────────────────


_MIN_RATE_PCT = 0
_MAX_RATE_PCT = 100


def _read_global_enabled() -> bool:
    """Parse the kill-switch env var with strict bool semantics.

    Recognises ``"true"`` / ``"1"`` / ``"yes"`` (case-insensitive)
    as True; everything else as False. Strict-mode prevents
    ambiguous values like ``"on"`` / ``"yep"`` from silently
    enabling chaos.
    """
    raw = os.environ.get(_ENABLED_ENV_VAR, "")
    return raw.strip().lower() in {"true", "1", "yes"}


def _read_site_rate_pct(site_id: str) -> int:
    """Parse the per-site rate env var.

    Returns 0 if unset OR if the value is malformed (out of
    range, not an integer). Loud-fail on malformed isn't right
    here — chaos config errors should DOWNGRADE to "no
    injection" rather than crash the daemon. The misconfiguration
    surfaces via the structured WARN below.
    """
    var_name = f"{_RATE_ENV_VAR_PREFIX}{site_id.upper()}{_RATE_ENV_VAR_SUFFIX}"
    raw = os.environ.get(var_name, "")
    if not raw:
        return 0
    try:
        rate = int(raw.strip())
    except ValueError:
        logger.warning(
            "voice.chaos.malformed_rate_env_var",
            env_var=var_name,
            raw_value=raw,
            action_required=(
                f"set {var_name} to an integer in "
                f"[{_MIN_RATE_PCT}, {_MAX_RATE_PCT}]; "
                f"falling back to 0 (no injection)"
            ),
        )
        return 0
    if not (_MIN_RATE_PCT <= rate <= _MAX_RATE_PCT):
        logger.warning(
            "voice.chaos.rate_out_of_range",
            env_var=var_name,
            raw_value=rate,
            min_rate=_MIN_RATE_PCT,
            max_rate=_MAX_RATE_PCT,
            action_required=(
                f"clamp the env var to [{_MIN_RATE_PCT}, {_MAX_RATE_PCT}]; "
                f"falling back to 0 (no injection)"
            ),
        )
        return 0
    return rate


# ── Injector ───────────────────────────────────────────────────────


class ChaosInjector:
    """Per-site chaos injection decision oracle.

    Construct one instance per call site. ``should_inject()``
    returns True with probability ``rate_pct/100`` when the
    global kill switch is enabled AND the site is in the
    allowlist. When the kill switch is False (the default),
    ``should_inject()`` always returns False without consulting
    the RNG.

    Thread-safe: an internal :class:`threading.Lock` serialises
    counter mutations + RNG access.

    Args:
        site_id: One of :class:`ChaosSite`. Loud-fail at
            construction if not in :data:`_KNOWN_SITES`.
        seed: Optional integer seed for the RNG. ``None`` (the
            default) uses :class:`random.SystemRandom` for
            non-deterministic injection (the right production
            choice — predictable seeds would let attackers time
            attacks around injection windows). Tests pass a
            stable seed for reproducible runs.
    """

    def __init__(
        self,
        site_id: str,
        *,
        seed: int | None = None,
    ) -> None:
        if site_id not in _KNOWN_SITES:
            allowed = sorted(_KNOWN_SITES)
            msg = (
                f"site_id={site_id!r} is not in the chaos allowlist. "
                f"Allowed: {allowed}. Add the value to ChaosSite + "
                f"document the failure mode + wire should_inject() at "
                f"the production call site."
            )
            raise ValueError(msg)
        self._site_id = site_id
        self._lock = threading.Lock()
        # SystemRandom is a Random subclass; the union annotation
        # keeps both branches typeable without a cast.
        self._rng: random.Random
        if seed is None:
            self._rng = random.SystemRandom()
        else:
            self._rng = random.Random(seed)
        self._injected_count = 0
        self._skipped_count = 0

    @property
    def site_id(self) -> str:
        return self._site_id

    @property
    def injected_count(self) -> int:
        with self._lock:
            return self._injected_count

    @property
    def skipped_count(self) -> int:
        with self._lock:
            return self._skipped_count

    @property
    def total_count(self) -> int:
        with self._lock:
            return self._injected_count + self._skipped_count

    @property
    def realised_rate_pct(self) -> float:
        """Realised injection rate as a percentage. 0 when no
        decisions have been made yet."""
        with self._lock:
            total = self._injected_count + self._skipped_count
            if total == 0:
                return 0.0
            return (self._injected_count / total) * 100.0

    def should_inject(self) -> bool:
        """Decide whether to inject a failure at this call site.

        Reads the global kill switch + per-site rate from the
        environment on EVERY call — operators can adjust mid-soak
        without restarting the daemon. The env-read overhead is
        cheap (two ``os.environ.get`` lookups + one int parse).

        Returns True with probability ``rate_pct / 100`` when the
        kill switch is enabled; False otherwise.
        """
        if not _read_global_enabled():
            with self._lock:
                self._skipped_count += 1
            return False
        rate_pct = _read_site_rate_pct(self._site_id)
        if rate_pct == 0:
            with self._lock:
                self._skipped_count += 1
            return False
        # Inclusive draw: ``randint(1, 100)`` ∈ [1, 100]; inject
        # when draw <= rate_pct. At rate_pct=10 → ~10% inject;
        # at rate_pct=100 → 100% inject (always); rate_pct=1 →
        # ~1% inject.
        with self._lock:
            draw = self._rng.randint(1, 100)
            inject = draw <= rate_pct
            if inject:
                self._injected_count += 1
            else:
                self._skipped_count += 1
        if inject:
            logger.info(
                "voice.chaos.injected",
                site_id=self._site_id,
                rate_pct=rate_pct,
                draw=draw,
                injected_count=self._injected_count,
                total_count=self._injected_count + self._skipped_count,
            )
        return inject

    def reset_counters(self) -> None:
        """Zero the realised-rate counters. Test-only helper.

        Production code never calls this — the counters are
        cumulative diagnostic state for the lifetime of the
        process. Provided so tests can establish a clean baseline
        without constructing a new injector each time.
        """
        with self._lock:
            self._injected_count = 0
            self._skipped_count = 0


__all__ = [
    "ChaosInjector",
    "ChaosSite",
]
