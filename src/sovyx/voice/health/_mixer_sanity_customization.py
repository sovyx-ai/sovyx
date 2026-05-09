"""User-customization heuristic for L2.5 mixer-sanity (V2 Master Plan §E.5).

Phase 5.F.11 god-file extraction from
``voice/health/_mixer_sanity.py`` (anti-pattern #16). Owns the 7-signal
heuristic that scores how likely an operator has hand-tuned their
audio mixer — the orchestrator uses this score to decide between
auto-applying a KB-driven preset vs. deferring to the operator's
configuration.

Contents:

* :data:`_SIGNAL_WEIGHTS` — per-signal weight mapping (sums to 1.0).
  Treat as single source of truth for tests; rebalancing requires an
  ADR amendment per V2 §4.I4.
* :data:`_ASOUND_STATE_RECENT_SECONDS` — 7-day mtime window for
  signal D.
* :class:`_UserCustomizationReport` — composite score + signals fired.
* :func:`detect_user_customization` — main entry point.
* :func:`_directory_has_configs` + :func:`_file_mtime_recent` —
  filesystem probe helpers.

All filesystem paths in :func:`detect_user_customization` are
injectable so tests can pin ``home_dir`` and ``asound_state_path`` at
a ``tmp_path`` fixture without touching the real user environment.

Anti-pattern #20 covered: parent module ``voice/health/_mixer_sanity.py``
re-exports every symbol so the public consumer at
``voice/health/__init__.py`` and the test file
``tests/unit/voice/health/test_mixer_sanity_customization.py`` (which
imports both ``detect_user_customization`` and the private constants
``_ASOUND_STATE_RECENT_SECONDS`` / ``_SIGNAL_WEIGHTS``) continue to
resolve via standard module-namespace lookup.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import HardwareContext


_SIGNAL_WEIGHTS: Mapping[str, float] = {
    # Order matches V2 Master Plan §E.5 bullet list. Sums to 1.0.
    "A_mixer_differs_from_factory": 0.30,
    "B_asoundrc_exists": 0.15,
    "C_pipewire_user_conf": 0.15,
    "D_asound_state_recent": 0.15,
    "E_wireplumber_user_conf": 0.10,
    "F_combo_store_has_entry_with_drift": 0.10,
    "G_capture_overrides_pinned": 0.05,
}
"""Per-signal weights for the user-customization heuristic.

