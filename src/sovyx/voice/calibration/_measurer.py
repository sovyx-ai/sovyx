"""Targeted measurement extraction for calibration.

Builds a :class:`MeasurementSnapshot` from real system state (Linux
mixer via ``amixer``) + diag-tarball artifacts (W/K analysis.json
files containing per-capture RMS / VAD probability / latency). The
calibration rules consume the snapshot to gate decisions; R10 in
particular needs ``mixer_attenuation_regime`` which is computed
locally here from the mixer percentages.

For v0.30.15 alpha, the measurer is a hybrid:

* **Real local capture**: ``amixer scontents`` parsed for the three
  controls R10 cares about (Capture, Mic Boost, Internal Mic Boost).
  Classified into ``"saturated" | "attenuated" | "healthy"`` via a
  deterministic threshold (>= 90% saturated; <= 10% attenuated;
  otherwise healthy).
* **Diag-tarball extraction**: when the CLI runs ``--full-diag``
  before ``--calibrate`` and passes the resulting tarball root, the
  measurer reads ``analysis.json`` files under ``E_portaudio/captures/``
  to populate ``rms_dbfs_per_capture`` + ``vad_speech_probability_*``.
  Without a tarball, those fields stay sentinel (which makes R10
  refuse to fire on the triage gate, by design -- the operator
  should run --full-diag first to disambiguate).

T2.3 of MISSION-voice-self-calibrating-system-2026-05-05.md (Layer 2
v0.30.15 foundation). Future v0.30.16+: add latency + jitter +
echo_correlation extraction once the diag emits them in
SUMMARY.json structured form.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.calibration.schema import (
    MEASUREMENT_SNAPSHOT_SCHEMA_VERSION,
    MeasurementSnapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.voice.diagnostics import TriageResult

logger = get_logger(__name__)

_AMIXER_TIMEOUT_S = 5.0
_SATURATED_THRESHOLD_PCT = 90
_ATTENUATED_THRESHOLD_PCT = 10

# Mixer simple-control name patterns to look up. The first match wins
# (controls have stable canonical names across distros, but we accept
# variants like "Mic Boost" vs "Internal Mic Boost"). The R10 gate
# triggers on the *combined* state of these three controls.
_CAPTURE_PATTERNS = ("Capture",)
_BOOST_PATTERNS = ("Mic Boost",)
_INTERNAL_MIC_BOOST_PATTERNS = ("Internal Mic Boost",)


def capture_measurements(
    *,
    diag_tarball_root: Path | None = None,
    triage_result: TriageResult | None = None,
    captured_at_utc: str | None = None,
    duration_s: float = 0.0,
) -> MeasurementSnapshot:
    """Build a MeasurementSnapshot for calibration.

    Args:
        diag_tarball_root: Optional extracted root of a diag tarball
            (the path returned by L1's :func:`run_full_diag`).
            When provided, ``analysis.json`` files under
            ``E_portaudio/captures/`` are read to populate
            ``rms_dbfs_per_capture`` + ``vad_speech_probability_*``.
        triage_result: Optional triage output; used to populate
            ``triage_winner_hid`` + ``triage_winner_confidence``.
        captured_at_utc: Override the capture timestamp (testability).
        duration_s: Wall-clock duration of the source diag run, if
            known. Defaults to 0.0 (caller didn't time it).

    Returns:
        A frozen :class:`MeasurementSnapshot`. Fields the local
        capture path can't populate (latency, jitter, echo) ship as
        sentinel zeros / ``None`` for v0.30.15; v0.30.16 adds the
        structured-SUMMARY.json extraction once the diag emits them.
    """
    mixer = _read_mixer_state()
    rms_per_capture, vad_max, vad_p99 = _extract_capture_metrics(diag_tarball_root)
    triage_hid: str | None = None
    triage_conf: float | None = None
    if triage_result is not None and triage_result.winner is not None:
        triage_hid = triage_result.winner.hid.value
        triage_conf = triage_result.winner.confidence

    return MeasurementSnapshot(
        schema_version=MEASUREMENT_SNAPSHOT_SCHEMA_VERSION,
        captured_at_utc=(
            captured_at_utc
            if captured_at_utc is not None
            else datetime.now(tz=UTC).isoformat(timespec="seconds")
        ),
        duration_s=duration_s,
        rms_dbfs_per_capture=rms_per_capture,
        vad_speech_probability_max=vad_max,
        vad_speech_probability_p99=vad_p99,
        # Noise floor + latency + jitter + echo extraction are deferred
        # to v0.30.16; sentinels keep v0.30.15 measurer-shape stable.
        noise_floor_dbfs_estimate=0.0,
        capture_callback_p99_ms=0.0,
        capture_jitter_ms=0.0,
        portaudio_latency_advertised_ms=0.0,
        mixer_card_index=mixer.get("card_index"),
        mixer_capture_pct=mixer.get("capture_pct"),
        mixer_boost_pct=mixer.get("boost_pct"),
        mixer_internal_mic_boost_pct=mixer.get("internal_mic_boost_pct"),
        mixer_attenuation_regime=_classify_mixer_regime(mixer),
        echo_correlation_db=None,
        triage_winner_hid=triage_hid,
        triage_winner_confidence=triage_conf,
    )


# ====================================================================
# Mixer state via amixer
# ====================================================================


def _read_mixer_state() -> dict[str, int | None]:
    """Parse ``amixer scontents`` for Capture + Mic Boost + Internal Mic Boost.

    Returns a dict with keys ``card_index``, ``capture_pct``,
    ``boost_pct``, ``internal_mic_boost_pct``. Missing controls are
    reported as ``None`` so the regime classifier can distinguish
    "not present" from "0%".
    """
    if shutil.which("amixer") is None:
        return {
            "card_index": None,
            "capture_pct": None,
            "boost_pct": None,
            "internal_mic_boost_pct": None,
        }
    output = _safe_amixer(["amixer", "-c", "0", "scontents"])
    if not output:
        return {
            "card_index": None,
            "capture_pct": None,
            "boost_pct": None,
            "internal_mic_boost_pct": None,
        }
    return {
        "card_index": 0,
        "capture_pct": _extract_first_pct(output, _CAPTURE_PATTERNS),
        "boost_pct": _extract_first_pct(output, _BOOST_PATTERNS),
        "internal_mic_boost_pct": _extract_first_pct(output, _INTERNAL_MIC_BOOST_PATTERNS),
    }


def _extract_first_pct(amixer_output: str, name_patterns: tuple[str, ...]) -> int | None:
    """Find the first simple-control matching name_patterns; return its %.

    The amixer scontents output is structured as::

        Simple mixer control 'Capture',0
          Capabilities: ...
          Front Left: Capture 53 [66%] [-13.50dB] [on]
          Front Right: Capture 53 [66%] [-13.50dB] [on]

    We scan for ``Simple mixer control 'NAME',`` blocks, match against
    the patterns, then extract the first ``[NN%]`` token within that
    block. Returns ``None`` when no matching control is found.
    """
    blocks = re.split(r"Simple mixer control '([^']+)',\d+", amixer_output)
    # Split returns: [pre, name1, body1, name2, body2, ...]
    for i in range(1, len(blocks) - 1, 2):
        name = blocks[i]
        body = blocks[i + 1]
        for pattern in name_patterns:
            if pattern in name:
                pct_match = re.search(r"\[(\d+)%\]", body)
                if pct_match is not None:
                    return int(pct_match.group(1))
                return None
    return None


def _classify_mixer_regime(mixer: dict[str, int | None]) -> str | None:
    """Classify the mixer state into ``saturated`` | ``attenuated`` | ``healthy``.

    Uses the three R10-relevant controls (Capture, Mic Boost, Internal
    Mic Boost). The classification is deterministic + threshold-based:

    * **attenuated**: Capture <= 10% AND any boost control <= 10%.
      The H10 canonical case (Sony VAIO) hits this branch.
    * **saturated**: Capture >= 90% AND any boost control >= 90%.
    * **healthy**: anything else.

    Returns ``None`` when no mixer state is available (e.g.
    ``amixer`` missing, non-Linux host) so the rule's
    ``mixer_attenuation_regime != "attenuated"`` gate refuses to fire.
    """
    capture = mixer.get("capture_pct")
    boost = mixer.get("boost_pct")
    internal = mixer.get("internal_mic_boost_pct")

    # If we have no usable signals, return None so the rule gate fails.
    has_signal = capture is not None or boost is not None or internal is not None
    if not has_signal:
        return None

    # Attenuated: capture is low AND at least one boost is low.
    cap_low = capture is not None and capture <= _ATTENUATED_THRESHOLD_PCT
    boost_low = boost is not None and boost <= _ATTENUATED_THRESHOLD_PCT
    internal_low = internal is not None and internal <= _ATTENUATED_THRESHOLD_PCT
    if cap_low and (boost_low or internal_low):
        return "attenuated"

    # Saturated: capture is high AND at least one boost is high.
    cap_high = capture is not None and capture >= _SATURATED_THRESHOLD_PCT
    boost_high = boost is not None and boost >= _SATURATED_THRESHOLD_PCT
    internal_high = internal is not None and internal >= _SATURATED_THRESHOLD_PCT
    if cap_high and (boost_high or internal_high):
        return "saturated"

    return "healthy"


# ====================================================================
# Diag tarball extraction (W/K capture analysis.json)
# ====================================================================


def _extract_capture_metrics(
    tarball_root: Path | None,
) -> tuple[tuple[float, ...], float, float]:
    """Extract per-capture RMS dBFS + max/p99 VAD probability from the diag tarball.

    Walks ``<tarball_root>/E_portaudio/captures/*/analysis.json`` and
    reads the ``rms_dbfs`` field; walks the sibling ``silero.json``
    for ``max_prob``. Returns ``((), 0.0, 0.0)`` on missing tarball
    or empty captures.

    Args:
        tarball_root: The extracted root of a diag tarball, or None.

    Returns:
        A tuple of ``(rms_dbfs_per_capture, vad_max, vad_p99)``.
    """
    if tarball_root is None or not tarball_root.is_dir():
        return ((), 0.0, 0.0)

    rms_values: list[float] = []
    vad_max_values: list[float] = []
    captures_dir = tarball_root / "E_portaudio" / "captures"
    if not captures_dir.is_dir():
        return ((), 0.0, 0.0)

    for capture_dir in sorted(captures_dir.iterdir()):
        if not capture_dir.is_dir():
            continue
        analysis_path = capture_dir / "analysis.json"
        if analysis_path.is_file():
            with contextlib.suppress(Exception):
                data = json.loads(analysis_path.read_text(encoding="utf-8"))
                rms = data.get("rms_dbfs")
                if isinstance(rms, (int, float)):
                    rms_values.append(float(rms))
        silero_path = capture_dir / "silero.json"
        if silero_path.is_file():
            with contextlib.suppress(Exception):
                data = json.loads(silero_path.read_text(encoding="utf-8"))
                max_prob = data.get("max_prob")
                if isinstance(max_prob, (int, float)):
                    vad_max_values.append(float(max_prob))

    vad_max = max(vad_max_values) if vad_max_values else 0.0
    # p99 with small samples: same as max for n < 100. We use the
    # 99th-percentile element of the sorted list (or max for n < 100).
    if vad_max_values:
        sorted_vad = sorted(vad_max_values)
        p99_idx = max(0, int(len(sorted_vad) * 0.99) - 1)
        vad_p99 = sorted_vad[p99_idx]
    else:
        vad_p99 = 0.0

    return (tuple(rms_values), vad_max, vad_p99)


# ====================================================================
# Subprocess helper (amixer-specific)
# ====================================================================


def _safe_amixer(cmd: list[str]) -> str:
    """Run an amixer command with bounded timeout; return stdout or ``""``."""
    try:
        completed = subprocess.run(  # noqa: S603 -- command is hardcoded by callers
            cmd,
            capture_output=True,
            text=True,
            timeout=_AMIXER_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        with contextlib.suppress(Exception):
            logger.debug(
                "voice.calibration.measurer.amixer_failed",
                cmd=" ".join(cmd),
                reason=type(exc).__name__,
            )
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout
