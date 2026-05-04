"""Enumerate ALSA cards exposing at least one capture (input) PCM.

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 1 T1.3.

Forensic anchor: the user's daemon (``c:\\Users\\guipe\\Downloads\\logs_01.txt``
line 843) emitted ``voice.factory.alsa_ucm_status ucm_card_id=0`` —
card 0 on that host is HDMI-only (devices 0–3 with 0 input channels).
The actual mic is on card 1 (``HD-Audio Generic: SN6180 Analog
hw:1,0`` with 2 input channels), which the UCM probe never visited.
This module gives :func:`sovyx.voice.factory._diagnostics._maybe_log_alsa_ucm_status`
the cards-with-input list it needs to fan out the probe correctly.

The helper reads ``/proc/asound/cards`` for card metadata (mirrors the
parser at :func:`sovyx.voice.health._linux_mixer_probe._parse_proc_cards`)
and ``/proc/asound/card<N>/`` for capture-PCM presence (any entry
matching ``r"pcm\\d+c$"`` — the trailing ``c`` is ALSA's "capture"
suffix, documented at
``src/sovyx/voice/health/_fingerprint_linux.py:104``).

Pure stdlib, sub-millisecond, returns ``[]`` on non-Linux or any
filesystem error so callers can short-circuit without raising.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from sovyx.observability.logging import get_logger
from sovyx.voice.health._linux_mixer_probe import _parse_proc_cards

logger = get_logger(__name__)


_PROC_CARDS = Path("/proc/asound/cards")
"""Kernel-exposed card metadata file (same source the mixer probe uses)."""

_PROC_CARD_ROOT = Path("/proc/asound")
"""Per-card directories live under ``/proc/asound/card<N>/``."""

_CAPTURE_PCM_RE = re.compile(r"^pcm\d+c$")
"""ALSA per-card PCM directory naming: ``pcm0c`` = capture device 0,
``pcm0p`` = playback device 0. The trailing ``c`` is the capture
discriminator. A card with ANY capture PCM is "input-capable" for the
purposes of UCM probing — even if no application is currently using it."""


def enumerate_input_card_ids() -> list[tuple[int, str]]:
    """Return ``[(card_index, card_id), ...]`` for every ALSA card
    that exposes at least one capture PCM.

    Returns an empty list when:

    * the platform is not Linux,
    * ``/proc/asound/cards`` does not exist (the kernel module
      ``snd`` is unloaded or the host has no audio driver),
    * the file is unreadable for any reason,
    * no card on the host has any capture PCM.

    The ordering follows the kernel index — card 0 first, then card
    1, etc. Callers that prefer the symbolic ``card_id`` (e.g.
    ``"PCH"``) can read the second tuple element; the numeric index
    is always present and is what ``alsaucm -c <N>`` accepts as a
    fallback when the symbolic id is unknown to the UCM database.
    """
    if not sys.platform.startswith("linux"):
        return []
    if not _PROC_CARDS.exists():
        logger.debug("alsa_input_cards_proc_cards_missing")
        return []
    try:
        cards_text = _PROC_CARDS.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug(
            "alsa_input_cards_proc_cards_read_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []

    cards = _parse_proc_cards(cards_text)
    results: list[tuple[int, str]] = []
    for card_index, card_id, _longname in cards:
        if _card_has_capture_pcm(card_index):
            results.append((card_index, card_id))
    return results


def _card_has_capture_pcm(card_index: int) -> bool:
    """Return True iff ``/proc/asound/card<N>/`` contains any capture PCM.

    Capture PCMs are directory entries matching ``pcm\\d+c$``. We
    iterate the directory once and return early on the first match —
    on a typical laptop a card has 1-4 PCMs total, so the cost is
    negligible.
    """
    card_dir = _PROC_CARD_ROOT / f"card{card_index}"
    try:
        if not card_dir.is_dir():
            return False
        for entry in card_dir.iterdir():
            if _CAPTURE_PCM_RE.match(entry.name):
                return True
    except OSError as exc:
        logger.debug(
            "alsa_input_cards_card_dir_read_failed",
            card_index=card_index,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    return False