Treat this mapping as the single source of truth for tests — any
reweighting MUST update every doctest + test assertion that depends
on the 0-1 total. Rebalancing requires an ADR amendment (see
ADR-voice-mixer-sanity-l2.5-bidirectional §4.I4).
"""


_ASOUND_STATE_RECENT_SECONDS: float = 7 * 24 * 3600.0
"""A mtime within the last 7 days on ``/var/lib/alsa/asound.state``
counts as "user tweaked recently". Shorter than the pilot-case
tolerance (factory-bad state rewrites the file on every boot — one
week excludes that).
"""


@dataclass(frozen=True, slots=True)
class _UserCustomizationReport:
    """Per-signal breakdown — for telemetry + test introspection."""

    score: float
    signals_fired: tuple[str, ...]


def detect_user_customization(
    *,
    factory_signature_score: float,
    hw: HardwareContext,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    endpoint_guid: str | None = None,
    home_dir: Path | None = None,
    asound_state_path: Path | None = None,
    time_now_s: float | None = None,
) -> _UserCustomizationReport:
    """Score user-customization likelihood in ``[0, 1]`` via 7 signals.

    All filesystem paths are injectable so tests can pin ``home_dir``
    and ``asound_state_path`` at a ``tmp_path`` fixture without
    touching the real user environment.

    Signal semantics (matching V2 §E.5):

    * **A** — current mixer deviates from the matched KB profile's
      factory signature. When ``factory_signature_score`` is low the
      mixer is unlike the factory-bad regime → user has moved it.
      Contributes ``(1.0 - factory_signature_score) * 0.30``.
    * **B** — ``~/.asoundrc`` exists. Explicit user config.
    * **C** — any file under ``~/.config/pipewire/pipewire.conf.d/``
      suggests PipeWire tuning.
    * **D** — ``/var/lib/alsa/asound.state`` mtime within the last
      7 days (``_ASOUND_STATE_RECENT_SECONDS``).
    * **E** — any file under
      ``~/.config/wireplumber/wireplumber.conf.d/``.
    * **F** — ``ComboStore`` has a recorded entry for this
      endpoint AND the factory-signature score is below 0.5
      (meaning "user got this working outside the factory-bad
      regime").
    * **G** — ``CaptureOverrides`` has a pinned combo for this
      endpoint (hard signal: user explicitly pinned a config).

    Args:
        factory_signature_score: ``0..1`` fraction from the matched
            profile's factory-signature check. Lower → stronger
            customization signal.
        hw: Detected hardware context. Currently consumed only by
            signal A via the factory score; kept in the signature
            so future signals (per-codec quirks) fit without an API
            break.
        combo_store: ``ComboStore`` singleton. ``None`` disables
            signal F (tests may choose to skip).
        capture_overrides: ``CaptureOverrides`` singleton. ``None``
            disables signal G.
        endpoint_guid: Needed to key into combo_store /
            capture_overrides. ``None`` disables F + G.
        home_dir: User home directory. Defaults to
            :meth:`Path.home()`; injected in tests.
        asound_state_path: Absolute path to asound.state. Defaults to
            ``/var/lib/alsa/asound.state``; injected in tests.
        time_now_s: ``time.time()`` override for deterministic
            mtime comparison in tests.

    Returns:
        :class:`_UserCustomizationReport` with the composite score
        and the list of signal codes that fired.
    """
    # Signal A is continuous — partial credit per plan. Every other
    # signal is boolean (present → full weight).
    del hw  # reserved for future per-codec quirks
    signals_fired: list[str] = []
    total: float = 0.0

    a_contribution = (
        max(0.0, 1.0 - float(factory_signature_score))
        * _SIGNAL_WEIGHTS["A_mixer_differs_from_factory"]
    )
    if a_contribution > 0:
        signals_fired.append("A_mixer_differs_from_factory")
    total += a_contribution

    home = home_dir if home_dir is not None else Path.home()

    if (home / ".asoundrc").exists():
        signals_fired.append("B_asoundrc_exists")
        total += _SIGNAL_WEIGHTS["B_asoundrc_exists"]

    pipewire_conf_d = home / ".config" / "pipewire" / "pipewire.conf.d"
    if _directory_has_configs(pipewire_conf_d):
        signals_fired.append("C_pipewire_user_conf")
        total += _SIGNAL_WEIGHTS["C_pipewire_user_conf"]

    asound_path = (
        asound_state_path if asound_state_path is not None else Path("/var/lib/alsa/asound.state")
    )
    now = time_now_s if time_now_s is not None else time.time()
    if _file_mtime_recent(asound_path, now=now, window_s=_ASOUND_STATE_RECENT_SECONDS):
        signals_fired.append("D_asound_state_recent")
        total += _SIGNAL_WEIGHTS["D_asound_state_recent"]

    wireplumber_conf_d = home / ".config" / "wireplumber" / "wireplumber.conf.d"
    if _directory_has_configs(wireplumber_conf_d):
        signals_fired.append("E_wireplumber_user_conf")
        total += _SIGNAL_WEIGHTS["E_wireplumber_user_conf"]

    if (
        combo_store is not None
        and endpoint_guid is not None
        and combo_store.get(endpoint_guid) is not None
        and factory_signature_score < 0.5  # noqa: PLR2004 — §E.5 threshold
    ):
        signals_fired.append("F_combo_store_has_entry_with_drift")
        total += _SIGNAL_WEIGHTS["F_combo_store_has_entry_with_drift"]

    if (
        capture_overrides is not None
        and endpoint_guid is not None
        and capture_overrides.get_entry(endpoint_guid) is not None
    ):
        signals_fired.append("G_capture_overrides_pinned")
        total += _SIGNAL_WEIGHTS["G_capture_overrides_pinned"]

    clamped = min(1.0, max(0.0, total))
    return _UserCustomizationReport(
        score=clamped,
        signals_fired=tuple(signals_fired),
    )


def _directory_has_configs(directory: Path) -> bool:
    """Return True iff ``directory`` exists AND contains at least one
    ``*.conf`` file. Non-existence is the dominant case — users who
    never tuned PipeWire / WirePlumber don't have these paths at all.
    """
    try:
        if not directory.is_dir():
            return False
        return any(entry.suffix == ".conf" for entry in directory.iterdir())
    except OSError:
        # Permission denied / transient I/O — don't fire the signal.
        return False


def _file_mtime_recent(path: Path, *, now: float, window_s: float) -> bool:
    """Return True iff ``path`` exists and its mtime is within
    ``window_s`` seconds of ``now``. Any OSError → False.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (now - mtime) <= window_s


__all__ = [
    "_ASOUND_STATE_RECENT_SECONDS",
    "_SIGNAL_WEIGHTS",
    "_UserCustomizationReport",
    "_directory_has_configs",
    "_file_mtime_recent",
    "detect_user_customization",
]
