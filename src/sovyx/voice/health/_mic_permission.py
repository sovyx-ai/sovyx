"""Windows microphone-permission preflight check (band-aid #34 partial).

The OS-level "App can access your microphone" Privacy switch (Windows
10 1809+) is enforced INSIDE the Windows audio stack — when denied,
``sd.InputStream`` opens cleanly and then delivers all-zero frames
forever. The pipeline looks "running" but is structurally deaf, and
the user has no way to know whether to file a Sovyx bug or fix
Settings → Privacy & security → Microphone.

Pre-band-aid #34 the only signal was the post-open silence detector
(via ``capture_validation_min_rms_db``), which surfaces the symptom
("we got zero frames") without the cause ("the OS denied permission").

This module reads the consent store directly so the voice factory can
loud-fail at startup with concrete remediation guidance instead of
letting the deaf-capture path silently engage.

Cross-platform scope:

* **Windows** — full implementation (this module). Reads HKCU + HKLM
  consent store keys.
* **macOS** — TCC ``tccutil`` shell-out is tracked separately as MA2
  (the macOS toolkit work). Returns :data:`MicPermissionStatus.UNKNOWN`
  here.
* **Linux** — no OS-level mic-consent UI; returns
  :data:`MicPermissionStatus.GRANTED` (the OS layer is permissive,
  PulseAudio / PipeWire handle their own session ACLs separately).

Reference: F1 inventory band-aid #34; Microsoft documentation of the
Capability Access Manager (CapAM) consent store.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Registry paths (load-bearing — operators reference these in their
# own remediation runbooks, so renames are a breaking change) ─────────


_CONSENT_KEY_PATH = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone"
)
"""Subkey under HKLM (machine-wide policy) and HKCU (per-user
preference) holding the canonical microphone consent state. The
``Value`` REG_SZ is ``"Allow"`` or ``"Deny"``."""


# ── Public types ──────────────────────────────────────────────────


class MicPermissionStatus(StrEnum):
    """Closed-set verdict of the consent-store probe.

    Anti-pattern #9 — value-based comparison stays xdist-safe and
    serialises verbatim into the structured-event ``status`` field."""

    GRANTED = "granted"
    """The OS has explicitly allowed microphone access for this scope."""

    DENIED = "denied"
    """The OS has explicitly denied microphone access. Capture will
    silently produce zero frames; loud-fail at startup."""

    UNKNOWN = "unknown"
    """The probe could not determine the state (registry key missing,
    OS API unavailable, non-Windows platform without an implementation
    yet). Caller decides whether to trust the absent signal."""


@dataclass(frozen=True, slots=True)
class MicPermissionReport:
    """Structured outcome of :func:`check_microphone_permission`.

    Carries enough detail for the voice factory's loud-fail message
    AND for the dashboard's ``GET /api/voice/status`` to surface the
    underlying scope (machine-wide policy vs. per-user setting)."""

    status: MicPermissionStatus
    """Aggregated verdict — DENIED if any scope explicitly denies."""

    machine_value: str | None = None
    """Raw HKLM consent value (``"Allow"`` / ``"Deny"`` / ``None``
    when the key is absent)."""

    user_value: str | None = None
    """Raw HKCU consent value."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-scope diagnostic notes (e.g. registry-open failures) that
    don't change the verdict but help operators trace the probe."""

    @property
    def remediation_hint(self) -> str:
        """Operator-actionable remediation message ready for emit."""
        if self.status is MicPermissionStatus.GRANTED:
            return ""
        if self.status is MicPermissionStatus.DENIED:
            return (
                "Windows is blocking microphone access for desktop apps. "
                "Open Settings → Privacy & security → Microphone, ensure "
                "'Microphone access' is On AND 'Let desktop apps access "
                "your microphone' is On, then restart Sovyx."
            )
        return (
            "Microphone permission state could not be determined "
            "(registry key absent or unsupported platform). If audio "
            "capture is silent, check Settings → Privacy & security → "
            "Microphone manually."
        )


# ── Probe ─────────────────────────────────────────────────────────


