"""Linux capture-chain APO detector (PulseAudio + PipeWire via subprocess).

Peer of :mod:`sovyx.voice._apo_detector`. Windows binds APOs at the OS
level via the MMDevices registry; Linux binds them at the session
manager level — so detection is shell-tool based, not introspective.

What we look for
================

* **PulseAudio** — ``module-echo-cancel`` loaded on the default source.
  Either WebRTC-AEC (default on most distros) or speex. Both destroy
  the raw mic signal upstream of PortAudio.

* **PipeWire** — a ``filter-chain`` node with a ``webrtc`` or
  ``echo-cancel`` plugin inline on the capture path, or the built-in
  ``libcamera.echo-cancel`` stream. On modern Fedora / Ubuntu 24.04+
  PipeWire is the default; WebRTC-AEC ships as an opt-in filter.

* **Noise suppression** — ``rnnoise`` / ``noise-suppression-for-voice``
  ladspa-chain entries. Less destructive than AEC but still a
  non-identity capture stage worth surfacing.

Detection strategy
==================

1. Try ``pactl list short modules`` (works against either PulseAudio or
   ``pipewire-pulse``). Parse ``module-echo-cancel`` / ``module-ladspa``
   / ``module-rnnoise`` entries.
2. Try ``pw-dump`` (PipeWire native). Parse for ``filter-chain`` nodes
   with ``echo-cancel`` / ``rnnoise`` labels. We use ``pw-dump`` instead
   of ``pw-cli ls`` because its JSON output is stable across
   PipeWire releases.

Every subprocess runs with a hard timeout and ``errors="replace"`` so a
missing tool / malformed output never propagates. The returned list is
empty on any failure — the caller treats that as "no APOs observed".

Design notes
============

* **Read-only.** Never run ``pactl unload-module`` or ``pw-cli s``; the
  durable fix for Linux echo-cancel lives in the cascade's ALSA ``hw:``
  attempts (ADR §4.2), which bypasses the session manager entirely.
* **Non-interactive.** All tools are invoked with ``--`` / ``-f``
  flags where available to avoid hitting the user's ``~/.config``.
* **Low output volume.** Only names that map to curated labels or
  have the ``echo-cancel`` / ``rnnoise`` substring are surfaced; raw
  module arguments are dropped.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 — short-timeout subprocess calls to trusted audio tools
import sys
from dataclasses import dataclass, field
from typing import Any

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_SUBPROCESS_TIMEOUT_S = 2.0
"""Hard wall-clock cap for each session-manager CLI call."""


_PULSE_MODULE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("module-echo-cancel", "PulseAudio Echo Cancel"),
    ("module-ladspa-sink", "PulseAudio LADSPA chain"),
    ("module-rnnoise", "PulseAudio RNNoise"),
    ("module-noise-suppression", "PulseAudio Noise Suppression"),
)
"""Substring → friendly-label catalog for PulseAudio / pipewire-pulse."""


_PIPEWIRE_NODE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("echo-cancel", "PipeWire Echo Cancel"),
    ("echocancel", "PipeWire Echo Cancel"),
    ("rnnoise", "PipeWire RNNoise"),
    ("noise-suppression", "PipeWire Noise Suppression"),
)
"""Substring → friendly-label catalog for PipeWire filter-chain nodes.

Matched against the node's ``node.name`` / ``node.description`` /
``factory.name`` fields — whichever is non-empty on the dumped node.
"""


_ECHO_CANCEL_SENTINELS: frozenset[str] = frozenset(
    {
        "PulseAudio Echo Cancel",
        "PipeWire Echo Cancel",
    },
)
"""Labels that flip :attr:`LinuxApoReport.echo_cancel_active` on.

