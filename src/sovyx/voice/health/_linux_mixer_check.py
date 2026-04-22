"""Preflight step 9 — Linux ALSA mixer sanity.

Bridges :func:`sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots`
into the :mod:`sovyx.voice.health.preflight` contract so ``sovyx doctor
voice`` and the setup wizard can surface a warning when the host's
mixer is in a known-bad configuration *before* the cascade opens a
stream and spends its entire budget diagnosing a problem that lives in
``amixer``.

The check is cheap (a handful of subprocess calls, total < 200 ms on
healthy hosts) and side-effect-free — it reads the mixer but never
mutates it. Remediation (writing the reset values) is owned by
:class:`sovyx.voice.health.bypass.LinuxALSAMixerResetBypass`.

Behaviour across platforms:

* **Non-Linux hosts** — the check succeeds unconditionally with an
  empty hint. The preflight orchestrator expects step 9 to be in the
  step list on every platform (so dashboards render a stable 9-row
  grid) but only the Linux path actually probes anything.
* **Linux without ``amixer``** — the check also succeeds; the mixer
  can't be diagnosed, but that alone is not a failure condition. The
  daemon still starts; the bypass strategy is just unreachable and
  the user receives the same generic ``apo_degraded`` signal as on
  other platforms.
* **Linux with ``amixer`` and a saturating card** — the check fails
  with a hint pointing at the dashboard + the CLI remediation.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health._linux_mixer_probe import enumerate_alsa_mixer_snapshots

if TYPE_CHECKING:
    from sovyx.voice.health.preflight import PreflightCheck


_HINT_SATURATED = (
    "Linux ALSA mixer has saturated pre-ADC gain — microphone input will "
    "clip on every peak of speech. Open the Voice settings page and click "
    "'Reset microphone gain' to apply a safe configuration, or run "
    "`sovyx doctor voice --fix`."
)


def check_linux_mixer_sanity(
    *,
    tuning: VoiceTuningConfig | None = None,
) -> PreflightCheck:
    """Step 9 factory — ALSA mixer saturation sanity on Linux.

    Returns a :class:`PreflightCheck` that:

    * Succeeds with an empty hint on non-Linux hosts.
    * Succeeds with an informational hint on Linux hosts where
      :func:`enumerate_alsa_mixer_snapshots` returns an empty list
      (``amixer`` missing, ``/proc/asound/cards`` unreadable) — the
      check refuses to flag what it cannot measure.
    * Fails when at least one ALSA card's
      :attr:`MixerCardSnapshot.saturation_warning` is ``True`` AND
      the aggregated boost exceeds
      :attr:`VoiceTuningConfig.linux_mixer_aggregated_boost_db_ceiling`
      OR any individual control has
      :attr:`MixerControlSnapshot.saturation_risk=True`. The returned
      ``details`` dict carries the full snapshot list (card index,
      id, longname, aggregated_boost_db, saturating-control names)
      so the dashboard can render an exact diagnosis without
      re-probing.

    Args:
        tuning: Optional tuning override. Production callers pass
            ``None`` and a fresh :class:`VoiceTuningConfig` is built
            so env overrides (``SOVYX_TUNING__VOICE__*``) are read
            live.

    Returns:
        A :class:`PreflightCheck` closure.
    """
    effective = tuning if tuning is not None else VoiceTuningConfig()

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        if sys.platform != "linux":
            return True, "", {"platform": sys.platform, "skipped": True}

        snapshots = enumerate_alsa_mixer_snapshots()
        if not snapshots:
            return (
                True,
                "",
                {
                    "platform": sys.platform,
                    "snapshots": [],
                    "note": "amixer unavailable or no controls probed",
                },
            )

        saturating = [s for s in snapshots if s.saturation_warning]
        details = {
            "platform": sys.platform,
            "snapshots": [
                {
                    "card_index": s.card_index,
                    "card_id": s.card_id,
                    "card_longname": s.card_longname,
                    "aggregated_boost_db": round(s.aggregated_boost_db, 2),
                    "saturation_warning": s.saturation_warning,
                    "saturating_controls": [c.name for c in s.controls if c.saturation_risk],
                }
                for s in snapshots
            ],
            "aggregated_boost_db_ceiling": (effective.linux_mixer_aggregated_boost_db_ceiling),
            "saturation_ratio_ceiling": (effective.linux_mixer_saturation_ratio_ceiling),
        }
        if not saturating:
            return True, "", details
        return False, _HINT_SATURATED, details

    return _check


__all__ = ["check_linux_mixer_sanity"]
