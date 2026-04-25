"""macOS code-signing entitlement verifier (MA5).

When Sovyx eventually ships as a notarised ``.app`` bundle for
distribution outside ``pip install``, the bundle MUST declare the
microphone-access entitlement
(``com.apple.security.device.audio-input``) for capture to work
on Hardened-Runtime-enforced macOS (Catalina 10.15+). Without the
entitlement, the OS denies microphone access regardless of the
TCC user-grant state — Sovyx captures all-zero frames forever.

Pre-MA5 the cascade had no signal to attribute "Hardened Runtime
silently denied mic" specifically. Operators who grant mic access
in System Settings AND see Sovyx still capturing silence had no
hint that the binary itself was missing the entitlement.

This module ships:

* :func:`verify_microphone_entitlement` — sync probe that runs
  ``codesign -d --entitlements - <executable>`` and returns whether
  the canonical mic entitlement is present.
* :func:`current_executable_path` — resolves the path Apple's
  Hardened Runtime evaluates against (typically ``sys.executable``
  for an interpreter; an ``.app`` bundle for a notarised build).
* :class:`EntitlementReport` — structured outcome carrying the
  verdict + raw codesign output for trace observability.

Discovery method: subprocess ``codesign`` (always present on macOS
since Catalina, no third-party dependency required). Bounded
3 s timeout.

Reference: F1 inventory mission task MA5; Apple Hardened Runtime +
Notarization documentation.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted Apple binary
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Bounds ─────────────────────────────────────────────────────────


_CODESIGN_TIMEOUT_S = 3.0
"""Wall-clock budget for ``codesign -d --entitlements -``. Codesign
is fast (<100 ms) on a healthy macOS; 3 s is generous enough for
slow disks while short enough that a wedged codesign doesn't stall
the voice pipeline."""


_MIC_ENTITLEMENT_KEY = "com.apple.security.device.audio-input"
"""The canonical entitlement key Apple's Hardened Runtime checks
against for microphone access. Stable since Catalina 10.15.

Other audio-related entitlements (NOT what we check — listed for
completeness):

* ``com.apple.security.device.camera`` — camera access (different).
* ``com.apple.security.device.microphone`` — DEPRECATED; pre-Catalina
  syntax. Modern macOS ignores this; we only check audio-input.
"""


# ── Public types ──────────────────────────────────────────────────


class EntitlementVerdict(StrEnum):
    """Closed-set vocabulary for mic entitlement state."""

    PRESENT = "present"
    """The microphone entitlement is declared in the binary's
    code-signing slot. Capture should work given a TCC user-grant."""

    ABSENT = "absent"
    """Codesign succeeded but no mic entitlement declared. On
    Hardened-Runtime-enforced macOS, capture WILL fail silently
    even if TCC was granted."""

    UNSIGNED = "unsigned"
    """The binary isn't code-signed at all. macOS Gatekeeper /
    Hardened Runtime treats this as the laxest mode (typical for
    ``python`` from Homebrew / pyenv); entitlements aren't enforced
    so capture works as long as TCC grants. Not a Sovyx bug."""

    UNKNOWN = "unknown"
    """Probe failed (codesign missing, subprocess error, parse
    failure, non-darwin). Treat as inconclusive — proceed but
    surface the note for forensic attribution."""


@dataclass(frozen=True, slots=True)
class EntitlementReport:
    """Structured outcome of :func:`verify_microphone_entitlement`."""

    verdict: EntitlementVerdict
    """Aggregate verdict."""

    executable_path: str = ""
    """The path codesign was invoked against. Empty when
    ``current_executable_path()`` couldn't resolve a meaningful
    target (e.g. on non-darwin)."""

    raw_codesign_stdout: str = ""
    """First 4 KB of codesign stdout. Useful when the dashboard
    needs to show the raw entitlements XML for forensic inspection.
    Truncated to keep the report small."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes."""

    @property
    def remediation_hint(self) -> str:
        """Operator-actionable message ready for the dashboard."""
        if self.verdict is EntitlementVerdict.PRESENT:
            return ""
        if self.verdict is EntitlementVerdict.ABSENT:
            return (
                "The Sovyx binary is code-signed but does NOT declare "
                "the microphone entitlement "
                f"({_MIC_ENTITLEMENT_KEY}). On Hardened Runtime macOS "
                "this blocks capture even with TCC granted. The fix is "
                "rebuilding Sovyx with the entitlement in the .plist + "
                "re-notarising the bundle."
            )
        if self.verdict is EntitlementVerdict.UNSIGNED:
            return (
                "Sovyx is running from an unsigned interpreter (typical "
                "for python via Homebrew / pyenv). Hardened Runtime "
                "isn't enforced; capture relies on TCC grants only. "
                "Not a Sovyx defect — entitlements only matter for "
                "notarised .app bundles."
            )
        return (
            "Code-signing entitlement state could not be determined. "
            "If capture is silent despite TCC granted, check: "
            "(1) the binary is signed with the mic entitlement, OR "
            "(2) you're running an unsigned interpreter (acceptable)."
        )