Mirrors the Windows ``voice_clarity_active`` bit: one load-bearing
boolean for the auto-bypass decision tree.
"""


@dataclass(frozen=True, slots=True)
class LinuxApoReport:
    """Summary of the APO chain observed across the Linux audio stack.

    Unlike Windows there is no per-endpoint binding — PulseAudio /
    PipeWire modules apply to the session as a whole. So a single
    report describes the whole capture pipeline.

    Attributes:
        session_manager: ``"pulseaudio"`` | ``"pipewire"`` | ``"mixed"``
            | ``"unknown"``. The dominant daemon as observed by the
            detection tools.
        known_apos: Deduplicated friendly names recognised via
            :data:`_PULSE_MODULE_PATTERNS` or :data:`_PIPEWIRE_NODE_PATTERNS`,
            insertion order preserved.
        raw_entries: Unfiltered lines / node names observed, preserved
            for forensics. Lower-case and de-duplicated.
        echo_cancel_active: ``True`` iff any label in
            :data:`_ECHO_CANCEL_SENTINELS` is present — the bit the
            orchestrator's Linux auto-bypass heuristic keys off.
    """

    session_manager: str
    known_apos: list[str] = field(default_factory=list)
    raw_entries: list[str] = field(default_factory=list)
    echo_cancel_active: bool = False


def detect_capture_apos_linux() -> list[LinuxApoReport]:
    """Enumerate Linux capture-chain APOs across PulseAudio + PipeWire.

    Returns an empty list on non-Linux platforms or when neither
    ``pactl`` nor ``pw-dump`` is reachable. A single report is emitted
    when any signal is present; the detector collapses pactl + pw-dump
    observations into one ``LinuxApoReport`` because both tools describe
    the same underlying session.
    """
    if sys.platform != "linux":
        return []

    pulse_apos, pulse_raw = _probe_pulse_modules()
    pw_apos, pw_raw, pw_present = _probe_pipewire_nodes()

    if not pulse_apos and not pw_apos and not pulse_raw and not pw_raw:
        return []

    session_manager = _classify_session(
        pulse_present=bool(pulse_raw) or bool(pulse_apos),
        pipewire_present=pw_present,
    )

    merged: list[str] = []
    seen: set[str] = set()
    for label in (*pulse_apos, *pw_apos):
        if label not in seen:
            seen.add(label)
            merged.append(label)

    raw_merged: list[str] = []
    raw_seen: set[str] = set()
    for entry in (*pulse_raw, *pw_raw):
        low = entry.lower()
        if low not in raw_seen:
            raw_seen.add(low)
            raw_merged.append(low)

    echo_cancel = any(label in _ECHO_CANCEL_SENTINELS for label in merged)

    return [
        LinuxApoReport(
            session_manager=session_manager,
            known_apos=merged,
            raw_entries=raw_merged,
            echo_cancel_active=echo_cancel,
        ),
    ]


def _probe_pulse_modules() -> tuple[list[str], list[str]]:
    """Return ``(known_apos, raw_entries)`` observed via ``pactl``.

    Empty tuples when ``pactl`` is missing, the daemon is unreachable,
    or the output is malformed. Each non-comment line of ``pactl list
    short modules`` has the shape ``<id>\\t<name>\\t<args>`` — we only
    care about the module name.
    """
    if shutil.which("pactl") is None:
        return [], []
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, timeout enforced
            ["pactl", "list", "short", "modules"],  # noqa: S607 — resolved via which() above
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
            text=True,
            errors="replace",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("voice_apo_linux_pactl_failed", detail=str(exc))
        return [], []
    if proc.returncode != 0:
        logger.debug(
            "voice_apo_linux_pactl_nonzero",
            returncode=proc.returncode,
            stderr=proc.stderr[:200] if proc.stderr else "",
        )
        return [], []

    known: list[str] = []
    seen: set[str] = set()
    raw: list[str] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) < 2:
            continue
        module_name = parts[1].strip()
        if not module_name:
            continue
        low = module_name.lower()
        raw.append(low)
        for needle, label in _PULSE_MODULE_PATTERNS:
            if needle in low and label not in seen:
                seen.add(label)
                known.append(label)
                break
    return known, raw


def _probe_pipewire_nodes() -> tuple[list[str], list[str], bool]:
    """Return ``(known_apos, raw_entries, pipewire_present)`` observed via ``pw-dump``.

    ``pipewire_present`` is ``True`` whenever ``pw-dump`` produced
    valid JSON, even if no filter-chain was found — the caller uses it
    to classify the session manager as PipeWire-native vs PulseAudio-only.
    """
    if shutil.which("pw-dump") is None:
        return [], [], False
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, timeout enforced
            ["pw-dump"],  # noqa: S607 — resolved via which() above
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
            text=True,
            errors="replace",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("voice_apo_linux_pwdump_failed", detail=str(exc))
        return [], [], False
    if proc.returncode != 0:
        return [], [], False

    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("voice_apo_linux_pwdump_malformed", detail=str(exc))
        return [], [], False

    if not isinstance(payload, list):
        return [], [], True

    known: list[str] = []
    seen: set[str] = set()
    raw: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "PipeWire:Interface:Node":
            continue
        info_raw = entry.get("info")
        info: dict[str, Any] = info_raw if isinstance(info_raw, dict) else {}
        props_raw = info.get("props")
        props: dict[str, Any] = props_raw if isinstance(props_raw, dict) else {}
        candidate_fields = (
            props.get("node.name"),
            props.get("node.description"),
            props.get("factory.name"),
            props.get("media.class"),
        )
        for field_value in candidate_fields:
            if not isinstance(field_value, str):
                continue
            low = field_value.lower()
            if not low:
                continue
            for needle, label in _PIPEWIRE_NODE_PATTERNS:
                if needle in low:
                    if label not in seen:
                        seen.add(label)
                        known.append(label)
                    if low not in raw:
                        raw.append(low)
                    break
    return known, raw, True


def _classify_session(*, pulse_present: bool, pipewire_present: bool) -> str:
    if pipewire_present and pulse_present:
        return "mixed"
    if pipewire_present:
        return "pipewire"
    if pulse_present:
        return "pulseaudio"
    return "unknown"


__all__ = [
    "LinuxApoReport",
    "detect_capture_apos_linux",
]
