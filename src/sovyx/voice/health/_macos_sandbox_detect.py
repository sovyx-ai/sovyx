"""MA13 — macOS sandbox detection.

Mission §3.14 / MA13: a Sovyx instance distributed via the Mac App
Store (mas-distributed) runs inside Apple's App Sandbox, which
imposes additional restrictions ON TOP of TCC consent — most
relevantly, the sandbox blocks subprocess execution and limits
filesystem access. A sandboxed Sovyx with TCC granted may STILL fail
silently because the sandbox blocks the audio capture path.

This probe parses ``codesign --display --requirements -`` output to
detect the ``com.apple.security.app-sandbox`` requirement. The result
ships as an additional field on the mic-permission report so the
dashboard can render the sandbox-aware remediation hint.

Note this is a **distribution-time** check — a developer build of
Sovyx running locally is NEVER sandboxed; only Mac App Store builds
are. The probe is therefore a no-op signal in development.

Public API:

* :func:`detect_sandbox_state` — synchronous probe.
* :class:`SandboxReport` — verdict + raw codesign output.
* :class:`SandboxVerdict` — SANDBOXED / UNSANDBOXED / UNKNOWN StrEnum.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 6.c.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_CODESIGN_TIMEOUT_S = 3.0
"""``codesign`` is fast but can stall on a notarisation reachability
check; 3 s is generous headroom."""

_SANDBOX_REQUIREMENT_KEY = "com.apple.security.app-sandbox"
"""Apple's canonical requirement key emitted in the codesign
``--requirements -`` output when the binary is sandboxed.

The full requirement clause looks like:

    designated => identifier "com.example.app" and ... and entitlement
    ["com.apple.security.app-sandbox"] = true

We match on the literal string presence, not full XML parse — the
``--requirements`` output format is operator-readable (no XML by
default), and substring matching survives Apple's periodic format
revisions."""


class SandboxVerdict(StrEnum):
    """Aggregate verdict of the sandbox probe.

    StrEnum (CLAUDE.md anti-pattern #9) for stable JSON
    serialisation in dashboards.
    """

    SANDBOXED = "sandboxed"
    """Binary declares ``com.apple.security.app-sandbox`` — the
    process is running inside the App Sandbox."""

    UNSANDBOXED = "unsandboxed"
    """Binary is signed but does NOT declare the sandbox
    requirement. Standard developer / direct-distribution builds."""

    UNKNOWN = "unknown"
    """Probe couldn't reach a verdict (non-darwin, codesign absent,
    binary unsigned, subprocess error). Treat as no-information."""


@dataclass(frozen=True, slots=True)
class SandboxReport:
    """Structured outcome of :func:`detect_sandbox_state`."""

    verdict: SandboxVerdict
    """Aggregate verdict."""

    executable_path: str = ""
    """The path codesign was invoked against."""

    raw_codesign_output: str = ""
    """First 4 KB of codesign stdout. Useful for dashboards that
    show the raw requirements clause for forensic inspection."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes."""

    @property
    def remediation_hint(self) -> str:
        """Operator-actionable message for the dashboard.

        SANDBOXED is NOT an error condition — it's a constraint. The
        hint explains the implications so an operator hitting silent
        capture failures has the right diagnostic frame.
        """
        if self.verdict is SandboxVerdict.UNSANDBOXED:
            return ""  # Standard build — no sandbox to worry about.
        if self.verdict is SandboxVerdict.SANDBOXED:
            return (
                "Sovyx is running in the macOS App Sandbox. Ensure "
                "the bundle declares the audio entitlements in its "
                "embedded.provisionprofile + .entitlements. The "
                "sandbox blocks ``subprocess`` calls; if Sovyx "
                "internally relies on ``system_profiler`` / ``sc.exe`` "
                "/ ``codesign``, those probes fail silently here. "
                "Mac App Store distribution typically requires the "
                "non-sandboxed direct-distribution build for a "
                "voice agent."
            )
        return (
            "Sandbox state could not be determined. Run "
            "``codesign --display --requirements - $(which python3)`` "
            "to see the active requirements clause."
        )


def detect_sandbox_state(*, executable: str | None = None) -> SandboxReport:
    """Synchronous sandbox probe.

    Args:
        executable: Override the path to inspect. ``None`` defaults to
            :data:`sys.executable` (the running interpreter or .app
            bundle binary).

    Returns:
        :class:`SandboxReport`. Never raises — non-darwin / unsigned
        / subprocess failures collapse into UNKNOWN with notes.
    """
    if sys.platform != "darwin":
        return SandboxReport(
            verdict=SandboxVerdict.UNKNOWN,
            notes=(f"non-darwin platform: {sys.platform}",),
        )

    target = executable or sys.executable
    if not target:
        return SandboxReport(
            verdict=SandboxVerdict.UNKNOWN,
            notes=("sys.executable empty — interpreter path unresolvable",),
        )

    codesign_path = shutil.which("codesign")
    if codesign_path is None:
        return SandboxReport(
            verdict=SandboxVerdict.UNKNOWN,
            executable_path=target,
            notes=("codesign binary not found on PATH",),
        )

    try:
        result = subprocess.run(  # noqa: S603 — codesign_path from shutil.which, args fixed
            (codesign_path, "--display", "--requirements", "-", target),
            capture_output=True,
            text=True,
            timeout=_CODESIGN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SandboxReport(
            verdict=SandboxVerdict.UNKNOWN,
            executable_path=target,
            notes=(f"codesign timed out after {_CODESIGN_TIMEOUT_S}s",),
        )
    except OSError as exc:
        return SandboxReport(
            verdict=SandboxVerdict.UNKNOWN,
            executable_path=target,
            notes=(f"codesign spawn failed: {exc!r}",),
        )

    # codesign writes the requirements clause to STDERR (not stdout).
    # Apple's docs are specific: "The entitlements are written to
    # standard error" — we read both for robustness against future
    # output-channel changes.
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    raw_truncated = output[:4096]

    if result.returncode != 0:
        # Common case: unsigned binary (returncode 1) — operator is
        # running an unsigned interpreter. Treat as UNKNOWN with a
        # specific note rather than UNSANDBOXED (we can't tell either
        # way without a signature).
        return SandboxReport(
            verdict=SandboxVerdict.UNKNOWN,
            executable_path=target,
            raw_codesign_output=raw_truncated,
            notes=(f"codesign exited {result.returncode}: {result.stderr.strip()[:120]}",),
        )

    if _SANDBOX_REQUIREMENT_KEY in output:
        return SandboxReport(
            verdict=SandboxVerdict.SANDBOXED,
            executable_path=target,
            raw_codesign_output=raw_truncated,
        )
    return SandboxReport(
        verdict=SandboxVerdict.UNSANDBOXED,
        executable_path=target,
        raw_codesign_output=raw_truncated,
    )


__all__ = [
    "SandboxReport",
    "SandboxVerdict",
    "detect_sandbox_state",
]