def check_microphone_permission() -> MicPermissionReport:
    """Synchronous OS-aware microphone consent probe.

    Returns :class:`MicPermissionReport` describing the verdict plus
    per-scope raw values. Never raises — registry / API failures
    collapse into UNKNOWN with structured ``notes`` for tracing.

    Win10 1809+ semantics:

    * If HKLM ``Value`` is ``"Deny"`` → DENIED (machine policy
      overrides user preference; even toggling the user UI would
      not unblock capture).
    * Otherwise if HKCU ``Value`` is ``"Deny"`` → DENIED.
    * If both keys are present and ``"Allow"`` → GRANTED.
    * Anything else (key absent / unexpected value) → UNKNOWN; the
      caller (voice factory) decides whether to proceed or block.
    """
    if sys.platform == "linux":
        return MicPermissionReport(
            status=MicPermissionStatus.GRANTED,
            notes=("linux: no OS-level capture-consent gate (PulseAudio / PipeWire ACLs)",),
        )
    if sys.platform == "darwin":
        return MicPermissionReport(
            status=MicPermissionStatus.UNKNOWN,
            notes=("darwin: TCC probe deferred to MA2; treat as UNKNOWN for now",),
        )
    if sys.platform != "win32":
        return MicPermissionReport(
            status=MicPermissionStatus.UNKNOWN,
            notes=(f"unsupported platform: {sys.platform}",),
        )
    return _check_windows()


def _check_windows() -> MicPermissionReport:
    """Windows-only consent-store probe.

    Reads HKLM (machine) THEN HKCU (user). HKLM takes precedence —
    a machine policy ``Deny`` blocks even when the user toggle is On."""
    try:
        import winreg
    except ImportError:  # pragma: no cover — impossible on win32
        return MicPermissionReport(
            status=MicPermissionStatus.UNKNOWN,
            notes=("winreg import unavailable",),
        )

    notes: list[str] = []
    machine_value = _read_consent(winreg, winreg.HKEY_LOCAL_MACHINE, scope="HKLM", notes=notes)
    user_value = _read_consent(winreg, winreg.HKEY_CURRENT_USER, scope="HKCU", notes=notes)

    # HKLM machine policy wins if it explicitly denies.
    if _is_deny(machine_value):
        return MicPermissionReport(
            status=MicPermissionStatus.DENIED,
            machine_value=machine_value,
            user_value=user_value,
            notes=tuple(notes),
        )
    if _is_deny(user_value):
        return MicPermissionReport(
            status=MicPermissionStatus.DENIED,
            machine_value=machine_value,
            user_value=user_value,
            notes=tuple(notes),
        )
    # Both Allow → granted; absent / unexpected → unknown (don't
    # falsely report GRANTED on missing keys, which happens on
    # Win10 < 1809 and Server SKUs).
    if _is_allow(machine_value) and _is_allow(user_value):
        return MicPermissionReport(
            status=MicPermissionStatus.GRANTED,
            machine_value=machine_value,
            user_value=user_value,
            notes=tuple(notes),
        )
    if _is_allow(user_value) and machine_value is None:
        # No machine policy set + user opted in → granted.
        return MicPermissionReport(
            status=MicPermissionStatus.GRANTED,
            machine_value=machine_value,
            user_value=user_value,
            notes=(*notes, "machine policy absent; relying on user consent"),
        )
    return MicPermissionReport(
        status=MicPermissionStatus.UNKNOWN,
        machine_value=machine_value,
        user_value=user_value,
        notes=(*notes, "consent state ambiguous; falling back to UNKNOWN"),
    )


def _read_consent(
    winreg_mod: object,
    hive: int,
    *,
    scope: str,
    notes: list[str],
) -> str | None:
    """Open ``hive\\_CONSENT_KEY_PATH`` and read the ``Value`` REG_SZ.

    Returns ``None`` when the key or value is absent (not an error —
    Win10 < 1809 / Server SKUs don't set them). Logs structured WARN
    on unexpected OSError so registry permission anomalies are
    visible without spamming."""
    try:
        key = winreg_mod.OpenKey(hive, _CONSENT_KEY_PATH)  # type: ignore[attr-defined]
    except FileNotFoundError:
        notes.append(f"{scope}: consent key absent")
        return None
    except OSError as exc:
        notes.append(f"{scope}: open failed ({exc!r})")
        logger.warning(
            "voice.mic_permission.registry_open_failed",
            scope=scope,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    try:
        value, _ = winreg_mod.QueryValueEx(key, "Value")  # type: ignore[attr-defined]
        return str(value) if value is not None else None
    except FileNotFoundError:
        notes.append(f"{scope}: Value field absent")
        return None
    except OSError as exc:
        notes.append(f"{scope}: query failed ({exc!r})")
        return None
    finally:
        with contextlib.suppress(OSError):
            winreg_mod.CloseKey(key)  # type: ignore[attr-defined]


def _is_deny(value: str | None) -> bool:
    """``True`` only for the canonical ``"Deny"`` string (case-
    insensitive). Anything else (None, ``"Allow"``, garbage) returns
    ``False`` so a missing / corrupted value never falsely fires the
    block."""
    return value is not None and value.strip().lower() == "deny"


def _is_allow(value: str | None) -> bool:
    return value is not None and value.strip().lower() == "allow"


__all__ = [
    "MicPermissionReport",
    "MicPermissionStatus",
    "check_microphone_permission",
]
