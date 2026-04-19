"""Layer 6 — Setup wizard orchestrator (ADR §4.6).

Headless composition of the L3 warm probe, the L2 cascade fallback,
and the pin-combo side-effect. Owns the state machine the CLI and
the dashboard setup flow both wrap — UI (Rich prompts, SPA progress
bars, live VAD meter) is strictly a presentation concern and lives
outside this module.

Flow (ADR §4.6 steps 4-6):

1. Caller supplies ``endpoint_guid`` + ``device_index`` + fingerprint
   metadata (obtained from device enumeration).
2. Wizard runs a **warm probe** on that device with whatever combo
   the platform cascade table suggests first (or a caller-supplied
   preferred combo).
3. Branch on the probe's :class:`Diagnosis`:

   * :attr:`Diagnosis.HEALTHY` → pin the combo and return
     :attr:`WizardOutcome.PASSED_DIRECT`.
   * :attr:`Diagnosis.APO_DEGRADED` / :attr:`Diagnosis.VAD_INSENSITIVE` →
     run the **L2 cascade** (warm mode). If any attempt wins, pin
     and return :attr:`WizardOutcome.PASSED_VIA_CASCADE`. Otherwise
     return :attr:`WizardOutcome.DEGRADED_NO_COMBO`.
   * :attr:`Diagnosis.MUTED` → :attr:`WizardOutcome.MUTED`.
   * :attr:`Diagnosis.LOW_SIGNAL` → :attr:`WizardOutcome.LOW_SIGNAL`.
   * :attr:`Diagnosis.PERMISSION_DENIED` →
     :attr:`WizardOutcome.PERMISSION_DENIED`.
   * Anything else (``NO_SIGNAL``, ``DRIVER_ERROR``, ``DEVICE_BUSY``,
     ``HOT_UNPLUGGED``, ``UNKNOWN``) → :attr:`WizardOutcome.OTHER`
     with the diagnosis verbatim in the details dict.

4. Every outcome carries a localizable ``hint`` string matched to
   the ADR's user-facing copy and a ``deep_link`` (OS settings URI
   when applicable).

The wizard refuses to pin a combo that didn't produce a fresh
:attr:`Diagnosis.HEALTHY` in the current run — a cascade fast-path
hit from a stale ComboStore entry is enough to *skip* the cascade
but not enough to justify overwriting the pin the user already has.

Injection points
----------------
* ``probe_fn`` — defaults to :func:`sovyx.voice.health.probe.probe`.
* ``cascade_fn`` — defaults to :func:`sovyx.voice.health.cascade.run_cascade`.
* ``capture_overrides`` — ``None`` disables pinning (dashboard
  "try only" button).
* ``preferred_combo`` — skips the cascade's first-attempt prediction
  when the caller already knows the right combo (dashboard re-probe).

Tests never touch PortAudio / ONNX because the orchestrator talks to
``probe_fn`` + ``cascade_fn`` through the same callable surface the
cascade uses internally.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.health.contract import (
    CascadeResult,
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sovyx.voice.health.capture_overrides import CaptureOverrides

logger = get_logger(__name__)


class WizardOutcome(StrEnum):
    """Final state of a :class:`VoiceSetupWizard.run` call."""

    PASSED_DIRECT = "passed_direct"
    """Warm probe returned HEALTHY on the first combo."""

    PASSED_VIA_CASCADE = "passed_via_cascade"
    """Warm probe degraded, cascade found a healthy combo."""

    DEGRADED_NO_COMBO = "degraded_no_combo"
    """Both the warm probe and the cascade failed. User must switch
    microphones, disable the capture APO, or proceed with degraded
    capture."""

    MUTED = "muted"
    """OS mute flag is on."""

    LOW_SIGNAL = "low_signal"
    """Input level is below the VAD floor — user needs to raise
    microphone gain."""

    PERMISSION_DENIED = "permission_denied"
    """OS denied microphone access (Windows MicrophoneAccess off,
    macOS TCC revoked)."""

    OTHER = "other"
    """Diagnosis the wizard has no specific remediation for (driver
    error, device busy, unplugged mid-flow, unknown)."""


_DEEP_LINK_BY_OUTCOME_AND_PLATFORM: Mapping[tuple[WizardOutcome, str], str] = {
    (WizardOutcome.MUTED, "win32"): "ms-settings:sound",
    (WizardOutcome.MUTED, "darwin"): "x-apple.systempreferences:com.apple.preference.sound",
    (WizardOutcome.LOW_SIGNAL, "win32"): "ms-settings:sound",
    (WizardOutcome.LOW_SIGNAL, "darwin"): "x-apple.systempreferences:com.apple.preference.sound",
    (WizardOutcome.PERMISSION_DENIED, "win32"): "ms-settings:privacy-microphone",
    (
        WizardOutcome.PERMISSION_DENIED,
        "darwin",
    ): "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}


_HINT_BY_OUTCOME: Mapping[WizardOutcome, str] = {
    WizardOutcome.PASSED_DIRECT: "Tudo certo. Microfone configurado.",
    WizardOutcome.PASSED_VIA_CASCADE: (
        "Resolvido automaticamente — encontramos uma configuração "
        "que contorna o processamento corrompido."
    ),
    WizardOutcome.DEGRADED_NO_COMBO: (
        "Não conseguimos encontrar uma combinação saudável para este "
        "microfone. Tente outro microfone, desabilite o 'Voice Clarity' "
        "nas configurações de som, ou prossiga mesmo assim."
    ),
    WizardOutcome.MUTED: "Microfone está mudo. Ative-o nas configurações do sistema.",
    WizardOutcome.LOW_SIGNAL: (
        "Sinal muito baixo. Aumente o ganho do microfone nas "
        "configurações do sistema e tente novamente."
    ),
    WizardOutcome.PERMISSION_DENIED: (
        "Permissão negada. Libere o acesso ao microfone nas "
        "configurações de privacidade do sistema."
    ),
    WizardOutcome.OTHER: (
        "Falha na configuração do microfone. Verifique se o dispositivo "
        "está conectado e se o serviço de áudio está rodando."
    ),
}


@dataclass(frozen=True, slots=True)
class WizardReport:
    """Aggregate outcome of a :meth:`VoiceSetupWizard.run` call.

    Attributes:
        outcome: The user-facing outcome bucket.
        hint: Localized message from :data:`_HINT_BY_OUTCOME`. Callers
            may override for locale if needed.
        deep_link: OS settings URI for ``MUTED`` / ``LOW_SIGNAL`` /
            ``PERMISSION_DENIED`` on Windows / macOS. Empty string
            otherwise (Linux has no universal deep-link surface, and
            cascade failures don't map to one single setting).
        winning_combo: The combo that produced HEALTHY, if any. ``None``
            on every failure outcome.
        direct_probe: The first warm probe's result. Always present.
        cascade_result: Populated when the wizard fell through to
            :func:`run_cascade`. ``None`` when the first probe passed
            or the first probe's diagnosis was non-degraded (``MUTED``
            et al.) and the cascade was skipped.
        pinned: ``True`` iff the wizard wrote the winning combo to the
            :class:`CaptureOverrides` file.
        endpoint_guid: Echoed for caller convenience.
        details: Extra diagnostic context (platform, detected APOs,
            cascade attempt count, probe timings).
    """

    outcome: WizardOutcome
    hint: str
    deep_link: str
    winning_combo: Combo | None
    direct_probe: ProbeResult
    cascade_result: CascadeResult | None
    pinned: bool
    endpoint_guid: str
    details: Mapping[str, Any] = field(default_factory=dict)


ProbeFn = Callable[..., "Awaitable[ProbeResult]"]
CascadeFn = Callable[..., "Awaitable[CascadeResult]"]


class VoiceSetupWizard:
    """Headless composition of warm-probe + cascade + pin-combo.

    Hold state between steps so the dashboard can drive a multi-round
    flow (list devices → user picks → run) without re-packaging every
    argument. The CLI and dashboard construct one wizard per user
    interaction.

    Attributes:
        platform_key: ``"win32"``, ``"linux"``, or ``"darwin"``. Chosen
            from :func:`sys.platform` at construction.
        probe_fn: Injected probe callable. Tests replace with a fake.
        cascade_fn: Injected cascade callable. Tests replace with a
            fake.
        capture_overrides: Writer for pinned combos. ``None`` suppresses
            pinning (useful from the dashboard "re-probe only" path).
    """

    def __init__(
        self,
        *,
        probe_fn: ProbeFn,
        cascade_fn: CascadeFn,
        capture_overrides: CaptureOverrides | None = None,
        platform_key: str | None = None,
    ) -> None:
        """Construct the wizard.

        Args:
            probe_fn: Warm-probe callable. Production callers pass
                :func:`sovyx.voice.health.probe.probe`; tests pass a
                fake returning a pre-made :class:`ProbeResult`.
            cascade_fn: Cascade callable. Production callers pass
                :func:`sovyx.voice.health.cascade.run_cascade`.
            capture_overrides: Persistent pinned-combo writer.
            platform_key: Override for ``sys.platform``. Used by tests
                to exercise per-platform deep-link selection.
        """
        self._probe_fn = probe_fn
        self._cascade_fn = cascade_fn
        self._capture_overrides = capture_overrides
        self._platform_key = platform_key or sys.platform

    async def run(
        self,
        *,
        endpoint_guid: str,
        device_index: int,
        preferred_combo: Combo,
        device_friendly_name: str = "",
        device_interface_name: str = "",
        device_class: str = "",
        endpoint_fxproperties_sha: str = "",
        detected_apos: Sequence[str] = (),
    ) -> WizardReport:
        """Execute the wizard flow for the given endpoint.

        Args:
            endpoint_guid: Stable endpoint GUID (MMDevice id on
                Windows, ALSA card+device on Linux, CoreAudio UID on
                macOS).
            device_index: PortAudio device index used by the probe.
            preferred_combo: Combo to try first. Callers typically pick
                the cascade table's first entry for the platform, or
                the winning combo from a prior :class:`ComboStore`
                entry when re-probing.
            device_friendly_name: Shown by the UI. Persisted to
                :class:`CaptureOverrides` on pin.
            device_interface_name: PortAudio device name (used for the
                13 invalidation rules on the store).
            device_class: Platform-specific device class bucket.
            endpoint_fxproperties_sha: Fingerprint for capture-APO
                detection (Windows).
            detected_apos: APOs observed via
                :mod:`sovyx.voice._apo_detector`.

        Returns:
            :class:`WizardReport` describing the outcome with an
            actionable hint.
        """
        if not endpoint_guid:
            msg = "endpoint_guid must be non-empty"
            raise ValueError(msg)

        logger.info(
            "voice_wizard_started",
            endpoint_guid=endpoint_guid,
            device_index=device_index,
            platform=self._platform_key,
            detected_apos=list(detected_apos),
        )

        direct_probe = await self._probe_fn(
            endpoint_guid=endpoint_guid,
            device_index=device_index,
            combo=preferred_combo,
            mode=ProbeMode.WARM,
            platform_key=self._platform_key,
        )

        # Happy path — first combo healthy, short-circuit.
        if direct_probe.diagnosis is Diagnosis.HEALTHY:
            pinned = self._try_pin(
                endpoint_guid=endpoint_guid,
                device_friendly_name=device_friendly_name,
                combo=preferred_combo,
                source="wizard",
                reason="warm probe passed on first combo",
            )
            return self._build_report(
                outcome=WizardOutcome.PASSED_DIRECT,
                winning_combo=preferred_combo,
                direct_probe=direct_probe,
                cascade_result=None,
                pinned=pinned,
                endpoint_guid=endpoint_guid,
                details={
                    "attempts": 1,
                    "rms_db": direct_probe.rms_db,
                    "vad_max_prob": direct_probe.vad_max_prob,
                },
            )

        # Remediable-by-user diagnoses short-circuit the cascade.
        short_circuit_outcomes: Mapping[Diagnosis, WizardOutcome] = {
            Diagnosis.MUTED: WizardOutcome.MUTED,
            Diagnosis.LOW_SIGNAL: WizardOutcome.LOW_SIGNAL,
            Diagnosis.PERMISSION_DENIED: WizardOutcome.PERMISSION_DENIED,
        }
        if direct_probe.diagnosis in short_circuit_outcomes:
            outcome = short_circuit_outcomes[direct_probe.diagnosis]
            return self._build_report(
                outcome=outcome,
                winning_combo=None,
                direct_probe=direct_probe,
                cascade_result=None,
                pinned=False,
                endpoint_guid=endpoint_guid,
                details={"diagnosis": direct_probe.diagnosis.value},
            )

        # APO_DEGRADED / VAD_INSENSITIVE / other — run the cascade.
        degraded_diagnoses = {
            Diagnosis.APO_DEGRADED,
            Diagnosis.VAD_INSENSITIVE,
            Diagnosis.NO_SIGNAL,
            Diagnosis.FORMAT_MISMATCH,
            Diagnosis.SELF_FEEDBACK,
        }
        if direct_probe.diagnosis not in degraded_diagnoses:
            return self._build_report(
                outcome=WizardOutcome.OTHER,
                winning_combo=None,
                direct_probe=direct_probe,
                cascade_result=None,
                pinned=False,
                endpoint_guid=endpoint_guid,
                details={"diagnosis": direct_probe.diagnosis.value},
            )

        logger.info(
            "voice_wizard_cascade_fallback",
            endpoint_guid=endpoint_guid,
            diagnosis=direct_probe.diagnosis.value,
        )
        cascade_result = await self._cascade_fn(
            endpoint_guid=endpoint_guid,
            device_index=device_index,
            mode=ProbeMode.WARM,
            platform_key=self._platform_key,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            device_class=device_class,
            endpoint_fxproperties_sha=endpoint_fxproperties_sha,
            detected_apos=detected_apos,
        )

        if cascade_result.winning_combo is not None:
            pinned = self._try_pin(
                endpoint_guid=endpoint_guid,
                device_friendly_name=device_friendly_name,
                combo=cascade_result.winning_combo,
                source="wizard",
                reason=(
                    f"cascade recovered from {direct_probe.diagnosis.value} "
                    f"in {cascade_result.attempts_count} attempt(s)"
                ),
            )
            return self._build_report(
                outcome=WizardOutcome.PASSED_VIA_CASCADE,
                winning_combo=cascade_result.winning_combo,
                direct_probe=direct_probe,
                cascade_result=cascade_result,
                pinned=pinned,
                endpoint_guid=endpoint_guid,
                details={
                    "attempts": cascade_result.attempts_count,
                    "source": cascade_result.source,
                    "original_diagnosis": direct_probe.diagnosis.value,
                },
            )

        return self._build_report(
            outcome=WizardOutcome.DEGRADED_NO_COMBO,
            winning_combo=None,
            direct_probe=direct_probe,
            cascade_result=cascade_result,
            pinned=False,
            endpoint_guid=endpoint_guid,
            details={
                "attempts": cascade_result.attempts_count,
                "budget_exhausted": cascade_result.budget_exhausted,
                "original_diagnosis": direct_probe.diagnosis.value,
            },
        )

    # ── internal helpers ──────────────────────────────────────────────

    def _try_pin(
        self,
        *,
        endpoint_guid: str,
        device_friendly_name: str,
        combo: Combo,
        source: str,
        reason: str,
    ) -> bool:
        """Pin the winning combo, swallowing file-system errors.

        A pin failure must not degrade the wizard outcome — the user's
        capture works this session regardless. We log at WARNING level
        so the dashboard can surface the persistence failure without
        blocking the "healthy" conclusion.
        """
        if self._capture_overrides is None:
            return False
        try:
            self._capture_overrides.pin(
                endpoint_guid,
                device_friendly_name=device_friendly_name,
                combo=combo,
                source=source,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — pin is best-effort on IO failure
            logger.warning(
                "voice_wizard_pin_failed",
                endpoint_guid=endpoint_guid,
                error=str(exc),
                exc_info=True,
            )
            return False
        return True

    def _build_report(
        self,
        *,
        outcome: WizardOutcome,
        winning_combo: Combo | None,
        direct_probe: ProbeResult,
        cascade_result: CascadeResult | None,
        pinned: bool,
        endpoint_guid: str,
        details: Mapping[str, Any],
    ) -> WizardReport:
        """Assemble the :class:`WizardReport` with platform-aware deep link."""
        deep_link = _DEEP_LINK_BY_OUTCOME_AND_PLATFORM.get((outcome, self._platform_key), "")
        report = WizardReport(
            outcome=outcome,
            hint=_HINT_BY_OUTCOME[outcome],
            deep_link=deep_link,
            winning_combo=winning_combo,
            direct_probe=direct_probe,
            cascade_result=cascade_result,
            pinned=pinned,
            endpoint_guid=endpoint_guid,
            details=dict(details),
        )
        logger.info(
            "voice_wizard_completed",
            endpoint_guid=endpoint_guid,
            outcome=outcome.value,
            pinned=pinned,
            had_cascade=cascade_result is not None,
        )
        return report


__all__ = [
    "CascadeFn",
    "ProbeFn",
    "VoiceSetupWizard",
    "WizardOutcome",
    "WizardReport",
]
