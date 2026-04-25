"""ALSA Use-Case-Manager (UCM) verb selection (F4 layer 2).

Layer 2 of the F1-spec'd 4-layer Linux mixer cascade. UCM is the
ALSA-level alternative to per-codec mixer hacks: vendor-shipped
profiles describe canonical use cases (``HiFi`` for speaker output,
``VoiceCall`` for HFP / mobile telephony, ``HiFi`` with a specific
input device modifier for noise-cancelling, etc.). When a card ships
a UCM profile, selecting the right verb resolves the mixer
configuration in one operation — no per-control sset, no
hardware-specific knowledge.

Shipped UCM profile coverage (as of 2026-04):

* Most laptops sold post-2020: bundled in ``alsa-ucm-conf`` distro
  package.
* USB headsets / DACs: vendor-supplied via udev hotplug.
* Embedded ARM (Raspberry Pi, etc.): SoC manufacturers bundle.

Capabilities:

* :func:`detect_ucm` — verdict + raw evidence (alsaucm presence,
  verb list, current verb).
* :func:`enumerate_verbs` — available verbs for a card via
  ``alsaucm -c <card> list _verbs``.
* :func:`get_active_verb` — current verb via
  ``alsaucm -c <card> get _verb``.
* :func:`set_verb` — explicit routing helper that runs
  ``alsaucm -c <card> set _verb <name>`` with structured error
  attribution.

Design contract mirrors :mod:`sovyx.voice.health._pipewire`:

* Detection NEVER raises — subprocess / parse failures collapse
  into UNKNOWN with structured ``notes``.
* Setting is EXPLICIT — detection alone never mutates state.
* Bounded subprocess timeouts so a wedged ``alsaucm`` never stalls
  preflight.

Reference: F1 inventory mission task F4; ALSA UCM docs
(``man 8 alsaucm``).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted alsa-utils binary
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Bounds + tunables ─────────────────────────────────────────────


_ALSAUCM_TIMEOUT_S = 3.0
"""Wall-clock budget for any single ``alsaucm`` query. Verb listing
returns in <50 ms on a healthy system; 3 s is generous enough that
a momentarily-busy daemon doesn't false-fail while short enough
that a wedged invocation doesn't stall preflight."""


_SET_VERB_TIMEOUT_S = 5.0
"""Wall-clock budget for ``alsaucm set _verb`` — slightly larger
than detection because the kernel has to apply the per-control
sequence the verb describes."""


# ── Public types ──────────────────────────────────────────────────


class UcmStatus(StrEnum):
    """Closed-set verdict of :func:`detect_ucm`."""

    UNAVAILABLE = "unavailable"
    """``alsaucm`` binary is not on PATH. UCM-based routing is
    structurally impossible — cascade should advance to layers 3-4."""

    NO_PROFILE = "no_profile"
    """``alsaucm`` is available but the card has no UCM profile
    shipped (most pre-2020 hardware). Cascade should advance to
    layers 3-4 (KB profile + AGC2)."""

    AVAILABLE = "available"
    """Card has ≥1 verb. Layer 2 is viable; operator may invoke
    :func:`set_verb` to engage."""

    ACTIVE = "active"
    """A verb is currently set (other than the implicit default).
    UCM is already shaping the mixer — no further intervention
    needed unless the active verb is sub-optimal."""

    UNKNOWN = "unknown"
    """Detection failed (subprocess error, parse failure). Cascade
    should treat as UNAVAILABLE for routing decisions but surface
    UNKNOWN for telemetry attribution."""


@dataclass(frozen=True, slots=True)
class UcmReport:
    """Structured detection outcome.

    Carries enough detail for the cascade's verdict AND for the
    dashboard's ``GET /api/voice/status`` to surface available
    profiles + the currently-active verb."""

    status: UcmStatus
    """Aggregated verdict."""

    card_id: str
    """ALSA card identifier the report describes (e.g. ``"PCH"``,
    ``"0"``). Stored on the report so callers don't need to keep
    a parallel lookup."""

    alsaucm_available: bool = False
    """``alsaucm`` binary resolvable on PATH."""

    verbs: tuple[str, ...] = field(default_factory=tuple)
    """All UCM verbs available for the card. Empty when none are
    shipped or enumeration failed."""

    active_verb: str | None = None
    """Currently-set verb. ``None`` when no verb is set or when
    the get failed."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes (subprocess errors, parse
    fallbacks) that don't change the verdict but help operators
    trace the probe."""


