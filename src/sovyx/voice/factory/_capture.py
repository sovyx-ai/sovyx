"""Capture-side factory helpers — platform key + bypass strategy list.

Split from the legacy ``factory.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T03.

Owns the small platform-aware bucket selectors that pick which bypass
strategies the capture-integrity coordinator should try, plus the
:func:`_resolve_platform_key` normaliser the orchestrator uses to
gate platform-specific code.

Internal — accessed via ``sovyx.voice.factory._capture`` from the
package orchestrator. Not re-exported as public API.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.voice.health.bypass import PlatformBypassStrategy


__all__ = ["_build_bypass_strategies", "_resolve_platform_key"]


def _resolve_platform_key() -> str:
    """Normalise ``sys.platform`` into the three-valued Phase 1 bucket.

    Mirrors :func:`sovyx.voice.health.contract._platform_key` + the
    existing :func:`sovyx.voice.health.current_platform_key` helper but
    is scoped to the factory so the strategy list can be selected
    without importing from inside a ``TYPE_CHECKING`` guard. Unknown
    POSIX-likes fall into the ``"linux"`` bucket, which in Phase 1
    means "no bypass strategies installed" (coordinator quarantines on
    exhaustion and the factory fails over to the next endpoint).
    """
    plat = sys.platform
    if plat.startswith("win"):
        return "win32"
    if plat == "darwin":
        return "darwin"
    return "linux"


def _build_bypass_strategies(platform_key: str) -> list[PlatformBypassStrategy]:
    """Return the platform-filtered bypass strategy list.

    Order within a platform is the order the coordinator tries them;
    a strategy whose ``probe_eligibility`` reports
    ``applicable=False`` is skipped without counting toward the
    ``bypass_strategy_max_attempts`` budget.

    * **win32** —
      :class:`~sovyx.voice.health.bypass.WindowsWASAPIExclusiveBypass`.
      The definitive fix for the Windows Voice Clarity /
      ``VocaEffectPack`` regression (CLAUDE.md anti-pattern #21).
    * **linux** —
      :class:`~sovyx.voice.health.bypass.LinuxALSAMixerResetBypass`
      first (mandatory, default-on, non-destructive: mutates the
      ALSA mixer in-place and reverts on teardown), then
      :class:`~sovyx.voice.health.bypass.LinuxPipeWireDirectBypass`
      (opt-in via
      :attr:`VoiceTuningConfig.linux_pipewire_direct_bypass_enabled`;
      tears down the capture stream and reopens against the ALSA
      kernel device, bypassing the session manager). A disabled
      opt-in strategy stays in the list but reports
      ``applicable=False`` so dashboards see the intent without
      paying apply cost.
    * **darwin** — empty until Phase 4 ships
      :class:`MacOSVPIODisable`.
    """
    if platform_key == "win32":
        from sovyx.voice.health.bypass import WindowsWASAPIExclusiveBypass

        return [WindowsWASAPIExclusiveBypass()]
    if platform_key == "linux":
        from sovyx.voice.health.bypass import (
            LinuxALSACaptureSwitchBypass,
            LinuxALSAMixerResetBypass,
            LinuxPipeWireDirectBypass,
            LinuxSessionManagerEscapeBypass,
            LinuxWirePlumberDefaultSourceBypass,
        )

        # Strategy order matters: cheapest + most specific first.
        #
        # 1. ``LinuxALSAMixerResetBypass`` — mandatory, default-on.
        #    Resets saturated pre-ADC gain; no stream teardown. Covers
        #    the common "boost control driven to 100% by the desktop"
        #    pathology. First because it is the lowest-cost check.
        # 2. ``LinuxSessionManagerEscapeBypass`` — VLX-003/VLX-004.
        #    Moves the capture from a pinned ``hw:X,Y`` to a session-
        #    manager virtual when another desktop app grabbed the
        #    hardware. Second because the reopen cost is higher than
        #    a mixer-reset but lower than the hw-direct bypass below.
        # 3. ``LinuxPipeWireDirectBypass`` — opt-in.
        #    Inverse direction: goes from session-manager to hw:
        #    direct. Only fires when the user has explicitly set
        #    ``linux_pipewire_direct_bypass_enabled=True`` because
        #    engaging the bypass steals the device from every other
        #    desktop app.
        # 4. ``LinuxWirePlumberDefaultSourceBypass`` — Mission §Phase 2
        #    T2.1 (v0.30.12). Reroutes the WirePlumber default source
        #    when it points at a ``.monitor`` / muted / near-zero-volume
        #    source. Default-OFF + lenient-on (telemetry only) per
        #    feedback_staged_adoption — opt-in via SOVYX_TUNING__VOICE__
        #    LINUX_WIREPLUMBER_DEFAULT_SOURCE_BYPASS_ENABLED=true. v0.31.0
        #    flips the default. Runs AFTER the existing 3 because it
        #    mutates host-wide audio routing (riskier than per-card
        #    mixer resets) so should only fire when nothing cheaper worked.
        # 5. ``LinuxALSACaptureSwitchBypass`` — Mission §Phase 2 T2.2
        #    (v0.30.12). Engages ALSA Capture switch when [off] +
        #    lifts Internal Mic Boost when at min raw value. Default-OFF
        #    + lenient-on. Runs LAST because it iterates ALL input cards
        #    (highest-cost probe of the Linux strategies) and mutates
        #    multiple controls per card.
        return [
            LinuxALSAMixerResetBypass(),
            LinuxSessionManagerEscapeBypass(),
            LinuxPipeWireDirectBypass(),
            LinuxWirePlumberDefaultSourceBypass(),
            LinuxALSACaptureSwitchBypass(),
        ]
    return []
