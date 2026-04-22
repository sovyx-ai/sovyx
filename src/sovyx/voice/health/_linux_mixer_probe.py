"""Linux ALSA mixer introspection — enumerate cards + gain controls.

Stateless read-only probe. Consumed by:

* :mod:`sovyx.voice.health._linux_mixer_apply` — apply + rollback.
* :mod:`sovyx.voice.health.bypass._linux_alsa_mixer` — eligibility +
  card-match logic for the :class:`LinuxALSAMixerResetBypass` strategy.
* :mod:`sovyx.voice.health._linux_mixer_check` — ``sovyx doctor voice``
  preflight.
* :mod:`sovyx.dashboard.routes.voice` — ``GET /api/voice/linux-mixer-diagnostics``
  endpoint.

Zero OS dependency beyond:

* ``sys.platform == 'linux'``
* ``shutil.which('amixer')`` — bundled with ``alsa-utils`` which is
  preinstalled on every major distro.
* ``/proc/asound/cards`` — kernel-exposed, world-readable.

All subprocess calls have a bounded timeout + ``errors="replace"`` so a
misbehaving codec driver never blocks the event loop. Missing tools,
malformed output, or a card with no mixer controls silently return
empty snapshots — the higher-level coordinator treats this as "no
Linux strategy applicable" and advances.

See ``docs-internal/plans/linux-alsa-mixer-saturation-fix.md`` §2.3.3
for the derivation.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted alsa-utils binary
import sys
from pathlib import Path

from sovyx.engine.config import VoiceTuningConfig
from sovyx.observability.logging import get_logger
from sovyx.voice.health.contract import MixerCardSnapshot, MixerControlSnapshot

logger = get_logger(__name__)


_PROC_CARDS = Path("/proc/asound/cards")
"""Kernel-exposed card index — source of truth for card enumeration."""


_BOOST_CONTROL_PATTERNS: tuple[str, ...] = (
    "Mic Boost",
    "Internal Mic Boost",
    "Front Mic Boost",
    "Rear Mic Boost",
    "Line Boost",
    "Capture",
)
"""Simple-control names the probe flags as pre-ADC gain stages.

Matched as a case-insensitive substring contains-check against the
control name. Covers amplifier-style boosts (``Mic Boost``, ``Internal
Mic Boost``) and the generic capture gain (``Capture``). The list is
intentionally small — ``Digital`` is excluded because
``Digital Playback Volume`` would false-positive as a capture-path
control, while the genuinely capture-side ``Digital Capture Volume``
already matches via the ``Capture`` substring.
"""


_CARD_HEADER_RE = re.compile(
    r"^\s*(?P<index>\d+)\s*\[(?P<id>[^\]]+)\]:\s*(?P<driver>\S+)\s*-\s*(?P<name>.+)$",
)
"""Matches the first line of each card block in ``/proc/asound/cards``.

Example::

    0 [Generic        ]: HDA-Intel - HD-Audio Generic
    1 [Generic_1      ]: HDA-Intel - HD-Audio Generic
"""


_SIMPLE_CONTROL_RE = re.compile(r"^Simple mixer control '(?P<name>[^']+)',\d+$")
"""Matches the block header of one control in ``amixer scontents`` output."""


_LIMITS_RE = re.compile(
    r"^\s*Limits:\s*(?:\w+\s+)?(?P<min>-?\d+)\s*-\s*(?P<max>-?\d+)\s*$",
)
"""Matches the ``Limits:`` line.

Tolerates the optional control-type prefix (``Capture``, ``Playback``)
that ``amixer`` inserts for controls with both playback and capture
capabilities — absent for pure-boost controls like
``Internal Mic Boost``.
"""


_CHANNEL_RE = re.compile(
    r"^\s*(?P<channel>Front Left|Front Right|Mono)\s*:\s*"
    r"(?:\w+\s+)?(?P<raw>-?\d+)"
    r"(?:\s*\[\d+%\])?"
    r"(?:\s*\[(?P<db>-?\d+(?:\.\d+)?)dB\])?",
)
"""Matches a per-channel reading line.

Examples this covers::

    Front Left: Capture 80 [100%] [6.00dB] [on]
    Front Right: 3 [100%] [36.00dB]
    Mono: Playback 70 [80%] [-12.75dB] [on]
