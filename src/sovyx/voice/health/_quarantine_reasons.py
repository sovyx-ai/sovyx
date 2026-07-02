"""Mission H3 SSoT — quarantine reason taxonomy + verdict/diagnosis resolvers.

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§T1.1-T1.3.

This module is the **single source of truth** for the values written to
:attr:`sovyx.voice.health._quarantine.QuarantineEntry.reason` /
:attr:`~sovyx.voice.health._quarantine.QuarantineEntry.derived_reason` /
:attr:`~sovyx.voice.health._quarantine.QuarantineEntry.resolved_reason`.
It replaces (post-STRICT) the legacy literal-default in
``capture_integrity.py`` ``_DEFAULT_QUARANTINE_REASON = "apo_degraded"``
plus the legacy 4-entry ``_VERDICT_TO_QUARANTINE_REASON`` dict and adds
the new :attr:`QuarantineReason.CAPTURE_DEAD` /
:attr:`~QuarantineReason.UNCLASSIFIED` reason values. Producers:
the coordinator layer (``capture_integrity.py`` ``_quarantine_endpoint``)
resolves via :func:`resolve_reason_from_verdict`; the cascade layer
(``cascade/_budget.py`` ``_quarantine_endpoint``, wired 2026-07-02 —
previously an AP #70 observe-only gap) resolves the terminal
:class:`Diagnosis` via :func:`resolve_reason_from_diagnosis`, which is
what makes ``CAPTURE_DEAD`` producible. Downstream consumers read the
resolved value through :func:`is_recheck_eligible` /
:func:`is_apo_class_reason` (``_kernel_invalidated_recheck`` filter +
watchdog APO recheck filter).

Anti-pattern compliance:

* #9 — :class:`QuarantineReason` is a :class:`StrEnum` (xdist-safe,
  value-based comparison, ``StrEnum`` member is its ``.value`` when
  passed where a ``str`` is expected).
* #16 — leaf module; no internal contract dependencies other than the
  ``contract`` subpackage's enums consumed lazily inside the resolvers.
* #20 — every consumer imports via the re-export in
  :mod:`sovyx.voice.health`; tests patch via
  ``patch.object(_quarantine_reasons, "resolve_reason_from_verdict")``.
* #46 — this module + the build-time Quality Gate 14 enforce the
  SSoT-resolver discipline for the quarantine acceptance gate.

Public surface:

* :class:`QuarantineReason` — 8-member canonical taxonomy.
* :data:`LEGACY_TWIN_MAP_REASONS` — 1:1 enum→str map for ADR-D14
  dual-field discipline.
* :func:`resolve_reason_from_verdict` — :class:`IntegrityVerdict` →
  :class:`QuarantineReason` (exhaustive ``match`` + ``assert_never``).
* :func:`resolve_reason_from_diagnosis` — :class:`Diagnosis` →
  :class:`QuarantineReason` (exhaustive ``match`` + ``assert_never``).
* :func:`is_apo_class_reason` — boolean classifier consumed by the
  watchdog APO recheck loop filter (Mission C1 §T1.7.b).
* :func:`is_recheck_eligible` — boolean classifier consumed by the
  kernel-invalidated cold-probe rechecker (Mission C1 §T1.7.b).
* :func:`is_lifecycle_tag` — boolean classifier permitting Gate 14
  allowlisting of lifecycle re-add literals such as
  ``"watchdog_recheck"`` / ``"factory_integration"``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final, assert_never

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sovyx.voice.health.contract import Diagnosis, IntegrityVerdict


class QuarantineReason(StrEnum):
    """Canonical quarantine-reason taxonomy (Mission H3 §0).

    Members:
        APO_DEGRADED: Capture-side DSP destroyed the signal (Windows
            Voice Clarity APO, Linux PulseAudio ``module-echo-cancel`` /
            PipeWire filter-chain, macOS Voice Isolation). The bypass-
            coordinator strategy ladder is the cure path; if exhausted,
            quarantine. Operator remediation is OS-side enhancement
            disable on Windows + module-unload / filter-chain audit on
            Linux + Voice Isolation toggle on macOS — H2's
            :attr:`voice.platform` metadata field disambiguates the
            playbook on the rendering layer.

        VAD_FRONTEND_DEAD: Mission C1 v0.44.0 verdict-router classification.
            Silero LSTM state corruption / ONNX session-state fault /
            shape mismatch at the VAD frontend. Recovery is via the
            VAD-frontend reset ladder BEFORE quarantine; quarantined
            entries are NOT cold-probe-recoverable (Silero session state
            is per-process; a fresh probe re-classifies dead). Operator
            remediation: daemon restart / model refresh /
            ``sovyx doctor voice --full-diag``.

        FORMAT_MISMATCH: Mission C1 v0.44.0 verdict. Frames reaching the
            VAD do not match the expected shape (16 kHz mono int16).
            Recovery is via :meth:`AudioCaptureTask.engage_frame_normalizer`
            BEFORE quarantine. Quarantined entries cure via OS-level
            default-input change or device replug.

        DRIVER_SILENT: Mission C1 v0.44.0 verdict. RMS near zero / flat
            DC on a *working* stream — OS driver is delivering callbacks
            but every buffer is silent. Distinct from :attr:`CAPTURE_DEAD`
            (working stream vs no signal at all). Recovery is via
            cascade re-walk; quarantined entries cure via replug, driver
            update, or OS-level mute toggle.

        CAPTURE_DEAD: **NEW in Mission H3** — terminal substrate failure:
            ``Diagnosis.NO_SIGNAL`` / ``STREAM_OPEN_TIMEOUT`` /
            ``HEARTBEAT_TIMEOUT`` exhausted across every cascade combo.
            Either zero callbacks fired at all, OR exact-zero PCM was
            delivered across every host API. No in-process cure exists;
            cure = physical replug, reboot, or OS audio-stack reset
            (``systemctl restart pulseaudio`` on Linux; Windows Audio
            service restart; ``sudo killall coreaudiod`` on macOS).
            Distinct from :attr:`DRIVER_SILENT` (working stream / RMS-
            near-zero) and :attr:`KERNEL_INVALIDATED` (open failed at
            ``IAudioClient::Initialize``).

        KERNEL_INVALIDATED: Windows kernel-side ``IAudioClient`` is in
            an invalidated state — every host API returns
            ``paInvalidDevice`` (-9996) on stream open despite the PnP
            layer reporting the device healthy. Cure is physical
            replug or reboot. Pre-mission reason value; preserved
            verbatim across Mission H3 phases.

        WATCHDOG_RECHECK: Lifecycle tag — the watchdog's recheck loop
            re-adds an entry under this reason while preserving the
            underlying ``resolved_reason``. NOT a terminal classification.
            Gate 14 allowlists this literal at the watchdog re-add site
            via ``# h3-allowlist: lifecycle-tag``.

        UNCLASSIFIED: **NEW in Mission H3** — fallback for any verdict /
            diagnosis that lands without a paired map entry. Gate 14
            mechanically prevents this from shipping (the exhaustive
            ``match`` + ``assert_never`` in
            :func:`resolve_reason_from_verdict` /
            :func:`resolve_reason_from_diagnosis` fails type-check on a
            new enum member without a paired ``case`` arm). Better to
            surface ``"unclassified"`` than lie with ``"apo_degraded"``
            if a Gate 14 bypass ever happens.
    """

    APO_DEGRADED = "apo_degraded"
    VAD_FRONTEND_DEAD = "vad_frontend_dead"
    FORMAT_MISMATCH = "format_mismatch"
    DRIVER_SILENT = "driver_silent"
    CAPTURE_DEAD = "capture_dead"
    KERNEL_INVALIDATED = "kernel_invalidated"
    WATCHDOG_RECHECK = "watchdog_recheck"
    UNCLASSIFIED = "unclassified"


LEGACY_TWIN_MAP_REASONS: Final[Mapping[QuarantineReason, str]] = {
    QuarantineReason.APO_DEGRADED: "apo_degraded",
    QuarantineReason.VAD_FRONTEND_DEAD: "vad_frontend_dead",
    QuarantineReason.FORMAT_MISMATCH: "format_mismatch",
    QuarantineReason.DRIVER_SILENT: "driver_silent",
    QuarantineReason.CAPTURE_DEAD: "capture_dead",
    QuarantineReason.KERNEL_INVALIDATED: "kernel_invalidated",
    QuarantineReason.WATCHDOG_RECHECK: "watchdog_recheck",
    QuarantineReason.UNCLASSIFIED: "unclassified",
}
"""Mission H3 ADR-D14 — enum → legacy string-literal twin map.

