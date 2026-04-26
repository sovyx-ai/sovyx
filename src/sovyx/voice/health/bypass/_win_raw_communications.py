"""Tier 1 — RAW + Communications bypass via ``IAudioClient3::SetClientProperties``.

Voice Windows Paranoid Mission §D2 — the cheapest bypass strategy on
Windows: a sub-millisecond ``IAudioClient3::SetClientProperties`` COM
call that bypasses the Microsoft Voice Clarity APO's MFX / SFX
layers. No exclusive lock (other apps holding the same endpoint are
unaffected), no admin, no registry mutation. Per-MMDevice surface, so
it covers MME / DirectSound / WDM-KS / WASAPI-shared endpoints
uniformly when the device reports
``System.Devices.AudioDevice.RawProcessingSupported=true``.

**v0.24.0 (foundation phase) — flag-gated stub.** The full IAudioClient3
ctypes shim (``voice/health/_audioclient3_raw.py``) lands in v0.25.0
wire-up (mission task T27). v0.24.0 ships:

* The strategy class registered on the
  :class:`PlatformBypassStrategy` Protocol so factory.py wire-up
  can import it without further plumbing in v0.25.0.
* Eligibility logic that respects the
  ``bypass_tier1_raw_enabled`` tuning flag — when the flag is
  ``False`` (foundation default), eligibility returns
  ``applicable=False, reason=raw_communications_bypass_disabled_by_tuning``
  and the coordinator advances without consuming the attempt
  budget (``factory.py:1689-1690`` contract).
* :meth:`apply` raises :class:`BypassApplyError(reason="strategy_disabled")`
  when the flag is ``False``. The coordinator never reaches this
  in production because eligibility blocks first; the explicit
  raise is a defence-in-depth gate so a future direct-call test
  doesn't accidentally execute a half-wired strategy.

**Why DEFER ``IPolicyConfig::SetPropertyValue(PKEY_AudioEndpoint_Disable_SysFx)``:**
documented in ``docs-internal/ADR-voice-bypass-tier-system.md``
§D3. ``SetPropertyValue`` succeeds on calling process but Win 11
22H2+ rebroadcasts ``MMNotificationClient::OnPropertyValueChanged``
to every active session, causing ``AUDCLNT_E_DEVICE_INVALIDATED``
in OTHER apps (Discord/Zoom/Teams) holding the same MMDevice. Tier
1 RAW covers the same MFX/SFX bypass surface with **zero cross-app
blast radius**. Asymmetric regret: including disable_sysfx expands
kill-radius without expanding cure-radius.

**v0.25.0+ wire-up contract** (documented now to lock in the
design):

* :meth:`apply` opens an :class:`IAudioClient3` against the active
  MMDevice via the ctypes shim, calls
  ``SetClientProperties(AUDIOCLIENT_PROPERTIES_RAW |
  AUDIO_STREAM_CATEGORY_COMMUNICATIONS)``, and closes the COM
  client. The PortAudio capture stream subsequently inherits the
  RAW + Communications flags via session attachment without
  needing a separate :meth:`AudioCaptureTask.request_*_restart`
  call.
* :meth:`revert` calls ``SetClientProperties(AUDIO_STREAM_CATEGORY_OTHER)``
  to clear the property and reopens the stream against the
  cascade-winning host_api.
* Failure tokens (mirror the existing exclusive bypass pattern):
  ``raw_property_rejected_by_driver`` (Realtek HD pre-2020 codecs
  that lie about ``RawProcessingSupported``),
  ``raw_open_failed_no_stream``,
  ``raw_open_failed_fallback_to_plain``, ``capture_task_not_running``,
  ``not_win32_platform``.

See:

* ``docs-internal/ADR-voice-bypass-tier-system.md`` for the
  design rationale + tier ordering.
* ``docs/modules/voice-troubleshooting-windows.md`` for the
  operator-facing flag flip procedure.
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


_STRATEGY_NAME = "win.raw_communications"
"""Coordinator-visible strategy identifier — stable external API.
Changing it breaks dashboard filters + the per-strategy metric
counter attributes."""


# ── Eligibility reason tokens ──────────────────────────────────────


_REASON_NOT_WIN32 = "not_win32_platform"
_REASON_DISABLED_BY_TUNING = "raw_communications_bypass_disabled_by_tuning"
_REASON_DEVICE_RAW_UNSUPPORTED = "device_raw_processing_unsupported"


# ── Cost hint (ms) ─────────────────────────────────────────────────


_APPLY_COST_MS = 5
"""Tier 1 is the cheapest bypass — IAudioClient3::SetClientProperties
is sub-millisecond + the open/close round-trip dominates. 5 ms is a
generous upper bound for telemetry; the coordinator never sequences
on cost."""


def _is_enabled() -> bool:
    """Read the foundation-phase flag at call time so env-var overrides
    take effect without a daemon restart.

    The module-level ``_CONST = _VoiceTuning().field`` pattern that
    the rest of the voice subsystem uses is fine for cold-path
    constants but the bypass eligibility is a hot-path read on every
    deaf-signal coordinator pass — we want operator flag flips to
    take effect within one heartbeat tick, not require a restart.
    Hence the per-call read.
    """
    return _VoiceTuning().bypass_tier1_raw_enabled


class WindowsRawCommunicationsBypass:
    """Tier 1 — RAW + Communications bypass via ``IAudioClient3``.

    See module docstring for the full design + v0.24.0 placeholder
    contract + v0.25.0 wire-up plan.

    Eligibility:
        * ``platform_key != "win32"`` → ``not_win32_platform``
        * ``bypass_tier1_raw_enabled`` is ``False`` (foundation
          default) → ``raw_communications_bypass_disabled_by_tuning``
        * v0.25.0+ also gates on
          ``System.Devices.AudioDevice.RawProcessingSupported`` per
          MMDevice; v0.24.0 stub does not query the property
          (placeholder).

    Apply:
        v0.24.0 placeholder — raises
        :class:`BypassApplyError(reason="strategy_disabled")` when
        the flag is ``False``. The coordinator never reaches this in
        production because eligibility blocks first; the raise is
        defence-in-depth so a direct-call test of an un-wired
        strategy fails loudly instead of silently doing nothing.

    Revert:
        v0.24.0 placeholder — no-op (the v0.24.0 apply never engages
        anything to revert). v0.25.0 wire-up replaces with
        ``SetClientProperties(AUDIO_STREAM_CATEGORY_OTHER)`` + reopen.
    """

    name: str = _STRATEGY_NAME

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
        if not _is_enabled():
            return Eligibility(
                applicable=False,
                reason=_REASON_DISABLED_BY_TUNING,
                estimated_cost_ms=0,
            )
        # v0.24.0 foundation phase: when the flag IS True (operator
        # opt-in), eligibility passes here as a placeholder that the
        # v0.25.0 wire-up will tighten by also reading
        # ``System.Devices.AudioDevice.RawProcessingSupported`` from
        # the endpoint's IPropertyStore. Until then we let the apply
        # path run — but the v0.24.0 apply still raises
        # ``strategy_disabled`` because the COM bindings aren't
        # wired yet. This means flipping the flag in v0.24.0 is a
        # safe no-op (eligibility passes → apply raises → coordinator
        # advances) rather than a silent feature-gate gap.
        return Eligibility(
            applicable=True,
            reason="",
            estimated_cost_ms=_APPLY_COST_MS,
        )

    async def apply(
        self,
        context: BypassContext,
    ) -> str:
        # v0.24.0 placeholder: the IAudioClient3 ctypes shim
        # (voice/health/_audioclient3_raw.py) lands in v0.25.0
        # wire-up. Until then any apply path raises with a stable
        # reason token so the coordinator records a structured
        # FAILED_TO_APPLY outcome rather than a generic exception.
        logger.warning(
            "voice.bypass.win_raw_communications.apply_not_yet_wired",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            host_api=context.host_api_name,
            target_version="v0.25.0",
            reason=(
                "v0.24.0 ships the strategy class + eligibility logic; the "
                "IAudioClient3 SetClientProperties COM call lands in v0.25.0 "
                "wire-up (mission task T27)."
            ),
        )
        raise BypassApplyError(
            "WindowsRawCommunicationsBypass apply path not wired in v0.24.0",
            reason="strategy_disabled",
        )

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        # v0.24.0 placeholder: apply never engages, so revert is a
        # no-op. Idempotent per the PlatformBypassStrategy contract.
        del context  # intentionally unused in v0.24.0


__all__ = ["WindowsRawCommunicationsBypass"]