"""


def enumerate_alsa_mixer_snapshots() -> list[MixerCardSnapshot]:
    """Return one :class:`MixerCardSnapshot` per ALSA card with a mixer.

    Completely stateless — every call re-enumerates. Empty list when:

    * Not on Linux.
    * ``amixer`` is not on ``PATH``.
    * ``/proc/asound/cards`` is missing.
    * Every card's ``amixer -c N scontents`` call fails, returns
      non-zero, or yields no parseable controls.

    Cards that enumerate but have zero controls are dropped from the
    result — a card with only ``Master`` (e.g. an HDMI-only output
    card) has no capture-path state to surface.
    """
    if sys.platform != "linux":
        return []
    if shutil.which("amixer") is None:
        return []
    if not _PROC_CARDS.exists():
        return []

    try:
        cards_text = _PROC_CARDS.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("linux_mixer_proc_cards_read_failed", detail=str(exc))
        return []

    tuning = VoiceTuningConfig()
    snapshots: list[MixerCardSnapshot] = []
    for card_index, card_id, card_longname in _parse_proc_cards(cards_text):
        controls = _probe_card_controls(card_index, tuning=tuning)
        if not controls:
            continue
        aggregated_boost_db = 0.0
        for ctl in controls:
            if ctl.is_boost_control and ctl.current_db is not None:
                aggregated_boost_db += ctl.current_db
        saturation_warning = any(c.saturation_risk for c in controls) or (
            aggregated_boost_db > tuning.linux_mixer_aggregated_boost_db_ceiling
        )
        snapshots.append(
            MixerCardSnapshot(
                card_index=card_index,
                card_id=card_id,
                card_longname=card_longname,
                controls=tuple(controls),
                aggregated_boost_db=aggregated_boost_db,
                saturation_warning=saturation_warning,
            ),
        )
    return snapshots


def _parse_proc_cards(text: str) -> list[tuple[int, str, str]]:
    """Extract ``(index, id, longname)`` from ``/proc/asound/cards``.

    The file interleaves a one-line header followed by a continuation
    line describing the hardware resource. We use the header for the
    index + short id, and prefer the continuation line for the
    longname (which is the human-readable codec name that ALSA reports
    under ``wpctl status`` and that PortAudio echoes back as
    :attr:`BypassContext.endpoint_friendly_name`).
    """
    results: list[tuple[int, str, str]] = []
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        header_match = _CARD_HEADER_RE.match(lines[idx])
        if header_match is None:
            idx += 1
            continue
        try:
            card_index = int(header_match.group("index"))
        except (ValueError, TypeError):
            idx += 1
            continue
        card_id = header_match.group("id").strip()
        short_name = header_match.group("name").strip()
        longname = short_name
        if idx + 1 < len(lines):
            second = lines[idx + 1].strip()
            if second and _CARD_HEADER_RE.match(second) is None:
                longname = second
        results.append((card_index, card_id, longname))
        idx += 2
    return results


def _probe_card_controls(
    card_index: int,
    *,
    tuning: VoiceTuningConfig,
) -> list[MixerControlSnapshot]:
    """Run ``amixer -c N scontents`` and parse its simple-control blocks.

    Returns an empty list on subprocess failure, non-zero exit, or
    fully unparseable output — the caller drops the card rather than
    surfacing a half-formed snapshot.
    """
    timeout_s = tuning.linux_mixer_subprocess_timeout_s
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, timeout enforced
            ["amixer", "-c", str(card_index), "scontents"],  # noqa: S607 — resolved via which() above
            capture_output=True,
            timeout=timeout_s,
            check=False,
            text=True,
            errors="replace",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug(
            "linux_mixer_amixer_failed",
            card_index=card_index,
            detail=str(exc),
        )
        return []
    if proc.returncode != 0:
        logger.debug(
            "linux_mixer_amixer_nonzero",
            card_index=card_index,
            returncode=proc.returncode,
            stderr=proc.stderr[:200] if proc.stderr else "",
        )
        return []

    return _parse_amixer_scontents(proc.stdout, tuning=tuning)


def _parse_amixer_scontents(
    text: str,
    *,
    tuning: VoiceTuningConfig,
) -> list[MixerControlSnapshot]:
    """Parse the scontents stream into structured snapshots.

    Each ``Simple mixer control '<name>',N`` block contributes 0 or 1
    snapshot — blocks without a readable raw value (e.g. pure boolean
    switches with no ``Limits:`` line) are dropped silently.
    """
    blocks = _split_simple_control_blocks(text)
    controls: list[MixerControlSnapshot] = []
    for name, block in blocks:
        snap = _parse_single_control(name, block, tuning=tuning)
        if snap is not None:
            controls.append(snap)
    return controls


def _split_simple_control_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Split ``amixer scontents`` output into ``(name, body_lines)`` tuples."""
    blocks: list[tuple[str, list[str]]] = []
    current_name: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        header_match = _SIMPLE_CONTROL_RE.match(line)
        if header_match is not None:
            if current_name is not None:
                blocks.append((current_name, current_body))
            current_name = header_match.group("name")
            current_body = []
        elif current_name is not None:
            current_body.append(line)
    if current_name is not None:
        blocks.append((current_name, current_body))
    return blocks


