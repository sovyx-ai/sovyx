"""Windows Group Policy detection for voice-affecting policies (T5.46 + T5.47).

Enterprise Windows fleets often deploy Group Policy (GP) settings
that restrict the audio APIs Sovyx relies on. The two policies
that materially affect Sovyx are:

* ``DisallowExclusiveDevice`` (HKLM Software/Microsoft/Windows/
  CurrentVersion/Policies/system/) — when set to 1, no app can
  open an audio endpoint in WASAPI exclusive mode. This kills
  Sovyx's Tier 3 bypass strategy outright; Tier 1 / Tier 2
  remain available.
* ``LimitDevicesToCallSpace`` (same path, future) — limits
  capture device exposure based on call-space rules. Detected
  for telemetry; remediation is "ask the IT admin".

Without GP detection, Sovyx would attempt Tier 3 on every
deaf-signal incident and fail with cryptic PortAudio error
``-9988``. With detection at boot, the operator sees a clear
"Group Policy blocks exclusive mode" message + Tier 3 stays
gracefully disabled.

Design notes (mirrors :mod:`._apo_detector`):

* **Windows-only.** On Linux/macOS this module imports cleanly
  but :func:`detect_group_policies` returns an empty
  :class:`GroupPolicySnapshot` (``platform_supported=False``).
  ``winreg`` is stdlib and Windows-only; the import is guarded.
* **Read-only.** We never write to GP keys. Modifying GP from a
  user-space app is forbidden by Windows + most fleet
  administrators.
* **Best-effort.** A missing GP key is the common case (no
  policies set); we treat it as "no restrictions" rather than
  "error". The ``probe_failure_reason`` field surfaces real
  failures (registry corruption, permission denial) for
  observability without breaking the boot.
* **Low-cardinality output.** Just two booleans + the failure
  reason string. Operators don't need every GP value; they need
  to know "is exclusive mode allowed".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

_POLICY_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\system"
"""HKLM root containing the audio-affecting policies."""

_DISALLOW_EXCLUSIVE_DEVICE = "DisallowExclusiveDevice"
_LIMIT_DEVICES_TO_CALL_SPACE = "LimitDevicesToCallSpace"


@dataclass(frozen=True, slots=True)
class GroupPolicySnapshot:
    """Snapshot of voice-affecting Windows Group Policy settings.

    All fields default to "no restriction" so a non-Windows host
    or a registry-read failure presents as "exclusive mode
    permitted, no telemetry restrictions" — i.e. Sovyx's normal
    operating mode.
    """

    platform_supported: bool = False
    """``True`` only on ``sys.platform == "win32"``. Linux + macOS
    callers receive a False snapshot from
    :func:`detect_group_policies` and can short-circuit."""

    exclusive_mode_disallowed: bool = False
    """``DisallowExclusiveDevice == 1``. When True, Tier 3
    (``capture_wasapi_exclusive``) MUST NOT be attempted — the
    OS will reject every open with PortAudio error -9988."""

    devices_limited_to_call_space: bool = False
    """``LimitDevicesToCallSpace == 1``. Restricts which capture
    devices Sovyx can enumerate. Currently informational only —
    the cascade walks whatever PortAudio sees."""

    raw_values: dict[str, int | None] = field(default_factory=dict)
    """Raw integer values read from the registry. Useful for
    forensics when the booleans don't match operator expectations
    (e.g. a policy set to 0 explicitly vs absent entirely)."""

    probe_failure_reason: str | None = None
    """Non-None when the probe encountered a real error (not
    "key absent"). Common values:
    ``"permission_denied"`` / ``"registry_unavailable"`` /
    ``"unexpected_value_type"``. Operators can grep for this
    field to distinguish "no GP set" from "GP probe broken"."""


def detect_group_policies() -> GroupPolicySnapshot:
    """Read voice-affecting GP keys; return a snapshot.

    Always-safe: the function NEVER raises. Non-Windows hosts get
    a default-empty snapshot with ``platform_supported=False``;
    Windows hosts with no policies set get the same shape with
    ``platform_supported=True`` (so dashboards can distinguish
    "checked, none found" from "couldn't check").

    Returns:
        :class:`GroupPolicySnapshot` carrying the two main
        booleans + raw values + an optional failure reason for
        observability.
    """
    if sys.platform != "win32":
        return GroupPolicySnapshot(platform_supported=False)

    try:
        import winreg  # noqa: PLC0415 — Windows-only stdlib import
    except ImportError:
        # Should not happen on Windows; surface as a probe failure
        # so the dashboard can flag the diagnostic gap.
        logger.debug("voice.gp.winreg_import_failed")
        return GroupPolicySnapshot(
            platform_supported=True,
            probe_failure_reason="registry_unavailable",
        )

    raw_values: dict[str, int | None] = {}
    failure_reason: str | None = None

    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _POLICY_ROOT,
            0,
            winreg.KEY_READ,
        )
    except FileNotFoundError:
        # Policy root absent → no policies set. This is the
        # common case on home / non-domain-joined machines.
        return GroupPolicySnapshot(
            platform_supported=True,
            raw_values={
                _DISALLOW_EXCLUSIVE_DEVICE: None,
                _LIMIT_DEVICES_TO_CALL_SPACE: None,
            },
        )
    except PermissionError:
        logger.debug("voice.gp.permission_denied")
        return GroupPolicySnapshot(
            platform_supported=True,
            probe_failure_reason="permission_denied",
        )
    except OSError as exc:
        logger.debug("voice.gp.open_key_failed", error=str(exc))
        return GroupPolicySnapshot(
            platform_supported=True,
            probe_failure_reason="registry_unavailable",
        )

    try:
        for value_name in (
            _DISALLOW_EXCLUSIVE_DEVICE,
            _LIMIT_DEVICES_TO_CALL_SPACE,
        ):
            raw_values[value_name] = _read_dword(winreg, key, value_name)
    finally:
        winreg.CloseKey(key)

    if raw_values.get(_DISALLOW_EXCLUSIVE_DEVICE) is not None and not isinstance(
        raw_values.get(_DISALLOW_EXCLUSIVE_DEVICE), int
    ):
        # Defensive — _read_dword returns int|None; this branch
        # should be unreachable, but if a future Windows release
        # changes the type Sovyx wants to surface it as a
        # probe failure rather than silently misclassify.
        failure_reason = "unexpected_value_type"

    exclusive_disallowed = bool(raw_values.get(_DISALLOW_EXCLUSIVE_DEVICE) == 1)
    devices_limited = bool(raw_values.get(_LIMIT_DEVICES_TO_CALL_SPACE) == 1)

    return GroupPolicySnapshot(
        platform_supported=True,
        exclusive_mode_disallowed=exclusive_disallowed,
        devices_limited_to_call_space=devices_limited,
        raw_values=raw_values,
        probe_failure_reason=failure_reason,
    )


def _read_dword(winreg_mod: object, key: object, value_name: str) -> int | None:
    """Read a REG_DWORD value; return None on absence / wrong type.

    Defensive against the corner cases:
    * Value name absent → ``FileNotFoundError`` → return None.
    * Wrong type (e.g. someone set a REG_SZ instead of REG_DWORD
      via gpedit.msc — possible but rare) → return None and let
      the caller treat as "policy not effectively set".
    """
    try:
        value, value_type = winreg_mod.QueryValueEx(  # type: ignore[attr-defined]
            key,
            value_name,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug(
            "voice.gp.read_value_failed",
            value_name=value_name,
            error=str(exc),
        )
        return None
    if value_type != winreg_mod.REG_DWORD:  # type: ignore[attr-defined]
        return None
    if not isinstance(value, int):
        return None
    return value


def log_group_policy_snapshot(snapshot: GroupPolicySnapshot) -> None:
    """Emit a structured boot-time log describing GP findings.

    Called once from the factory's boot cascade. The log fires
    INFO when nothing is restricted, WARN when a restriction is
    detected (operators need to act on it).

    Args:
        snapshot: The snapshot from :func:`detect_group_policies`.
    """
    if not snapshot.platform_supported:
        # Non-Windows; no signal to log.
        return

    if snapshot.probe_failure_reason is not None:
        logger.warning(
            "voice.group_policy.probe_failed",
            **{
                "voice.gp.reason": snapshot.probe_failure_reason,
                "voice.gp.remediation": (
                    "Sovyx couldn't read voice-affecting Group Policy "
                    "keys. Tier 3 (WASAPI exclusive) may still work, "
                    "but pre-flight detection of GP restrictions is "
                    "disabled until the registry probe succeeds. "
                    "Common cause: low-privilege service account "
                    "without HKLM read access on policy keys."
                ),
            },
        )
        return

    if snapshot.exclusive_mode_disallowed:
        logger.warning(
            "voice.group_policy.exclusive_mode_disallowed",
            **{
                "voice.gp.exclusive_disallowed": True,
                "voice.gp.devices_limited": (snapshot.devices_limited_to_call_space),
                "voice.gp.remediation": (
                    "DisallowExclusiveDevice=1 — your Group Policy "
                    "blocks WASAPI exclusive mode. Tier 3 bypass "
                    "is disabled; expect Tier 1 (RAW) and Tier 2 "
                    "(host_api_rotate) to handle Voice Clarity-class "
                    "incidents. If those tiers don't resolve a "
                    "deaf-signal scenario on your hardware, ask "
                    "your Windows admin to override the policy "
                    "for the Sovyx process."
                ),
            },
        )
        return

    logger.info(
        "voice.group_policy.no_restrictions",
        **{
            "voice.gp.exclusive_disallowed": False,
            "voice.gp.devices_limited": (snapshot.devices_limited_to_call_space),
        },
    )
