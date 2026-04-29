"""Tier 2 — host-API rotate-then-exclusive bypass.

Voice Windows Paranoid Mission §D2 — for endpoints whose runtime
``host_api_name`` is ``MME`` / ``Windows DirectSound`` / ``Windows
WDM-KS``, rotate the capture stream to ``Windows WASAPI`` and then
engage exclusive mode. Bypasses every APO layer (MFX/SFX/EFX) on the
capture pipeline because exclusive mode does not traverse the APO
graph at all.

**v0.24.0 (foundation phase) — flag-gated stub.** The actual
2-phase rotate-then-exclusive logic + the new
:meth:`AudioCaptureTask.request_host_api_rotate` method land in
v0.25.0 wire-up (mission task T28). v0.24.0 ships:

* The strategy class on the :class:`PlatformBypassStrategy` Protocol
  so factory.py wire-up can register it without further plumbing
  in v0.25.0.
* Eligibility logic that respects the
  ``bypass_tier2_host_api_rotate_enabled`` tuning flag AND the
  cross-validator gate (``cascade_host_api_alignment_enabled`` must
  also be True — enforced at boot by
  :func:`engine/config.py::_enforce_paranoid_mission_dependencies`).
* Host-API filter that activates only on MME / DirectSound / WDM-KS
  (the W-2 not_applicable gap that Tier 3 ``win.wasapi_exclusive``
  cannot reach because Tier 3's eligibility filter restricts to
  WASAPI-shared endpoints). Tier 2 + Tier 3 partition the Windows
  host_api space without overlap.
* :meth:`apply` raises :class:`BypassApplyError(reason="strategy_disabled")`
  when the flag is ``False``. Defence-in-depth gate; eligibility
  blocks first in production.

**v0.25.0+ wire-up contract** (documented now to lock in the
design):

Apply (2-phase):

.. code-block:: python

    # Phase A — rotate to WASAPI
    rotate_result = await context.capture_task.request_host_api_rotate(
        target_host_api="Windows WASAPI",
        target_exclusive=False,
    )
    # Phase B — engage exclusive (only if Phase A succeeded)
    if rotate_result.verdict is HostApiRotateVerdict.ROTATED_SUCCESS:
        excl_result = await context.capture_task.request_exclusive_restart()

Apply return tags:

* ``rotated_then_exclusive_engaged`` — both phases engaged
* ``rotated_then_exclusive_downgraded`` — A engaged but B fell to
  shared (still better than MME)

Failure tokens:

* ``rotate_no_wasapi_sibling`` — no WASAPI DeviceEntry available
* ``rotate_target_open_failed``
* ``rotate_fallback_to_source_host_api``
* ``rotated_but_exclusive_open_failed``
* ``capture_task_not_running``

Revert (2-step inverse):

.. code-block:: python

    shared_result = await context.capture_task.request_shared_restart()
    if self._source_host_api is not None:
        rotate_result = await context.capture_task.request_host_api_rotate(
            target_host_api=self._source_host_api,
            target_exclusive=False,
        )

State preservation: ``self._source_host_api`` is set during apply
(``capture_task._host_api_name`` BEFORE rotate). Strategy instances
are per-coordinator-session per the
:class:`PlatformBypassStrategy._strategy.py:93-95` contract, so
in-strategy state is safe.

**Cross-validator dependency:** Tier 2 mutates
``self._host_api_name`` on the capture stream and relies on the
opener honouring that on subsequent device-error reopens. Without
``cascade_host_api_alignment_enabled=True`` (Furo W-4 fix in
``_stream_opener._device_chain``), the opener falls back to
PortAudio enumeration order on the next reopen and silently undoes
the rotation — the strategy would report ``ROTATED_SUCCESS`` while
the actual endpoint drifts back to MME on the first hiccup. The
cross-validator at :func:`engine/config.py::_enforce_paranoid_mission_dependencies`
rejects this contradictory configuration at boot with a
remediation hint.

See:

* ``docs-internal/ADR-voice-bypass-tier-system.md`` — design.
* ``docs-internal/ADR-voice-cascade-runtime-alignment.md`` — the
  W-4 fix Tier 2 depends on.
* ``docs/modules/voice-troubleshooting-windows.md`` — operator
  flag-flip procedure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import Eligibility

if TYPE_CHECKING:
    from sovyx.voice.health.contract import BypassContext

logger = get_logger(__name__)


# ── Strategy identity ───────────────────────────────────────────────


_STRATEGY_NAME = "win.host_api_rotate_then_exclusive"
"""Coordinator-visible strategy identifier — stable external API.
Changing it breaks dashboard filters + the per-strategy metric
counter attributes."""


# ── Eligibility reason tokens ──────────────────────────────────────


_REASON_NOT_WIN32 = "not_win32_platform"
_REASON_DISABLED_BY_TUNING = "host_api_rotate_disabled_by_tuning"
_REASON_ENDPOINT_ALREADY_ON_WASAPI = "endpoint_already_on_wasapi"


# ── Host-API allowlist (lowercased canonical PortAudio labels) ─────


_NON_WASAPI_HOST_API_LABELS: frozenset[str] = frozenset(
    {
        # PortAudio v19 canonical Windows host-API labels (the only
        # legitimate values for this strategy's eligibility).
        "mme",
        "windows directsound",
        "windows wdm-ks",
    }
)
"""Eligible host_api labels (lowercased) — Tier 2 activates only on
these. WASAPI-shared endpoints are Tier 3's surface
(``win.wasapi_exclusive``); together Tier 2 + Tier 3 partition the
Windows host_api space without overlap."""


# ── Cost hint (ms) ─────────────────────────────────────────────────


_APPLY_COST_MS = 800
"""2-phase rotate + exclusive engagement on modern Windows + Razer/
Realtek hardware: Phase A rotate ≈ 200-400 ms (PortAudio host-API
switch), Phase B exclusive engagement ≈ 200-400 ms (WASAPI exclusive
negotiation). 800 ms is a safe upper bound for telemetry; the
coordinator never sequences on cost."""


def _tier2_enabled() -> bool:
    """Read the tier-2 flag at call time so env-var overrides take
    effect without a daemon restart.

    Cross-validator gate: the
    :func:`engine/config.py::_enforce_paranoid_mission_dependencies`
    boot validator rejects ``bypass_tier2_host_api_rotate_enabled=True``
    + ``cascade_host_api_alignment_enabled=False`` at config load
    time with a remediation hint. That single boot-time gate is the
    authoritative check; no runtime defence-in-depth is added here
    because every ``_VoiceTuning()`` call re-runs the validator and
    a contradictory configuration cannot survive load.
    """
    return _VoiceTuning().bypass_tier2_host_api_rotate_enabled


class WindowsHostApiRotateThenExclusiveBypass:
    """Tier 2 — rotate non-WASAPI to WASAPI, then engage exclusive.

    See module docstring for the full 2-phase apply / revert design,
    failure-token vocabulary, cross-validator dependency on
    cascade-runtime alignment, and v0.25.0 wire-up contract.

    Eligibility:
        * ``platform_key != "win32"`` → ``not_win32_platform``
        * ``bypass_tier2_host_api_rotate_enabled`` is ``False``
          (foundation default) → ``host_api_rotate_disabled_by_tuning``
        * ``host_api_name`` ∉ {MME, Windows DirectSound, Windows WDM-
          KS} → ``endpoint_already_on_wasapi`` (delegated to Tier 3).

    Cross-validator gate: the contradictory configuration
    ``bypass_tier2_host_api_rotate_enabled=True`` +
    ``cascade_host_api_alignment_enabled=False`` is rejected at boot
    by :func:`engine/config.py::_enforce_paranoid_mission_dependencies`
    with a remediation hint. The boot-time gate is authoritative —
    by the time eligibility runs, the flag combination is already
    proven safe.

    Apply:
        v0.24.0 placeholder — raises :class:`BypassApplyError(reason=
        "strategy_disabled")`. v0.25.0 wire-up replaces with the
        2-phase rotate-then-exclusive logic.

    Revert:
        v0.24.0 placeholder — no-op (the v0.24.0 apply never engages
        anything to revert).
    """

    name: str = _STRATEGY_NAME

    def __init__(self) -> None:
        # v0.25.0+ wire-up: ``self._source_host_api`` set during
        # apply (``capture_task._host_api_name`` before rotate) so
        # revert can restore the pre-apply host_api. Per-coordinator-
        # session strategy instance per `_strategy.py:93-95`.
        self._source_host_api: str | None = None

    async def probe_eligibility(
        self,
        context: BypassContext,
    ) -> Eligibility:
        if context.platform_key != "win32":
            return Eligibility(
                applicable=False,
                reason=_REASON_NOT_WIN32,
                estimated_cost_ms=0,
            )
        if not _tier2_enabled():
            return Eligibility(
                applicable=False,
                reason=_REASON_DISABLED_BY_TUNING,
                estimated_cost_ms=0,
            )
        # Cross-validator at engine/config.py guarantees alignment is
        # enabled whenever Tier 2 is enabled — no runtime check needed.
        host_api_normalised = (context.host_api_name or "").strip().lower()
        if host_api_normalised not in _NON_WASAPI_HOST_API_LABELS:
            # WASAPI-shared endpoint — delegate to Tier 3
            # ``win.wasapi_exclusive``. Tier 2 + Tier 3 partition the
            # Windows host_api space without overlap.
            return Eligibility(
                applicable=False,
                reason=_REASON_ENDPOINT_ALREADY_ON_WASAPI,
                estimated_cost_ms=0,
            )
        return Eligibility(
            applicable=True,
            reason="",
            estimated_cost_ms=_APPLY_COST_MS,
        )

    async def apply(
        self,
        context: BypassContext,
    ) -> str:
        """T28 wire-up — 2-phase rotate-then-exclusive bypass.

        Phase A: rotate the capture stream from MME / DirectSound /
        WDM-KS to ``Windows WASAPI`` via
        :meth:`AudioCaptureTask.request_host_api_rotate`. This step
        moves the stream onto the WASAPI shared graph but does NOT
        yet bypass the APO chain (WASAPI shared still traverses MFX
        / SFX / EFX).

        Phase B: only if Phase A engaged, call
        :meth:`AudioCaptureTask.request_exclusive_restart` to engage
        WASAPI exclusive mode on the rotated stream. Exclusive mode
        does NOT traverse the APO graph at all — the actual MFX/SFX/
        EFX bypass that Tier 2 promises.

        Return tags (caller is the coordinator's apply driver):

        * ``rotated_then_exclusive_engaged`` — both phases engaged
        * ``rotated_then_exclusive_downgraded`` — Phase A engaged
          but Phase B fell back to shared (still better than the
          original MME / DirectSound / WDM-KS path because WASAPI
          shared has fewer APO layers than legacy host APIs)

        Raises:
            BypassApplyError: with a stable ``reason`` token on each
                failure mode. Coordinator translates into a
                structured ``BypassVerdict.FAILED_TO_APPLY``.
        """
        capture_task = context.capture_task
        if capture_task is None:
            raise BypassApplyError(
                "BypassContext.capture_task is None — coordinator wire-up bug",
                reason="capture_task_not_running",
            )

        # Snapshot the source host_api BEFORE the rotation so revert
        # can restore the pre-apply state. Per-coordinator-session
        # strategy instance state is safe (see class docstring).
        self._source_host_api = getattr(capture_task, "_host_api_name", None)

        # Phase A — rotate to WASAPI (shared mode for now; Phase B
        # engages exclusive separately so each phase has its own
        # verdict for clean telemetry).
        rotate_result = await capture_task.request_host_api_rotate(
            target_host_api="Windows WASAPI",
            target_exclusive=False,
        )
        if not rotate_result.engaged:
            logger.warning(
                "voice.bypass.win_host_api_rotate_then_exclusive.rotate_failed",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                source_host_api=context.host_api_name,
                rotate_verdict=rotate_result.verdict.value,
                rotate_detail=rotate_result.detail,
            )
            detail = rotate_result.detail or rotate_result.verdict.value
            raise BypassApplyError(
                f"host-api rotate to Windows WASAPI failed: {detail}",
                reason=f"rotate_{rotate_result.verdict.value}",
            )

        # Phase B — engage exclusive on the rotated stream.
        excl_result = await capture_task.request_exclusive_restart()
        if excl_result.engaged:
            logger.info(
                "voice.bypass.win_host_api_rotate_then_exclusive.engaged",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                source_host_api=self._source_host_api,
                target_host_api="Windows WASAPI",
            )
            return "rotated_then_exclusive_engaged"

        # Phase B downgrade — Phase A's rotation took (we're on
        # WASAPI shared) but the exclusive engagement fell to shared.
        # Still net-positive: WASAPI shared has fewer APO layers
        # than MME / DirectSound / WDM-KS.
        logger.warning(
            "voice.bypass.win_host_api_rotate_then_exclusive.exclusive_downgraded",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            source_host_api=self._source_host_api,
            exclusive_verdict=excl_result.verdict.value,
            exclusive_detail=excl_result.detail,
        )
        return "rotated_then_exclusive_downgraded"

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        """T28 wire-up — 2-step inverse of apply.

        Step 1: revert exclusive mode (if it was engaged) via
        :meth:`AudioCaptureTask.request_shared_restart`.
        Step 2: rotate back to the source host_api (captured during
        apply) via :meth:`AudioCaptureTask.request_host_api_rotate`.

        Idempotent + best-effort: any failure is logged but does NOT
        raise. The coordinator's revert is called on a teardown path
        that must complete; raising would leave the pipeline in an
        inconsistent state.
        """
        capture_task = context.capture_task
        if capture_task is None:
            return
        # Step 1 — revert to shared mode. Best-effort.
        try:
            await capture_task.request_shared_restart()
        except Exception as exc:  # noqa: BLE001 — revert is best-effort
            logger.warning(
                "voice.bypass.win_host_api_rotate_then_exclusive.revert_shared_failed",
                strategy=_STRATEGY_NAME,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # Step 2 — rotate back to source host_api. Skip when the
        # apply was never called (``self._source_host_api`` is None)
        # or when the source was already WASAPI (no rotation
        # performed; rotating back is a no-op).
        if self._source_host_api is None:
            return
        if self._source_host_api == "Windows WASAPI":
            return
        try:
            await capture_task.request_host_api_rotate(
                target_host_api=self._source_host_api,
                target_exclusive=False,
            )
        except Exception as exc:  # noqa: BLE001 — revert is best-effort
            logger.warning(
                "voice.bypass.win_host_api_rotate_then_exclusive.revert_rotate_failed",
                strategy=_STRATEGY_NAME,
                error=str(exc),
                error_type=type(exc).__name__,
                source_host_api=self._source_host_api,
            )


__all__ = ["WindowsHostApiRotateThenExclusiveBypass"]
