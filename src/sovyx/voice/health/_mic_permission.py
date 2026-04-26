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
        """Operator-actionable remediation message ready for emit.

        OS-aware: routes to the matching Settings / System
        Preferences path so the user navigates ONE click instead of
        Googling "how to fix mic permission". Detected via
        ``sys.platform`` at access time so the same dataclass works
        across OSes without per-construction branching."""
        import sys as _sys

        if self.status is MicPermissionStatus.GRANTED:
            return ""
        if self.status is MicPermissionStatus.DENIED:
            if _sys.platform == "darwin":
                # MA2: macOS TCC. The exact path:
                # System Settings → Privacy & Security → Microphone.
                return (
                    "macOS is blocking microphone access for Sovyx. "
                    "Open System Settings → Privacy & Security → "
                    "Microphone, enable Sovyx (or your Terminal / IDE "
                    "if running Sovyx from one), then restart Sovyx. "
                    "If Sovyx isn't listed, the OS will prompt on the "
                    "next capture attempt — accept the prompt."
                )
            # Windows + others.
            return (
                "Windows is blocking microphone access for desktop apps. "
                "Open Settings → Privacy & security → Microphone, ensure "
                "'Microphone access' is On AND 'Let desktop apps access "
                "your microphone' is On, then restart Sovyx."
            )
        # UNKNOWN — don't presume which OS; surface the generic hint.
        if _sys.platform == "darwin":
            return (
                "Microphone permission state could not be determined "
                "(TCC.db unreadable — likely needs Full Disk Access for "
                "the Terminal / IDE running Sovyx). If audio capture is "
                "silent, check System Settings → Privacy & Security → "
                "Microphone manually."
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
        return _check_macos()
    if sys.platform != "win32":
        return MicPermissionReport(
            status=MicPermissionStatus.UNKNOWN,
            notes=(f"unsupported platform: {sys.platform}",),
        )
    return _check_windows()


def _check_macos() -> MicPermissionReport:
    """macOS-only TCC consent probe (MA2).

    Reads ``~/Library/Application Support/com.apple.TCC/TCC.db`` via
    sqlite3 and translates the ``kTCCServiceMicrophone`` row's
    auth_value into a :class:`MicPermissionStatus`. Wraps the
    underlying probe so this module's per-OS dispatch stays uniform
    in shape with ``_check_windows``."""
    try:
        from sovyx.voice.health._mic_permission_mac import (
            auth_value_to_status_token,
            query_macos_microphone_permission,
        )

        auth_value, probe_notes = query_macos_microphone_permission()
    except Exception as exc:  # noqa: BLE001 — probe boundary
        # The TCC reader itself crashed (unexpected). Don't propagate
        # — collapse into UNKNOWN with a structured note so the
        # cascade's other layers still get to act.
        logger.warning(
            "voice.mic_permission.tcc_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return MicPermissionReport(
            status=MicPermissionStatus.UNKNOWN,
            notes=(f"TCC probe crashed: {exc!r}",),
        )

    token = auth_value_to_status_token(auth_value)
    if token == "granted":
        status = MicPermissionStatus.GRANTED
    elif token == "denied":
        status = MicPermissionStatus.DENIED
    else:
        status = MicPermissionStatus.UNKNOWN

    return MicPermissionReport(
        status=status,
        machine_value=None,  # macOS doesn't have HKLM-equivalent.
        user_value=str(auth_value) if auth_value is not None else None,
        notes=tuple(probe_notes),
    )


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
    # HKEY_LOCAL_MACHINE / HKEY_CURRENT_USER are Windows-only
    # constants; mypy on Linux/macOS doesn't see them as attributes
    # of the winreg module (which is conditionally importable).
    machine_value = _read_consent(
        winreg,
        winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined,unused-ignore]
        scope="HKLM",
        notes=notes,
    )
    user_value = _read_consent(
        winreg,
        winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined,unused-ignore]
        scope="HKCU",
        notes=notes,
    )

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