Every :class:`QuarantineReason` member maps to its pre-mission string
literal (the value itself for the four pre-existing values; new H3
values map to themselves). Preserved through Phase 3 STRICT so any
future taxonomy migration can locate the legacy form for backward-
compat replay of historical events.
"""


# Lifecycle-tag literals — NOT terminal verdict classifications. Used by
# the watchdog re-add path + the cascade ``probe_*`` centraliser (which
# also serves the boot-time factory-integration cascade;
# ``"factory_integration"`` itself has no producer at HEAD and is kept
# for backward compatibility). Gate 14 allowlists these as the
# ``reason=`` value when accompanied by an
# ``# h3-allowlist: lifecycle-tag`` inline comment.
_LIFECYCLE_TAGS: Final[frozenset[str]] = frozenset(
    {
        "watchdog_recheck",
        "factory_integration",
        "probe_pinned",
        "probe_store",
        "probe_cascade",
        "kernel_invalidated_recheck",
        "probe",  # legacy ``EndpointQuarantine.add`` default at HEAD
    },
)


# APO-class reasons — watchdog APO recheck loop filter target. Inherited
# from ``_quarantine.py`` Mission C1 §T1.7.b classifier; the wider
# Mission H3 taxonomy adds no new APO-class members because the new
# CAPTURE_DEAD verdict is NOT APO-recoverable.
_APO_CLASS_REASONS: Final[frozenset[str]] = frozenset(
    {
        QuarantineReason.APO_DEGRADED.value,
        QuarantineReason.VAD_FRONTEND_DEAD.value,
        QuarantineReason.FORMAT_MISMATCH.value,
    },
)


# Non-recheck-eligible reasons — kernel-invalidated cold-probe rechecker
# filter target. Inherited from ``_quarantine.py`` Mission C1 §T1.7.b.
# CAPTURE_DEAD is added in Mission H3 — no in-process cure exists; a
# cold re-probe of the substrate-dead endpoint will just re-detect the
# same dead state and burn the rechecker's budget.
_RECHECK_INELIGIBLE_REASONS: Final[frozenset[str]] = frozenset(
    {
        QuarantineReason.VAD_FRONTEND_DEAD.value,
        QuarantineReason.FORMAT_MISMATCH.value,
        QuarantineReason.CAPTURE_DEAD.value,
    },
)


def is_apo_class_reason(reason: str) -> bool:
    """Return True iff the quarantine reason routes to the APO recheck loop.

    Mission C1 §T1.7.b classifier — consumed by
    :meth:`sovyx.voice.health.watchdog.VoiceCaptureWatchdog._apo_recheck_loop`
    and the hot-plug clear telemetry surface.

    Args:
        reason: Either :attr:`QuarantineEntry.resolved_reason` (post-H3),
            :attr:`QuarantineEntry.derived_reason` (Mission C1 LENIENT),
            or :attr:`QuarantineEntry.reason` fallback. Empty string
            returns False.

    Returns:
        True iff ``reason`` is in the canonical APO-class set.
    """
    return reason in _APO_CLASS_REASONS


def is_recheck_eligible(reason: str) -> bool:
    """Return True iff the kernel-invalidated rechecker should re-probe.

    Mission C1 §T1.7.b defensive filter — verdicts whose recovery
    happens BEFORE quarantine (VAD-frontend reset / FrameNormalizer
    engage) and Mission H3's :attr:`QuarantineReason.CAPTURE_DEAD` are
    NOT recheck-eligible because a cold re-probe of the quarantined
    endpoint will not change the underlying state.

    Args:
        reason: Either :attr:`QuarantineEntry.resolved_reason` (post-H3),
            :attr:`QuarantineEntry.derived_reason` (Mission C1 LENIENT),
            or :attr:`QuarantineEntry.reason` fallback. Empty string
            returns True (legacy entries default to recheck-eligible to
            preserve pre-mission behavior).

    Returns:
        True iff the rechecker should attempt a cold probe; False to
        skip the entry (it will expire on its own TTL).
    """
    return reason not in _RECHECK_INELIGIBLE_REASONS


def is_lifecycle_tag(reason: str) -> bool:
    """Return True iff ``reason`` is a lifecycle tag, not a terminal verdict.

    Used by Quality Gate 14 to permit string-literal ``reason=`` values
    at call sites that re-add an existing entry under a lifecycle label
    (e.g. watchdog recheck re-add tags ``reason="watchdog_recheck"``;
    the underlying verdict classification lives on
    :attr:`QuarantineEntry.derived_reason` /
    :attr:`~QuarantineEntry.resolved_reason` and survives the re-add).
    """
    return reason in _LIFECYCLE_TAGS


def resolve_reason_from_verdict(verdict: IntegrityVerdict) -> QuarantineReason:
    """Single-source-of-truth :class:`IntegrityVerdict` → :class:`QuarantineReason`.

    Exhaustive ``match`` covered by ``assert_never`` — mypy strict flags
    a missing case if a future :class:`IntegrityVerdict` member is added
    without a paired ``case`` arm here.

    The three verdicts that MUST NEVER reach a quarantine site
    (``HEALTHY``, ``VAD_MUTE``, ``INCONCLUSIVE``) raise :class:`ValueError`
    at runtime — programming error: the coordinator's verdict-router
    branches MUST handle these earlier (HEALTHY short-circuits;
    VAD_MUTE benign-skips; INCONCLUSIVE retries — but the T6.16 retry
    covers only the POST-APPLY probe, so a PRE-BYPASS INCONCLUSIVE can
    legitimately survive to bypass-strategy exhaustion; call sites
    passing such a fall-through verdict MUST map INCONCLUSIVE to
    ``terminal_verdict=None`` so ``_quarantine_endpoint``'s documented
    legacy fallback resolves the apo-recheck-eligible
    :attr:`QuarantineReason.APO_DEGRADED` instead of raising here and
    blocking quarantine/failover). The runtime error
    fails loudly so coordinator-side dispatch bugs surface immediately
    instead of silently classifying a benign verdict as a terminal one.

    Args:
        verdict: The terminal :class:`IntegrityVerdict` that drove the
            quarantine decision. Must be one of the four
            ``IntegrityVerdict.APO_DEGRADED`` / ``VAD_FRONTEND_DEAD`` /
            ``FORMAT_MISMATCH`` / ``DRIVER_SILENT`` members.

    Returns:
        The canonical :class:`QuarantineReason` value.

    Raises:
        ValueError: If ``verdict`` is :attr:`IntegrityVerdict.HEALTHY` /
            :attr:`~IntegrityVerdict.VAD_MUTE` /
            :attr:`~IntegrityVerdict.INCONCLUSIVE` — these MUST be handled
            by the coordinator's verdict-router earlier in dispatch.
    """
    from sovyx.voice.health.contract import (
        IntegrityVerdict as _Verdict,  # noqa: N813 — short alias for match arms
    )

    match verdict:
        case _Verdict.APO_DEGRADED:
            return QuarantineReason.APO_DEGRADED
        case _Verdict.VAD_FRONTEND_DEAD:
            return QuarantineReason.VAD_FRONTEND_DEAD
        case _Verdict.FORMAT_MISMATCH:
            return QuarantineReason.FORMAT_MISMATCH
        case _Verdict.DRIVER_SILENT:
            return QuarantineReason.DRIVER_SILENT
        case _Verdict.HEALTHY | _Verdict.VAD_MUTE | _Verdict.INCONCLUSIVE:
            msg = (
                f"verdict {verdict!r} must not reach _quarantine_endpoint — "
                "coordinator's verdict-router must handle this branch earlier "
                "(HEALTHY short-circuits; VAD_MUTE benign-skips; "
                "INCONCLUSIVE retries). See Mission H3 §4.3 ADR-D3 + "
                "CLAUDE.md anti-pattern #46."
            )
            raise ValueError(msg)
        case _ as never:
            assert_never(never)


def resolve_reason_from_diagnosis(diagnosis: Diagnosis) -> QuarantineReason:
    """Single-source-of-truth :class:`Diagnosis` → :class:`QuarantineReason`.

    Exhaustive ``match`` for the cascade-layer terminal diagnoses.
    Mission H3 §4.4 ADR-D4.

    Cascade-layer terminal conditions:

    * :attr:`Diagnosis.NO_SIGNAL` / :attr:`~Diagnosis.STREAM_OPEN_TIMEOUT`
      / :attr:`~Diagnosis.HEARTBEAT_TIMEOUT` → :attr:`QuarantineReason.CAPTURE_DEAD`.
    * :attr:`Diagnosis.KERNEL_INVALIDATED` → :attr:`QuarantineReason.KERNEL_INVALIDATED`.
    * :attr:`Diagnosis.APO_DEGRADED` / :attr:`~Diagnosis.MIXER_SATURATED`
      → :attr:`QuarantineReason.APO_DEGRADED`. Mixer saturation is an
      upstream-of-APO failure; the L2.5 mixer-sanity layer is the cure
      BEFORE quarantine; if it reaches quarantine the bypass-coordinator
      ladder applies.
    * :attr:`Diagnosis.FORMAT_MISMATCH` /
      :attr:`~Diagnosis.INVALID_SAMPLE_RATE_NO_AUTO_CONVERT` /
      :attr:`~Diagnosis.INSUFFICIENT_BUFFER_SIZE` →
      :attr:`QuarantineReason.FORMAT_MISMATCH`.

    Cascade-fallthrough conditions raise :class:`ValueError` because
    they MUST be retried with a different combo, not quarantined:
    ``DRIVER_ERROR`` / ``DEVICE_BUSY`` / ``PERMISSION_DENIED`` /
    ``PERMISSION_REVOKED_RUNTIME`` / ``EXCLUSIVE_MODE_NOT_AVAILABLE``.

    Benign / non-terminal diagnoses raise :class:`ValueError` because
    they MUST NOT reach quarantine: ``HEALTHY`` / ``MUTED`` /
    ``LOW_SIGNAL`` / ``VAD_INSENSITIVE`` / the ``MIXER_*`` family /
    ``UNKNOWN``.

    Args:
        diagnosis: The terminal :class:`Diagnosis` that drove the
            quarantine decision.

    Returns:
        The canonical :class:`QuarantineReason` value.

    Raises:
        ValueError: When the diagnosis is non-terminal, benign, or
            cascade-fallthrough rather than a legitimate quarantine
            terminal condition.
    """
    from sovyx.voice.health.contract import (
        Diagnosis as _Diag,  # noqa: N813 — short alias for match arms
    )

    match diagnosis:
        case _Diag.NO_SIGNAL | _Diag.STREAM_OPEN_TIMEOUT | _Diag.HEARTBEAT_TIMEOUT:
            return QuarantineReason.CAPTURE_DEAD
        case _Diag.KERNEL_INVALIDATED:
            return QuarantineReason.KERNEL_INVALIDATED
        case _Diag.APO_DEGRADED | _Diag.MIXER_SATURATED:
            return QuarantineReason.APO_DEGRADED
        case (
            _Diag.FORMAT_MISMATCH
            | _Diag.INVALID_SAMPLE_RATE_NO_AUTO_CONVERT
            | _Diag.INSUFFICIENT_BUFFER_SIZE
        ):
            return QuarantineReason.FORMAT_MISMATCH
        case (
            _Diag.DRIVER_ERROR
            | _Diag.DEVICE_BUSY
            | _Diag.PERMISSION_DENIED
            | _Diag.PERMISSION_REVOKED_RUNTIME
            | _Diag.EXCLUSIVE_MODE_NOT_AVAILABLE
        ):
            msg = (
                f"diagnosis {diagnosis!r} is a cascade-fallthrough condition, "
                "not a quarantine terminal — the cascade should retry-different-combo, "
                "not quarantine. See Mission H3 §4.4 ADR-D4 + anti-pattern #46."
            )
            raise ValueError(msg)
        case (
            _Diag.HEALTHY
            | _Diag.MUTED
            | _Diag.LOW_SIGNAL
            | _Diag.VAD_INSENSITIVE
            | _Diag.MIXER_ZEROED
            | _Diag.MIXER_UNKNOWN_PATTERN
            | _Diag.MIXER_CUSTOMIZED
            | _Diag.UNKNOWN
        ):
            msg = (
                f"diagnosis {diagnosis!r} must not reach _quarantine_endpoint — "
                "this is a non-terminal or benign diagnosis; coordinator / cascade "
                "branch must handle it earlier. See Mission H3 §4.4 ADR-D4 + "
                "anti-pattern #46."
            )
            raise ValueError(msg)
        case _ as never:
            assert_never(never)


__all__ = [
    "LEGACY_TWIN_MAP_REASONS",
    "QuarantineReason",
    "is_apo_class_reason",
    "is_lifecycle_tag",
    "is_recheck_eligible",
    "resolve_reason_from_diagnosis",
    "resolve_reason_from_verdict",
]


# Module-load invariant — every QuarantineReason member must appear in
# the LEGACY_TWIN_MAP_REASONS. If a future commit adds a member without
# extending the map, this catches it at import time.
_missing = frozenset(QuarantineReason) - frozenset(LEGACY_TWIN_MAP_REASONS)
if _missing:  # pragma: no cover — defensive, tested via test_quarantine_reasons.py
    _msg = (
        f"LEGACY_TWIN_MAP_REASONS must cover every QuarantineReason member; missing: {_missing!r}"
    )
    raise RuntimeError(_msg)
