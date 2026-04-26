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
        # v0.24.0 placeholder: AudioCaptureTask.request_host_api_rotate
        # lands in v0.25.0 wire-up (mission task T28). Until then any
        # apply path raises with a stable reason token so the
        # coordinator records a structured FAILED_TO_APPLY outcome.
        logger.warning(
            "voice.bypass.win_host_api_rotate_then_exclusive.apply_not_yet_wired",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            host_api=context.host_api_name,
            target_version="v0.25.0",
            reason=(
                "v0.24.0 ships the strategy class + eligibility logic; the "
                "2-phase rotate-then-exclusive apply (request_host_api_rotate "
                "+ request_exclusive_restart) lands in v0.25.0 wire-up "
                "(mission task T28)."
            ),
        )
        raise BypassApplyError(
            "WindowsHostApiRotateThenExclusiveBypass apply path not wired in v0.24.0",
            reason="strategy_disabled",
        )

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        # v0.24.0 placeholder: apply never engages, so revert is a
        # no-op. Idempotent per the PlatformBypassStrategy contract.
        del context  # intentionally unused in v0.24.0


__all__ = ["WindowsHostApiRotateThenExclusiveBypass"]