class UcmRoutingError(Exception):
    """Raised when an explicit routing operation (:func:`set_verb`)
    fails. Carries structured detail for telemetry — never silently
    swallowed."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
        command: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr
        self.command = command


# ── Detection ─────────────────────────────────────────────────────


def detect_ucm(card_id: str) -> UcmReport:
    """Synchronous Layer-2 detection probe for a single ALSA card.

    Args:
        card_id: ALSA card identifier (numeric index ``"0"`` or
            symbolic ``"PCH"``). Forwarded verbatim to ``alsaucm
            -c <card_id>``.

    Returns:
        :class:`UcmReport` with verdict + evidence. Never raises.
    """
    if sys.platform != "linux":
        return UcmReport(
            status=UcmStatus.UNAVAILABLE,
            card_id=card_id,
            notes=(f"non-linux platform: {sys.platform}",),
        )

    notes: list[str] = []
    alsaucm_path = shutil.which("alsaucm")
    if alsaucm_path is None:
        notes.append("alsaucm binary not found on PATH")
        return UcmReport(
            status=UcmStatus.UNAVAILABLE,
            card_id=card_id,
            alsaucm_available=False,
            notes=tuple(notes),
        )

    verbs = _enumerate_verbs(alsaucm_path, card_id, notes)
    active = _query_active_verb(alsaucm_path, card_id, notes)

    if not verbs:
        return UcmReport(
            status=UcmStatus.NO_PROFILE,
            card_id=card_id,
            alsaucm_available=True,
            verbs=(),
            active_verb=active,
            notes=tuple(notes),
        )

    # Status: ACTIVE if a verb is set AND it's in the list; else
    # AVAILABLE (verbs exist but none active or active is unset/null).
    verdict = UcmStatus.ACTIVE if active is not None and active in verbs else UcmStatus.AVAILABLE
    return UcmReport(
        status=verdict,
        card_id=card_id,
        alsaucm_available=True,
        verbs=tuple(verbs),
        active_verb=active,
        notes=tuple(notes),
    )


def enumerate_verbs(card_id: str) -> tuple[str, ...]:
    """Standalone verb enumeration helper. Returns empty tuple on
    any failure — never raises."""
    if sys.platform != "linux":
        return ()
    alsaucm = shutil.which("alsaucm")
    if alsaucm is None:
        return ()
    return tuple(_enumerate_verbs(alsaucm, card_id, []))


def get_active_verb(card_id: str) -> str | None:
    """Standalone active-verb query. Returns None on any failure."""
    if sys.platform != "linux":
        return None
    alsaucm = shutil.which("alsaucm")
    if alsaucm is None:
        return None
    return _query_active_verb(alsaucm, card_id, [])


# ── Routing ───────────────────────────────────────────────────────


async def set_verb(card_id: str, verb: str) -> None:
    """Set the active UCM verb on a card.

    Args:
        card_id: ALSA card identifier.
        verb: Verb name (must be one of the values returned by
            :func:`enumerate_verbs`). Caller is responsible for
            validating the choice — this function does NOT pre-check
            so the operator can deliberately set an unlisted verb
            for testing if needed.

    Raises:
        UcmRoutingError: ``alsaucm`` missing, subprocess timeout,
            or non-zero exit. Carries ``returncode`` + ``stderr``
            for telemetry attribution.
    """
    alsaucm = shutil.which("alsaucm")
    if alsaucm is None:
        msg = "alsaucm binary not found on PATH; cannot set verb"
        raise UcmRoutingError(msg)
    args = (alsaucm, "-c", card_id, "set", "_verb", verb)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            args,
            capture_output=True,
            text=True,
            timeout=_SET_VERB_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"alsaucm set _verb exceeded {_SET_VERB_TIMEOUT_S} s budget"
        raise UcmRoutingError(msg, command=args, stderr=str(exc)) from exc
    if result.returncode != 0:
        msg = f"alsaucm set _verb {verb!r} on card {card_id!r} exited {result.returncode}"
        raise UcmRoutingError(
            msg,
            returncode=result.returncode,
            stderr=result.stderr.strip(),
            command=args,
        )


# ── Internal helpers ──────────────────────────────────────────────


def _enumerate_verbs(
    alsaucm_path: str,
    card_id: str,
    notes: list[str],
) -> list[str]:
    """Run ``alsaucm -c <card> list _verbs`` and parse stdout.

    Output format example::

        Available verbs:
            HiFi: Default high-fidelity playback / capture
            VoiceCall: Hands-free / mobile telephony
            HDMI: HDMI audio output

    Each verb is on its own line, name preceding the first colon.
    Returns empty list on subprocess / parse failure (caller logs
    the note via the shared ``notes`` accumulator)."""
    try:
        result = subprocess.run(
            (alsaucm_path, "-c", card_id, "list", "_verbs"),
            capture_output=True,
            text=True,
            timeout=_ALSAUCM_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        notes.append(f"alsaucm list _verbs (card={card_id}) timed out")
        return []
    except OSError as exc:
        notes.append(f"alsaucm spawn failed: {exc!r}")
        return []
    if result.returncode != 0:
        notes.append(
            f"alsaucm list _verbs exited {result.returncode}: {result.stderr.strip()[:120]}",
        )
        return []
    verbs: list[str] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        # Skip the header line + blank lines.
        if not line or line.lower().startswith("available verbs"):
            continue
        # Each verb line looks like "Name: Description".
        # Some alsaucm versions omit the colon — accept either.
        name = line.split(":", 1)[0].strip() if ":" in line else line
        if name:
            verbs.append(name)
    return verbs


def _query_active_verb(
    alsaucm_path: str,
    card_id: str,
    notes: list[str],
) -> str | None:
    """Run ``alsaucm -c <card> get _verb`` and return the parsed
    active verb, or ``None`` when none is set / probe failed."""
    try:
        result = subprocess.run(
            (alsaucm_path, "-c", card_id, "get", "_verb"),
            capture_output=True,
            text=True,
            timeout=_ALSAUCM_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        notes.append(f"alsaucm get _verb (card={card_id}) timed out")
        return None
    except OSError as exc:
        notes.append(f"alsaucm get _verb spawn failed: {exc!r}")
        return None
    if result.returncode != 0:
        notes.append(f"alsaucm get _verb exited {result.returncode}")
        return None
    # ``get _verb`` prints the verb name on stdout, possibly quoted.
    stripped = result.stdout.strip().strip('"').strip("'")
    return stripped or None


__all__ = [
    "UcmReport",
    "UcmRoutingError",
    "UcmStatus",
    "detect_ucm",
    "enumerate_verbs",
    "get_active_verb",
    "set_verb",
]
