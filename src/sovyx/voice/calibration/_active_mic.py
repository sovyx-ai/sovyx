"""Resolve the operator's active mic to an ALSA card index.

v0.31.5 LE-1 closure: complete the GAP 5 wire-up. v0.31.4 added the
``active_mic_card_index`` parameter to :class:`CalibrationApplier`
and :func:`capture_measurements` but no production caller passed it;
in production every site got ``None`` → fallback ``candidates[0]`` →
R10 still boosted the wrong physical mic on multi-mic homes.

This helper bridges the gap: callers feed it the operator's persisted
:class:`MindConfig` and it returns the ALSA card index that owns the
operator's active capture device. v0.31.6 T2.1 extends the resolver
with a :command:`pactl` (PulseAudio/PipeWire) path that runs BEFORE the
``arecord -l`` fallback — PipeWire-fronted Linux setups (Mint 22,
Fedora, recent Ubuntu) report device names with Pulse-specific
suffixes (``" Wireless Analog Stereo"``) that the bracketed
``arecord`` short-name doesn't carry, so the bare-substring matcher
silently missed them.

Returns ``None`` defensively when:
- ``mind_config`` is None or has no ``voice_input_device_name`` field;
- ``voice_input_device_name`` is empty (operator hasn't completed the
  setup wizard yet);
- neither ``pactl`` nor ``arecord`` is installed (non-Linux, missing
  pulseaudio-utils + alsa-utils);
- no ALSA card name matches the persisted device name on either path.

The ``None`` return is ALWAYS safe — ``CalibrationApplier`` and
``capture_measurements`` both interpret it as "no preference", which
preserves the v0.31.3 first-attenuated-card behaviour. The new
behaviour activates only when this resolver finds a real match.

History: introduced in v0.31.5 to complete v0.31.4 GAP 5;
extended in v0.31.6 T2.1 with the ``pactl`` path for
PipeWire/PulseAudio-fronted Linux.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

from sovyx.observability.logging import get_logger
from sovyx.voice._tool_env import linux_tool_env

logger = get_logger(__name__)


_ARECORD_TIMEOUT_S = 5.0
_PACTL_TIMEOUT_S = 5.0

# Each ``arecord -l`` line for a card looks like:
#   ``card 2: Pro [Razer BlackShark V2 Pro], device 0: USB Audio [USB Audio]``
# Capture (a) the integer card index and (b) the bracketed display
# name. Match is case-insensitive on the bracketed name only — the
# card prefix (``"Pro "`` above) is just an ALSA short-id and is not
# what the operator's wizard persisted.
_CARD_LINE_RE = re.compile(
    r"^card\s+(?P<index>\d+):\s+\S+\s+\[(?P<name>[^\]]+)\]",
    re.IGNORECASE | re.MULTILINE,
)

# Pulse exposes one verbose ``Source #N`` block per source; we need the
# ``Description:`` line and the ``alsa.card`` property nested under
# ``Properties:``. Blocks are split on the ``Source #`` marker rather
# than parsed via a multi-line regex (avoids catastrophic backtracking
# on long source lists).
_PACTL_SOURCE_SPLIT_RE = re.compile(r"^Source #\d+\s*$", re.MULTILINE)
_PACTL_DESCRIPTION_RE = re.compile(r"^\s*Description:\s*(?P<desc>.+?)\s*$", re.MULTILINE)
_PACTL_ALSA_CARD_RE = re.compile(r'^\s*alsa\.card\s*=\s*"(?P<index>\d+)"', re.MULTILINE)

# Pulse-specific suffixes that decorate friendly names but aren't
# present on the underlying ALSA short-name. Stripped case-insensitively
# in the matcher so substring comparison works on PipeWire-fronted hosts.
_PULSE_SUFFIX_RE = re.compile(
    r"\s*(?:analog\s+stereo|analog\s+mono|stereo|mono"
    r"|:\s*usb\s+audio\s*\(hw:\d+,\d+\))\s*$",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_device_name(name: str) -> str:
    """Strip Pulse-specific suffixes + lowercase + collapse whitespace.

    Used by both the ``pactl`` path and the ``arecord`` path so the
    substring matcher works uniformly across PipeWire/PulseAudio
    descriptions and bare ALSA short-names.
    """
    # Strip suffixes greedily — chains like " Analog Stereo" reduce
    # via repeated single-suffix removal until no further match.
    stripped = name
    while True:
        new = _PULSE_SUFFIX_RE.sub("", stripped)
        if new == stripped:
            break
        stripped = new
    return _WHITESPACE_RE.sub(" ", stripped).strip().lower()


def _resolve_via_pactl(persisted_name: str) -> int | None:
    """Try the pactl path: parse ``pactl list sources`` for ``alsa.card``.

    Returns the ALSA card index when a source's ``Description`` (after
    normalisation) substring-matches the normalised ``persisted_name``.
    Returns ``None`` when pactl is unavailable, fails, times out, or
    produces no match — every ``None`` exit emits a structured
    ``voice.calibration.active_mic_unresolved`` log so the caller can
    fall through to the arecord path with telemetry intact.
    """
    if shutil.which("pactl") is None:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="pactl_unavailable",
        )
        return None
    try:
        completed = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
            # LINUX-6: pin the locale so ``Description:`` / property
            # labels stay English-parseable on pt_BR/de/fr desktops.
            env=linux_tool_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="pactl_failed",
            detail=str(exc)[:200],
        )
        return None
    if completed.returncode != 0:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="pactl_failed",
            exit_code=completed.returncode,
        )
        return None

    needle = _normalize_device_name(persisted_name)
    if not needle:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="pactl_no_match",
            persisted_name_hash=_short_name_hash(persisted_name),
        )
        return None

    # Split on ``Source #N`` markers; the first chunk is the optional
    # preamble before any source block (usually empty) — skip it.
    chunks = _PACTL_SOURCE_SPLIT_RE.split(completed.stdout)
    for body in chunks[1:]:
        desc_match = _PACTL_DESCRIPTION_RE.search(body)
        card_match = _PACTL_ALSA_CARD_RE.search(body)
        if desc_match is None or card_match is None:
            continue
        desc_norm = _normalize_device_name(desc_match.group("desc"))
        if not desc_norm:
            continue
        if needle in desc_norm or desc_norm in needle:
            card_index = int(card_match.group("index"))
            logger.info(
                "voice.calibration.active_mic_resolved",
                backend="pactl",
                card_index=card_index,
            )
            return card_index

    logger.info(
        "voice.calibration.active_mic_unresolved",
        reason="pactl_no_match",
        persisted_name_hash=_short_name_hash(persisted_name),
    )
    return None


def resolve_active_mic_card(*, mind_config: Any) -> int | None:  # noqa: ANN401
    """Map ``MindConfig.voice_input_device_name`` to an ALSA card index.

    Blocking (``pactl`` / ``arecord`` subprocesses, up to 5 s timeout
    each) — async callers MUST wrap this in :func:`asyncio.to_thread`
    per anti-pattern #14 (the wizard orchestrator does).

    Args:
        mind_config: The mind whose persisted mic to resolve. May be
            ``None`` (CLI doctor invocations without mind context);
            return ``None`` defensively in that case.

    Returns:
        The integer ALSA card index whose name matches the operator's
        persisted ``voice_input_device_name`` (substring match on a
        normalised form, case-insensitive), or ``None`` when the
        mapping cannot be established. Callers MUST treat ``None`` as
        "no preference" and preserve their pre-v0.31.4 fallback
        behaviour.

    Side effects:
        Emits structured ``voice.calibration.active_mic_resolved`` log
        on success (with ``backend`` and ``card_index``) and
        ``voice.calibration.active_mic_unresolved`` on every fallback
        path (closed-enum ``reason``: no_mind_config /
        no_persisted_name / pactl_unavailable / pactl_failed /
        pactl_no_match / arecord_unavailable / arecord_failed /
        arecord_nonzero_exit / no_match).
    """
    if mind_config is None:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="no_mind_config",
        )
        return None
    persisted_name = getattr(mind_config, "voice_input_device_name", "") or ""
    if not persisted_name.strip():
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="no_persisted_name",
        )
        return None

    # Try pactl FIRST — works on both PulseAudio and PipeWire (via the
    # ``pipewire-pulse`` shim) and exposes the friendly Description
    # that ``sounddevice`` reports to the wizard. Falls through on any
    # failure so non-Pulse hosts still get the bare-arecord path.
    pactl_card = _resolve_via_pactl(persisted_name)
    if pactl_card is not None:
        return pactl_card

    if shutil.which("arecord") is None:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="arecord_unavailable",
        )
        return None
    try:
        completed = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=_ARECORD_TIMEOUT_S,
            check=False,
            # LINUX-6: LC_ALL=C keeps the ``card N: ...`` listing in
            # the English shape the card-line regex matches.
            env=linux_tool_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="arecord_failed",
            detail=str(exc)[:200],
        )
        return None
    if completed.returncode != 0:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="arecord_nonzero_exit",
            exit_code=completed.returncode,
        )
        return None

    # Normalised substring match against each card's bracketed display
    # name. Bidirectional check so operator's "Razer" matches
    # "Razer BlackShark V2 Pro" AND vice versa.
    needle = _normalize_device_name(persisted_name)
    for match in _CARD_LINE_RE.finditer(completed.stdout):
        card_name = _normalize_device_name(match.group("name"))
        if not card_name or not needle:
            continue
        if needle in card_name or card_name in needle:
            card_index = int(match.group("index"))
            logger.info(
                "voice.calibration.active_mic_resolved",
                backend="arecord",
                card_index=card_index,
            )
            return card_index

    logger.info(
        "voice.calibration.active_mic_unresolved",
        reason="no_match",
        persisted_name_hash=_short_name_hash(persisted_name),
    )
    return None


def _short_name_hash(value: str) -> str:
    """Stable 16-hex prefix of SHA256(value) — for log correlation
    without leaking the operator's mic name verbatim.

    Mirrors :func:`sovyx.observability.privacy.short_hash` — kept as
    a local helper so this module has no inter-package dependency
    chain (the caller chain in calibration is already deep).
    """
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