def _parse_single_control(
    name: str,
    body: list[str],
    *,
    tuning: VoiceTuningConfig,
) -> MixerControlSnapshot | None:
    """Parse one simple-control body block.

    Returns ``None`` when the block has no ``Limits:`` line or no
    readable channel value — these blocks describe boolean switches or
    enum-typed controls that the reset strategy has no business
    touching.
    """
    min_raw: int | None = None
    max_raw: int | None = None
    front_left_raw: int | None = None
    front_left_db: float | None = None
    front_right_raw: int | None = None
    mono_raw: int | None = None
    mono_db: float | None = None

    for line in body:
        limits_m = _LIMITS_RE.match(line)
        if limits_m is not None:
            try:
                min_raw = int(limits_m.group("min"))
                max_raw = int(limits_m.group("max"))
            except (ValueError, TypeError):
                continue
            continue
        ch_m = _CHANNEL_RE.match(line)
        if ch_m is None:
            continue
        channel = ch_m.group("channel")
        try:
            raw = int(ch_m.group("raw"))
        except (ValueError, TypeError):
            continue
        db_str = ch_m.group("db")
        db_val: float | None = None
        if db_str is not None:
            try:
                db_val = float(db_str)
            except ValueError:
                db_val = None
        if channel == "Front Left":
            front_left_raw = raw
            front_left_db = db_val
        elif channel == "Front Right":
            front_right_raw = raw
        elif channel == "Mono":
            mono_raw = raw
            mono_db = db_val

    if min_raw is None or max_raw is None:
        return None
    if max_raw <= min_raw:
        return None

    current_raw = front_left_raw if front_left_raw is not None else mono_raw
    current_db = front_left_db if front_left_raw is not None else mono_db
    if current_raw is None:
        return None

    # max_db is inferable only when the control currently sits at
    # max_raw — at that instant current_db IS max_db. We never probe
    # twice at different raw values because that would require a
    # mutation cycle just for dB discovery.
    max_db = current_db if current_raw == max_raw else None

    is_boost_control = _matches_boost_pattern(name)
    ratio_denominator = max_raw - min_raw
    ratio = (current_raw - min_raw) / ratio_denominator if ratio_denominator > 0 else 0.0
    saturation_risk = is_boost_control and ratio > tuning.linux_mixer_saturation_ratio_ceiling

    asymmetric = (
        front_right_raw is not None
        and front_left_raw is not None
        and front_left_raw != front_right_raw
    )

    return MixerControlSnapshot(
        name=name,
        min_raw=min_raw,
        max_raw=max_raw,
        current_raw=current_raw,
        current_db=current_db,
        max_db=max_db,
        is_boost_control=is_boost_control,
        saturation_risk=saturation_risk,
        asymmetric=asymmetric,
    )


def _matches_boost_pattern(name: str) -> bool:
    """Case-insensitive contains-check against :data:`_BOOST_CONTROL_PATTERNS`."""
    lowered = name.lower()
    return any(pattern.lower() in lowered for pattern in _BOOST_CONTROL_PATTERNS)


__all__ = [
    "enumerate_alsa_mixer_snapshots",
]
