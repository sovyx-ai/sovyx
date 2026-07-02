"""Classify PortAudio exceptions raised on exclusive-mode open (T5.48).

WASAPI exclusive mode opens fail in three failure-mode classes
that need DIFFERENT operator remediation:

* **BUSY** — another process holds the exclusive lock. Retry in
  a few seconds may succeed; the lock is application-scoped, not
  permanent. Common on machines running Discord, Skype, Teams,
  or any DAW.
* **UNSUPPORTED** — the device doesn't expose an exclusive
  endpoint, OR the requested format isn't supported in exclusive
  mode. Retry won't help; the operator must fall back to shared
  mode OR rotate to a different host API (Tier 2 ``host_api_rotate``).
* **GP_BLOCKED** — Windows Group Policy denies exclusive mode
  fleet-wide. Retry won't help either; the operator must
  coordinate with their Windows admin OR accept that Tier 3 is
  permanently disabled on this host.

Without classification, all three look like a generic
``PortAudioError -9988`` to the operator. The Phase 5 / T5.48
contract maps each class to:

1. A distinct ``ExclusiveOpenFailureClass`` enum value — consumed
   for OBSERVABILITY only: the structured log in
   :meth:`AudioCaptureTask.request_exclusive_restart`
   (``audio_capture_exclusive_restart_failed``) carries
   ``failure_class`` + ``remediation``. No production code
   branches on the enum; the Tier-3 short-circuit for GP-blocked
   fleets is driven by the BOOT-time Group Policy snapshot
   (see below), not by this per-failure classifier.
2. A different remediation message in the structured log.
3. A separate counter label so dashboards can graph the
   distribution.

Pure-string matching: PortAudio doesn't expose the WASAPI
HRESULT directly, but the exception message includes the
``AUDCLNT_E_*`` macro name (or the hex / decimal HRESULT) when
the host-API layer surfaces the underlying error. Hex / decimal
HRESULTs are FIRST-CLASS triggers (locale-neutral — on a pt-BR
Windows the prose is ``"Acesso negado"``, so English words can
never be a required secondary condition; WINDOWS-3 audit fix).

Companion to :mod:`._group_policy_detector` (T5.46): when GP is
known to deny exclusive mode at boot, Tier 3 is short-circuited
entirely — :func:`sovyx.voice.factory._capture._build_bypass_strategies`
plumbs ``GroupPolicySnapshot.exclusive_mode_disallowed`` into
:class:`~sovyx.voice.health.bypass._win_wasapi_exclusive.WindowsWASAPIExclusiveBypass`,
whose ``probe_eligibility`` then reports ineligible with reason
``gp_exclusive_disallowed``, avoiding the open + classify
roundtrip.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto


class ExclusiveOpenFailureClass(StrEnum):
    """Outcome of :func:`classify_exclusive_open_failure`.

    StrEnum (CLAUDE.md anti-pattern #9) so the value is the
    same as the name — no value-vs-name comparison footguns
    under xdist namespace duplication.
    """

    BUSY = auto()
    """Another app holds the exclusive lock. Retry MAY succeed."""

    UNSUPPORTED = auto()
    """Device has no exclusive endpoint, OR format isn't
    supported in exclusive mode. Retry won't help; operator
    must fall back to shared OR rotate host API."""

    GP_BLOCKED = auto()
    """Windows Group Policy denies exclusive mode. Retry won't
    help; operator must coordinate with Windows admin."""

    OTHER = auto()
    """Catch-all for unexpected error shapes. The
    :class:`ExclusiveOpenFailureReport.detail` field carries the
    raw exception string so dashboards can render it."""


@dataclass(frozen=True, slots=True)
class ExclusiveOpenFailureReport:
    """Classification result + remediation hint."""

    failure_class: ExclusiveOpenFailureClass
    """Which of the four classes the failure mapped to."""

    remediation: str
    """Operator-facing remediation hint, suitable for direct
    inclusion in a structured log's ``hint`` field. Always a
    single sentence (≤ 160 chars) so dashboards can render
    inline without wrapping."""

    detail: str
    """Original exception string (lowercased + truncated to
    256 chars). Useful when ``failure_class == OTHER`` for
    operators who need to inspect the raw PortAudio surface."""


# AUDCLNT_E_* macros + matching HRESULT values that signal
# "device is busy" — another exclusive client holds the lock or
# the WASAPI session is in a transient lock state.
# AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED belongs here for the ALONE
# case: the same error has two distinct causes, and without
# access-denied / policy evidence the transient-contention reading
# is the correct default (the GP-driven reading is covered by the
# evidence-gated GP_BLOCKED branch, which runs FIRST).
_BUSY_PATTERNS = (
    "audclnt_e_device_in_use",
    "audclnt_e_resource_not_available",
    "audclnt_e_exclusive_mode_not_allowed",
    "0x8889000a",  # AUDCLNT_E_DEVICE_IN_USE
    "device unavailable",
    "device is busy",
    "device or resource busy",
    "-9985",  # paDeviceUnavailable
)

# Patterns for "exclusive endpoint absent" or "format unsupported
# in exclusive". Retry won't help; fall back to shared OR rotate
# host API.
_UNSUPPORTED_PATTERNS = (
    "audclnt_e_unsupported_format",
    "0x88890008",  # AUDCLNT_E_UNSUPPORTED_FORMAT
    "-2004287480",  # decimal AUDCLNT_E_UNSUPPORTED_FORMAT
    "audclnt_e_endpoint_create_failed",
    "audclnt_e_service_not_running",
    "invalid sample rate",
    "format not supported",
    "-9988",  # paBadIODeviceCombination
    "-9996",  # paInvalidDevice
)

# Group Policy-driven blocks. Windows surfaces these as
# AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED in some cases AND the
# DisallowExclusiveDevice policy preempts the open entirely
# with E_ACCESSDENIED in others.
#
# Every pattern here is SUFFICIENT on its own (no secondary guard):
# in the exclusive-open context an access-denied HRESULT is
# GP-relevant even when the surrounding prose is localized
# ("Acesso negado" on pt-BR) or absent (bare hex from PortAudio).
# The pre-WINDOWS-3 code required an English word ("access" /
# "policy") alongside the hex, routing real GP blocks on localized
# Windows to OTHER with a useless "file an issue" remediation.
# Note AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED is deliberately NOT in
# this tuple — alone it means BUSY (see _BUSY_PATTERNS); it only
# indicates GP when PAIRED with one of these evidence patterns.
_GP_BLOCKED_PATTERNS = (
    "e_accessdenied",
    "0x80070005",  # E_ACCESSDENIED HRESULT
    "-2147024891",  # decimal E_ACCESSDENIED
    "access is denied",
    "group policy",
    "policy restricts",
)

_BUSY_REMEDIATION = (
    "Another app holds the WASAPI exclusive lock (Discord, Teams, "
    "Skype, or a DAW). Retry in a few seconds OR close the other app."
)

_UNSUPPORTED_REMEDIATION = (
    "Device has no exclusive endpoint or format isn't supported. "
    "Tier 3 won't recover here; capture falls back to shared mode — "
    "disable the vendor DSP/APO manually if audio stays degraded."
)

_GP_BLOCKED_REMEDIATION = (
    "Group Policy denies WASAPI exclusive mode. Coordinate with your "
    "Windows admin to override DisallowExclusiveDevice for the Sovyx "
    "process, OR accept that Tier 3 is disabled on this fleet."
)

_OTHER_REMEDIATION = (
    "Unrecognised PortAudio error during exclusive open. Inspect "
    "``detail`` for the raw exception text; please file an issue if "
    "this recurs across reboots."
)

_DETAIL_CHAR_BUDGET = 256


def classify_exclusive_open_failure(
    exc: BaseException,
) -> ExclusiveOpenFailureReport:
    """Map ``exc`` from a failed exclusive-mode open into a typed report.

    Pattern-matches on the lowercased exception string. The
    ``AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED`` substring has two
    distinct causes surfaced by the same Windows error, resolved
    by evidence: PAIRED with access-denied / policy evidence
    (macro, hex / decimal HRESULT, or English phrase — the
    HRESULT forms are the locale-neutral triggers) it classifies
    ``GP_BLOCKED``; ALONE it classifies ``BUSY`` (transient
    contention — another app holds the lock). The first hit wins
    per :class:`ExclusiveOpenFailureClass` priority order
    (GP_BLOCKED → BUSY → UNSUPPORTED → OTHER) so the GP-driven
    case isn't silently misclassified as transient contention.

    Args:
        exc: The exception raised by the PortAudio / sounddevice
            stream-open call. Typically ``sd.PortAudioError`` but
            the function accepts any ``BaseException`` so callers
            that wrap the failure in a custom type still get
            classified.

    Returns:
        :class:`ExclusiveOpenFailureReport` with the failure
        class, a one-sentence remediation hint, and the truncated
        original detail.
    """
    msg = str(exc).lower()

    if any(pat in msg for pat in _GP_BLOCKED_PATTERNS):
        return ExclusiveOpenFailureReport(
            failure_class=ExclusiveOpenFailureClass.GP_BLOCKED,
            remediation=_GP_BLOCKED_REMEDIATION,
            detail=msg[:_DETAIL_CHAR_BUDGET],
        )

    if any(pat in msg for pat in _BUSY_PATTERNS):
        return ExclusiveOpenFailureReport(
            failure_class=ExclusiveOpenFailureClass.BUSY,
            remediation=_BUSY_REMEDIATION,
            detail=msg[:_DETAIL_CHAR_BUDGET],
        )

    if any(pat in msg for pat in _UNSUPPORTED_PATTERNS):
        return ExclusiveOpenFailureReport(
            failure_class=ExclusiveOpenFailureClass.UNSUPPORTED,
            remediation=_UNSUPPORTED_REMEDIATION,
            detail=msg[:_DETAIL_CHAR_BUDGET],
        )

    return ExclusiveOpenFailureReport(
        failure_class=ExclusiveOpenFailureClass.OTHER,
        remediation=_OTHER_REMEDIATION,
        detail=msg[:_DETAIL_CHAR_BUDGET],
    )