# ── Probe ─────────────────────────────────────────────────────────


def current_executable_path() -> str:
    """Resolve the path Apple's Hardened Runtime evaluates against.

    For a Python interpreter this is ``sys.executable``. For an
    eventual ``.app`` bundle it would be
    ``Contents/MacOS/<binary>``. We use ``sys.executable`` directly
    because: (a) it's always correct for the current process,
    (b) ``.app`` builds will set ``sys.executable`` to the bundled
    binary anyway, (c) no special-casing needed."""
    return sys.executable


def verify_microphone_entitlement(
    *,
    executable: str | None = None,
) -> EntitlementReport:
    """Synchronous mic-entitlement probe.

    Args:
        executable: Override the path to inspect. ``None`` defaults
            to :func:`current_executable_path`.

    Returns:
        :class:`EntitlementReport` with verdict + raw codesign
        stdout (truncated to 4 KB) + diagnostic notes. Never raises.
    """
    if sys.platform != "darwin":
        return EntitlementReport(
            verdict=EntitlementVerdict.UNKNOWN,
            notes=(f"non-darwin platform: {sys.platform}",),
        )

    target = executable or current_executable_path()
    if not target:
        return EntitlementReport(
            verdict=EntitlementVerdict.UNKNOWN,
            notes=("could not resolve executable path",),
        )

    cs_path = shutil.which("codesign")
    if cs_path is None:
        return EntitlementReport(
            verdict=EntitlementVerdict.UNKNOWN,
            executable_path=target,
            notes=("codesign binary not found on PATH",),
        )

    try:
        # ``-d --entitlements -`` prints the embedded entitlements
        # XML to stdout. ``--xml`` is implicit on modern codesign.
        result = subprocess.run(
            (cs_path, "-d", "--entitlements", "-", target),
            capture_output=True,
            text=True,
            timeout=_CODESIGN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return EntitlementReport(
            verdict=EntitlementVerdict.UNKNOWN,
            executable_path=target,
            notes=("codesign timed out",),
        )
    except OSError as exc:
        return EntitlementReport(
            verdict=EntitlementVerdict.UNKNOWN,
            executable_path=target,
            notes=(f"codesign spawn failed: {exc!r}",),
        )

    raw = result.stdout[:4096]
    # codesign on UNSIGNED binaries returns code 1 with stderr
    # "code object is not signed at all".
    if result.returncode != 0:
        stderr_lower = result.stderr.lower()
        if "not signed" in stderr_lower:
            return EntitlementReport(
                verdict=EntitlementVerdict.UNSIGNED,
                executable_path=target,
                raw_codesign_stdout=raw,
                notes=("binary is not code-signed (typical for python)",),
            )
        return EntitlementReport(
            verdict=EntitlementVerdict.UNKNOWN,
            executable_path=target,
            raw_codesign_stdout=raw,
            notes=(f"codesign exited {result.returncode}: {result.stderr.strip()[:200]}",),
        )

    # Signed binary. Check if mic entitlement is present in the
    # output. Substring match — handles both XML and binary plist
    # formats codesign can emit.
    if _MIC_ENTITLEMENT_KEY in raw:
        return EntitlementReport(
            verdict=EntitlementVerdict.PRESENT,
            executable_path=target,
            raw_codesign_stdout=raw,
        )
    return EntitlementReport(
        verdict=EntitlementVerdict.ABSENT,
        executable_path=target,
        raw_codesign_stdout=raw,
        notes=(f"signed but missing entitlement {_MIC_ENTITLEMENT_KEY}",),
    )


__all__ = [
    "EntitlementReport",
    "EntitlementVerdict",
    "current_executable_path",
    "verify_microphone_entitlement",
]
