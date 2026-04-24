"""L2.5 mixer-sanity orchestrator — bidirectional mixer healing (F1.E).

The entry point :func:`check_and_maybe_heal` runs inside
:func:`~sovyx.voice.health.cascade.run_cascade`, after the pinned-
override + ComboStore fast-paths and before the platform cascade
walk. It probes the ALSA mixer, classifies the regime, detects user
customization, and — when appropriate — applies a KB-driven preset,
validates the result, and persists via ``alsactl store -f``. Full
rollback fires on any validation or apply failure.

This module is the behavioural heart of L2.5. It wires together:

* :mod:`~sovyx.voice.health._linux_mixer_probe` — mixer state read
* :mod:`~sovyx.voice.health._mixer_roles` — role resolution
* :mod:`~sovyx.voice.health._mixer_kb` — KB match + scoring
* :mod:`~sovyx.voice.health._linux_mixer_apply` — apply + rollback
* A caller-injected ``validation_probe_fn`` — post-apply signal check
* A caller-injected ``persist_fn`` — ``alsactl store`` wrapper

Every external side-effect is injected through a typed callable so
the state machine is fully mockable in unit tests. Default
implementations live at the bottom of the file and are used in
production.

See V2 Master Plan Part C.1 (placement), C.2 (state machine), E.1
(public API), E.5 (customization heuristic), E.6 (validation).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted alsa-utils binary
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias, runtime_checkable

from sovyx.observability.logging import get_logger
from sovyx.voice.health._linux_mixer_apply import (
    apply_mixer_preset as _default_apply_mixer_preset,
)
from sovyx.voice.health._linux_mixer_apply import (
    restore_mixer_snapshot as _default_restore_mixer_snapshot,
)
from sovyx.voice.health._linux_mixer_probe import (
    enumerate_alsa_mixer_snapshots as _default_mixer_probe,
)
from sovyx.voice.health._mixer_kb.matcher import _match_factory_signature
from sovyx.voice.health.contract import (
    Diagnosis,
    MixerSanityDecision,
    MixerSanityResult,
    RemediationHint,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._mixer_kb import MixerKBLookup, MixerKBMatch
    from sovyx.voice.health._mixer_roles import MixerControlRoleResolver
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import (
        CandidateEndpoint,
        HardwareContext,
        MixerApplySnapshot,
        MixerCardSnapshot,
        MixerValidationMetrics,
        ValidationGates,
    )

logger = get_logger(__name__)


# ── DI type aliases ──────────────────────────────────────────────────


MixerProbeFn: TypeAlias = "Callable[[], Sequence[MixerCardSnapshot]]"
"""Reads current mixer state. Defaults to
:func:`~sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots`.
Synchronous because the default wraps ``amixer`` subprocess calls
with their own internal timeout and the caller runs this in a
thread-bounded context.
"""


MixerApplyFn: TypeAlias = "Callable[..., Awaitable[MixerApplySnapshot]]"
"""Applies a KB preset. Signature matches
:func:`~sovyx.voice.health._linux_mixer_apply.apply_mixer_preset`.
"""


MixerRestoreFn: TypeAlias = "Callable[..., Awaitable[None]]"
"""Restores a mixer snapshot. Signature matches
:func:`~sovyx.voice.health._linux_mixer_apply.restore_mixer_snapshot`.
"""


ValidationProbeFn: TypeAlias = (
    "Callable[[CandidateEndpoint, VoiceTuningConfig], Awaitable[MixerValidationMetrics]]"
)
"""Runs the post-apply audio probe + metric composition.

Callers with real ``capture_raw_fn`` + ``wake_word_probe_fn`` inject
a richer implementation; the state machine does not care about
acquisition strategy.
"""


PersistFn: TypeAlias = "Callable[[Sequence[int], VoiceTuningConfig], Awaitable[bool]]"
"""Persists a set of ALSA cards via ``alsactl store``. Returns
``True`` on success. Default: :func:`default_persist_via_alsactl`.
"""


@runtime_checkable
class _TelemetryProto(Protocol):
    """Minimal surface L2.5 needs from
    :class:`~sovyx.voice.health._telemetry.VoiceHealthTelemetry`.

    Defined as a Protocol so tests can inject a no-op stub; production
    wires the real telemetry singleton via
    :func:`~sovyx.voice.health._telemetry.get_telemetry`.
    """

    def record_mixer_sanity_outcome(
        self,
        *,
        decision: str,
        matched_profile: str | None,
        score: float,
    ) -> None: ...


class _NoopTelemetry:
    """Fallback when the telemetry singleton is disabled or absent."""

    def record_mixer_sanity_outcome(
        self,
        *,
        decision: str,  # noqa: ARG002 — Protocol conformance
        matched_profile: str | None,  # noqa: ARG002
        score: float,  # noqa: ARG002
    ) -> None:
        """No-op — L2.5 respects the user's ``telemetry.enabled=False``."""
        return


# ── Customization heuristic (V2 Master Plan §E.5) ───────────────────


_SIGNAL_WEIGHTS: Mapping[str, float] = {
    # Order matches V2 Master Plan §E.5 bullet list. Sums to 1.0.
    "A_mixer_differs_from_factory": 0.30,
    "B_asoundrc_exists": 0.15,
    "C_pipewire_user_conf": 0.15,
    "D_asound_state_recent": 0.15,
    "E_wireplumber_user_conf": 0.10,
    "F_combo_store_has_entry_with_drift": 0.10,
    "G_capture_overrides_pinned": 0.05,
}
"""Per-signal weights for the user-customization heuristic.

Treat this mapping as the single source of truth for tests — any
reweighting MUST update every doctest + test assertion that depends
on the 0-1 total. Rebalancing requires an ADR amendment (see
ADR-voice-mixer-sanity-l2.5-bidirectional §4.I4).
"""


_ASOUND_STATE_RECENT_SECONDS: float = 7 * 24 * 3600.0
"""A mtime within the last 7 days on ``/var/lib/alsa/asound.state``
counts as "user tweaked recently". Shorter than the pilot-case
tolerance (factory-bad state rewrites the file on every boot — one
week excludes that).
"""


@dataclass(frozen=True, slots=True)
class _UserCustomizationReport:
    """Per-signal breakdown — for telemetry + test introspection."""

    score: float
    signals_fired: tuple[str, ...]


def detect_user_customization(
    *,
    factory_signature_score: float,
    hw: HardwareContext,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    endpoint_guid: str | None = None,
    home_dir: Path | None = None,
    asound_state_path: Path | None = None,
    time_now_s: float | None = None,
) -> _UserCustomizationReport:
    """Score user-customization likelihood in ``[0, 1]`` via 7 signals.

    All filesystem paths are injectable so tests can pin ``home_dir``
    and ``asound_state_path`` at a ``tmp_path`` fixture without
    touching the real user environment.

    Signal semantics (matching V2 §E.5):

    * **A** — current mixer deviates from the matched KB profile's
      factory signature. When ``factory_signature_score`` is low the
      mixer is unlike the factory-bad regime → user has moved it.
      Contributes ``(1.0 - factory_signature_score) * 0.30``.
    * **B** — ``~/.asoundrc`` exists. Explicit user config.
    * **C** — any file under ``~/.config/pipewire/pipewire.conf.d/``
      suggests PipeWire tuning.
    * **D** — ``/var/lib/alsa/asound.state`` mtime within the last
      7 days (``_ASOUND_STATE_RECENT_SECONDS``).
    * **E** — any file under
      ``~/.config/wireplumber/wireplumber.conf.d/``.
    * **F** — ``ComboStore`` has a recorded entry for this
      endpoint AND the factory-signature score is below 0.5
      (meaning "user got this working outside the factory-bad
      regime").
    * **G** — ``CaptureOverrides`` has a pinned combo for this
      endpoint (hard signal: user explicitly pinned a config).

    Args:
        factory_signature_score: ``0..1`` fraction from the matched
            profile's factory-signature check. Lower → stronger
            customization signal.
        hw: Detected hardware context. Currently consumed only by
            signal A via the factory score; kept in the signature
            so future signals (per-codec quirks) fit without an API
            break.
        combo_store: ``ComboStore`` singleton. ``None`` disables
            signal F (tests may choose to skip).
        capture_overrides: ``CaptureOverrides`` singleton. ``None``
            disables signal G.
        endpoint_guid: Needed to key into combo_store /
            capture_overrides. ``None`` disables F + G.
        home_dir: User home directory. Defaults to
            :meth:`Path.home()`; injected in tests.
        asound_state_path: Absolute path to asound.state. Defaults to
            ``/var/lib/alsa/asound.state``; injected in tests.
        time_now_s: ``time.time()`` override for deterministic
            mtime comparison in tests.

    Returns:
        :class:`_UserCustomizationReport` with the composite score
        and the list of signal codes that fired.
    """
    # Signal A is continuous — partial credit per plan. Every other
    # signal is boolean (present → full weight).
    del hw  # reserved for future per-codec quirks
    signals_fired: list[str] = []
    total: float = 0.0

    a_contribution = (
        max(0.0, 1.0 - float(factory_signature_score))
        * _SIGNAL_WEIGHTS["A_mixer_differs_from_factory"]
    )
    if a_contribution > 0:
        signals_fired.append("A_mixer_differs_from_factory")
    total += a_contribution

    home = home_dir if home_dir is not None else Path.home()

    if (home / ".asoundrc").exists():
        signals_fired.append("B_asoundrc_exists")
        total += _SIGNAL_WEIGHTS["B_asoundrc_exists"]

    pipewire_conf_d = home / ".config" / "pipewire" / "pipewire.conf.d"
    if _directory_has_configs(pipewire_conf_d):
        signals_fired.append("C_pipewire_user_conf")
        total += _SIGNAL_WEIGHTS["C_pipewire_user_conf"]

    asound_path = (
        asound_state_path if asound_state_path is not None else Path("/var/lib/alsa/asound.state")
    )
    now = time_now_s if time_now_s is not None else time.time()
    if _file_mtime_recent(asound_path, now=now, window_s=_ASOUND_STATE_RECENT_SECONDS):
        signals_fired.append("D_asound_state_recent")
        total += _SIGNAL_WEIGHTS["D_asound_state_recent"]

    wireplumber_conf_d = home / ".config" / "wireplumber" / "wireplumber.conf.d"
    if _directory_has_configs(wireplumber_conf_d):
        signals_fired.append("E_wireplumber_user_conf")
        total += _SIGNAL_WEIGHTS["E_wireplumber_user_conf"]

    if (
        combo_store is not None
        and endpoint_guid is not None
        and combo_store.get(endpoint_guid) is not None
        and factory_signature_score < 0.5  # noqa: PLR2004 — §E.5 threshold
    ):
        signals_fired.append("F_combo_store_has_entry_with_drift")
        total += _SIGNAL_WEIGHTS["F_combo_store_has_entry_with_drift"]

    if (
        capture_overrides is not None
        and endpoint_guid is not None
        and capture_overrides.get_entry(endpoint_guid) is not None
    ):
        signals_fired.append("G_capture_overrides_pinned")
        total += _SIGNAL_WEIGHTS["G_capture_overrides_pinned"]

    clamped = min(1.0, max(0.0, total))
    return _UserCustomizationReport(
        score=clamped,
        signals_fired=tuple(signals_fired),
    )


def _directory_has_configs(directory: Path) -> bool:
    """Return True iff ``directory`` exists AND contains at least one
    ``*.conf`` file. Non-existence is the dominant case — users who
    never tuned PipeWire / WirePlumber don't have these paths at all.
    """
    try:
        if not directory.is_dir():
            return False
        return any(entry.suffix == ".conf" for entry in directory.iterdir())
    except OSError:
        # Permission denied / transient I/O — don't fire the signal.
        return False


def _file_mtime_recent(path: Path, *, now: float, window_s: float) -> bool:
    """Return True iff ``path`` exists and its mtime is within
    ``window_s`` seconds of ``now``. Any OSError → False.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (now - mtime) <= window_s


# ── State machine ───────────────────────────────────────────────────


_StepName: TypeAlias = Literal[
    "probe",
    "classify",
    "detect_customization",
    "apply",
    "validate",
    "persist",
    "rollback",
    "done",
]


@dataclass(slots=True)
class _OrchestratorContext:
    """Mutable state passed between steps. Private to the orchestrator.

    Every field starts ``None``/empty and fills in as the state
    machine advances. The final :meth:`build_result` reads this state
    into an immutable :class:`MixerSanityResult`.
    """

    endpoint: CandidateEndpoint
    hw: HardwareContext
    tuning: VoiceTuningConfig
    start_time_s: float

    # Injected callables
    mixer_probe_fn: MixerProbeFn
    mixer_apply_fn: MixerApplyFn
    mixer_restore_fn: MixerRestoreFn
    kb_lookup: MixerKBLookup
    role_resolver: MixerControlRoleResolver
    validation_probe_fn: ValidationProbeFn
    persist_fn: PersistFn
    telemetry: _TelemetryProto
    combo_store: ComboStore | None = None
    capture_overrides: CaptureOverrides | None = None

    # Filled as the state machine progresses
    mixer_snapshot: tuple[MixerCardSnapshot, ...] = ()
    kb_match: MixerKBMatch | None = None
    customization: _UserCustomizationReport | None = None
    apply_snapshot: MixerApplySnapshot | None = None
    validation_metrics: MixerValidationMetrics | None = None
    validation_passed: bool | None = None
    probe_duration_ms: int = 0
    apply_duration_ms: int | None = None
    diagnosis_before: Diagnosis = Diagnosis.UNKNOWN
    diagnosis_after: Diagnosis | None = None
    regime: Literal["saturation", "attenuation", "mixed", "healthy", "unknown"] = "unknown"
    decision: MixerSanityDecision | None = None
    error_token: str | None = None
    remediation: RemediationHint | None = None
    # Persist outcome — False/None means the preset applied but
    # survives only until reboot.
    persist_succeeded: bool | None = None

    def controls_modified(self) -> tuple[str, ...]:
        """Names of controls actually mutated, as a flat tuple."""
        if self.apply_snapshot is None:
            return ()
        return tuple(name for name, _ in self.apply_snapshot.applied_controls)

    def cards_probed(self) -> tuple[int, ...]:
        return tuple(card.card_index for card in self.mixer_snapshot)

    def budget_exceeded(self) -> bool:
        # ``>=`` rather than ``>``: the budget is a hard cap — the
        # orchestrator must terminate BY that wall-clock, not AT or
        # beyond. Also makes ``budget_s=0`` a deterministic
        # "fail-fast" for tests (under low-resolution monotonic
        # clocks the strict ``>`` yields 0 > 0 on the very first
        # check, which would never fire).
        elapsed = time.monotonic() - self.start_time_s
        return elapsed >= self.tuning.linux_mixer_sanity_budget_s


@dataclass(frozen=True, slots=True)
class _StepResult:
    """Outcome of one step — names the next step."""

    next_step: _StepName


# ── Public entry point ──────────────────────────────────────────────


async def check_and_maybe_heal(
    endpoint: CandidateEndpoint,
    hw: HardwareContext,
    *,
    kb_lookup: MixerKBLookup,
    role_resolver: MixerControlRoleResolver,
    validation_probe_fn: ValidationProbeFn,
    tuning: VoiceTuningConfig,
    mixer_probe_fn: MixerProbeFn | None = None,
    mixer_apply_fn: MixerApplyFn | None = None,
    mixer_restore_fn: MixerRestoreFn | None = None,
    persist_fn: PersistFn | None = None,
    telemetry: _TelemetryProto | None = None,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
) -> MixerSanityResult:
    """Run the full L2.5 state machine for ``endpoint``.

    Returns a :class:`MixerSanityResult` whose
    :attr:`MixerSanityResult.decision` the cascade keys off to decide
    whether to skip the platform walk (``HEALED``) or continue
    (``SKIPPED_*``, ``DEFERRED_*``, ``ROLLED_BACK``, ``ERROR``).

    Timings: target ≤ 3 s typical; hard abort at
    :attr:`VoiceTuningConfig.linux_mixer_sanity_budget_s` (default
    5 s). On timeout, any in-flight apply is rolled back and the
    decision becomes ``ERROR`` with
    ``error=MIXER_SANITY_BUDGET_EXCEEDED``.

    Platform: Linux only in F1. Non-Linux callers receive
    ``DEFERRED_PLATFORM`` with no side-effects.
    """
    if sys.platform != "linux":
        logger.debug(
            "mixer_sanity_non_linux_defer",
            platform=sys.platform,
            endpoint_guid=endpoint.endpoint_guid,
        )
        return _defer_platform_result()

    ctx = _OrchestratorContext(
        endpoint=endpoint,
        hw=hw,
        tuning=tuning,
        start_time_s=time.monotonic(),
        mixer_probe_fn=mixer_probe_fn if mixer_probe_fn is not None else _default_mixer_probe,
        mixer_apply_fn=mixer_apply_fn
        if mixer_apply_fn is not None
        else _default_apply_mixer_preset,
        mixer_restore_fn=mixer_restore_fn
        if mixer_restore_fn is not None
        else _default_restore_mixer_snapshot,
        kb_lookup=kb_lookup,
        role_resolver=role_resolver,
        validation_probe_fn=validation_probe_fn,
        persist_fn=persist_fn if persist_fn is not None else default_persist_via_alsactl,
        telemetry=telemetry if telemetry is not None else _NoopTelemetry(),
        combo_store=combo_store,
        capture_overrides=capture_overrides,
    )

    orchestrator = _SanityOrchestrator(ctx)
    try:
        await orchestrator.run()
    except asyncio.CancelledError:
        # Caller cancelled mid-run — attempt rollback, re-raise.
        await orchestrator.rollback_if_needed()
        raise
    except BaseException as exc:  # noqa: BLE001 — defensive: any unhandled error becomes ERROR
        logger.exception(
            "mixer_sanity_unexpected_error",
            endpoint_guid=endpoint.endpoint_guid,
            error_type=type(exc).__name__,
        )
        await orchestrator.rollback_if_needed()
        ctx.decision = MixerSanityDecision.ERROR
        ctx.error_token = "MIXER_SANITY_UNEXPECTED_ERROR"

    result = orchestrator.build_result()
    ctx.telemetry.record_mixer_sanity_outcome(
        decision=result.decision.value,
        matched_profile=result.matched_kb_profile,
        score=result.kb_match_score,
    )
    return result


# ── Orchestrator ────────────────────────────────────────────────────


class _SanityOrchestrator:
    """Drives the 7-step state machine.

    Each step reads ``self._ctx``, makes one decision, and returns a
    :class:`_StepResult` naming the next step. :meth:`run` is the
    dispatch loop.
    """

    def __init__(self, ctx: _OrchestratorContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        """Run the state machine from entry to done."""
        step = "probe"
        while step != "done":
            if self._ctx.budget_exceeded():
                logger.warning(
                    "mixer_sanity_budget_exceeded",
                    endpoint_guid=self._ctx.endpoint.endpoint_guid,
                    step=step,
                )
                self._ctx.decision = MixerSanityDecision.ERROR
                self._ctx.error_token = "MIXER_SANITY_BUDGET_EXCEEDED"
                await self.rollback_if_needed()
                return
            match step:
                case "probe":
                    result = await self._step_probe()
                case "classify":
                    result = await self._step_classify()
                case "detect_customization":
                    result = await self._step_detect_customization()
                case "apply":
                    result = await self._step_apply()
                case "validate":
                    result = await self._step_validate()
                case "persist":
                    result = await self._step_persist()
                case "rollback":
                    result = await self._step_rollback()
                case _:  # pragma: no cover — exhaustiveness
                    msg = f"unexpected step {step!r}"
                    raise RuntimeError(msg)
            step = result.next_step

    def build_result(self) -> MixerSanityResult:
        """Freeze current context into the terminal
        :class:`MixerSanityResult` record.
        """
        c = self._ctx
        decision = c.decision if c.decision is not None else MixerSanityDecision.ERROR
        match = c.kb_match
        return MixerSanityResult(
            decision=decision,
            diagnosis_before=c.diagnosis_before,
            diagnosis_after=c.diagnosis_after,
            regime=c.regime,
            matched_kb_profile=match.profile.profile_id if match is not None else None,
            kb_match_score=match.score if match is not None else 0.0,
            user_customization_score=(
                c.customization.score if c.customization is not None else 0.0
            ),
            cards_probed=c.cards_probed(),
            controls_modified=c.controls_modified(),
            rollback_snapshot=c.apply_snapshot,
            probe_duration_ms=c.probe_duration_ms,
            apply_duration_ms=c.apply_duration_ms,
            validation_passed=c.validation_passed,
            validation_metrics=c.validation_metrics,
            remediation=c.remediation,
            error=c.error_token,
        )

    async def rollback_if_needed(self) -> None:
        """Invoke ``mixer_restore_fn`` if we have an apply snapshot.

        Best-effort — rollback failures are logged and swallowed so
        the caller's exception semantics are preserved.
        """
        if self._ctx.apply_snapshot is None:
            return
        try:
            await self._ctx.mixer_restore_fn(
                self._ctx.apply_snapshot,
                tuning=self._ctx.tuning,
            )
        except BaseException as exc:  # noqa: BLE001 — rollback must never propagate
            logger.warning(
                "mixer_sanity_rollback_failed",
                endpoint_guid=self._ctx.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )

    # ── Individual steps ────────────────────────────────────────────

    async def _step_probe(self) -> _StepResult:
        """Read current mixer state. Failure → ERROR (no rollback)."""
        c = self._ctx
        probe_start = time.monotonic()
        try:
            snapshots = await asyncio.to_thread(c.mixer_probe_fn)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(
                "mixer_sanity_probe_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.decision = MixerSanityDecision.ERROR
            c.error_token = "MIXER_SANITY_PROBE_FAILED"
            return _StepResult(next_step="done")
        c.mixer_snapshot = tuple(snapshots)
        c.probe_duration_ms = int((time.monotonic() - probe_start) * 1000)
        if not c.mixer_snapshot:
            # No cards → nothing to heal. Deferring cleanly means the
            # cascade proceeds — user may be on a USB-only / BT-only
            # setup that doesn't expose the HDA-style mixer surface.
            logger.debug(
                "mixer_sanity_no_cards",
                endpoint_guid=c.endpoint.endpoint_guid,
            )
            c.decision = MixerSanityDecision.DEFERRED_NO_KB
            c.regime = "unknown"
            c.error_token = "MIXER_SANITY_NO_CARDS"
            return _StepResult(next_step="done")
        return _StepResult(next_step="classify")

    async def _step_classify(self) -> _StepResult:
        """Match KB + assign regime + decide next step."""
        c = self._ctx
        match = c.kb_lookup.match(
            c.hw,
            c.mixer_snapshot,
            min_score=c.tuning.linux_mixer_sanity_kb_match_threshold,
        )
        c.kb_match = match
        if match is None:
            # No profile matched above threshold OR ambiguous.
            # Distinguish via the lookup's own logging; for the result
            # we keep the aggregated "no actionable KB" bucket.
            c.regime = _classify_regime_heuristically(c.mixer_snapshot)
            if c.regime == "healthy":
                c.decision = MixerSanityDecision.SKIPPED_HEALTHY
                c.diagnosis_before = Diagnosis.HEALTHY
                return _StepResult(next_step="done")
            c.decision = MixerSanityDecision.DEFERRED_NO_KB
            c.diagnosis_before = Diagnosis.MIXER_UNKNOWN_PATTERN
            c.error_token = "MIXER_SANITY_NO_KB_MATCH"
            c.remediation = RemediationHint(
                code="remediation.mixer_unknown",
                severity="info",
            )
            return _StepResult(next_step="done")
        # We have a KB profile. Assign regime from the profile and
        # the observed state; diagnosis is MIXER_ZEROED /
        # MIXER_SATURATED based on match.profile.factory_regime.
        regime = match.profile.factory_regime
        # `"either"` is a KB-author hint that this profile targets both
        # regimes; at classification time we still need a concrete
        # label, so fall back to the probe-based heuristic.
        if regime == "attenuation":
            c.regime = "attenuation"
        elif regime == "saturation":
            c.regime = "saturation"
        elif regime == "mixed":
            c.regime = "mixed"
        else:  # "either"
            c.regime = _classify_regime_heuristically(c.mixer_snapshot)
        c.diagnosis_before = _diagnosis_for_regime(c.regime)
        return _StepResult(next_step="detect_customization")

    async def _step_detect_customization(self) -> _StepResult:
        """Run the 7-signal heuristic + branch APPLY / DEFER / SKIP."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        # Compute the factory-signature score separately so the heuristic
        # can score signal A accurately. (The kb_lookup's composite score
        # mixes in other fields.)
        factory_score = _match_factory_signature(
            c.kb_match.profile.factory_signature,
            c.mixer_snapshot,
            c.role_resolver,
            c.hw,
        )
        c.customization = detect_user_customization(
            factory_signature_score=factory_score,
            hw=c.hw,
            combo_store=c.combo_store,
            capture_overrides=c.capture_overrides,
            endpoint_guid=c.endpoint.endpoint_guid,
        )
        score = c.customization.score
        if score > c.tuning.linux_mixer_user_customization_threshold_skip:
            c.decision = MixerSanityDecision.SKIPPED_CUSTOMIZED
            c.diagnosis_before = Diagnosis.MIXER_CUSTOMIZED
            c.remediation = RemediationHint(
                code="remediation.mixer_customized",
                severity="info",
            )
            logger.info(
                "mixer_sanity_skipped_customized",
                endpoint_guid=c.endpoint.endpoint_guid,
                customization_score=score,
                signals=list(c.customization.signals_fired),
            )
            return _StepResult(next_step="done")
        if score >= c.tuning.linux_mixer_user_customization_threshold_apply:
            # Ambiguous zone — defer; dashboard card (F1.I) offers choice.
            c.decision = MixerSanityDecision.DEFERRED_AMBIGUOUS
            c.error_token = "MIXER_SANITY_USER_CUSTOMIZED_AMBIGUOUS"
            c.remediation = RemediationHint(
                code="remediation.mixer_customized",
                severity="info",
            )
            logger.info(
                "mixer_sanity_deferred_customization_ambiguous",
                endpoint_guid=c.endpoint.endpoint_guid,
                customization_score=score,
            )
            return _StepResult(next_step="done")
        return _StepResult(next_step="apply")

    async def _step_apply(self) -> _StepResult:
        """Apply the KB preset to every probed card."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        apply_start = time.monotonic()
        # Apply to the card whose controls best fit the profile.
        # Multi-card systems are rare on laptops; F1 targets the
        # first card — F2 can extend to multi-card selection by
        # scoring each card independently.
        target_card = c.mixer_snapshot[0]
        role_mapping = c.role_resolver.resolve_card(target_card, c.hw)
        try:
            c.apply_snapshot = await c.mixer_apply_fn(
                target_card.card_index,
                c.kb_match.profile.recommended_preset,
                role_mapping,
                tuning=c.tuning,
            )
        except Exception as exc:  # noqa: BLE001 — translate to ERROR decision
            logger.warning(
                "mixer_sanity_apply_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                profile_id=c.kb_match.profile.profile_id,
                detail=str(exc)[:200],
            )
            c.decision = MixerSanityDecision.ERROR
            c.error_token = "MIXER_SANITY_APPLY_FAILED"
            # apply_mixer_preset already rolled back internally; our
            # snapshot stays None.
            return _StepResult(next_step="done")
        c.apply_duration_ms = int((time.monotonic() - apply_start) * 1000)
        return _StepResult(next_step="validate")

    async def _step_validate(self) -> _StepResult:
        """Run post-apply validation; gates pass → persist, fail → rollback."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        try:
            metrics = await c.validation_probe_fn(c.endpoint, c.tuning)
        except Exception as exc:  # noqa: BLE001 — validation failure ⇒ rollback
            logger.warning(
                "mixer_sanity_validation_probe_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.validation_passed = False
            c.error_token = "MIXER_SANITY_VALIDATION_FAILED"
            return _StepResult(next_step="rollback")
        c.validation_metrics = metrics
        gates = c.kb_match.profile.validation_gates
        passed = _check_validation_gates(metrics, gates)
        c.validation_passed = passed
        if passed:
            c.diagnosis_after = Diagnosis.HEALTHY
            return _StepResult(next_step="persist")
        c.error_token = "MIXER_SANITY_VALIDATION_FAILED"
        logger.info(
            "mixer_sanity_validation_gates_failed",
            endpoint_guid=c.endpoint.endpoint_guid,
            metrics=metrics,
        )
        return _StepResult(next_step="rollback")

    async def _step_persist(self) -> _StepResult:
        """alsactl store — best-effort, HEALED either way."""
        c = self._ctx
        try:
            c.persist_succeeded = await c.persist_fn(c.cards_probed(), c.tuning)
        except BaseException as exc:  # noqa: BLE001 — persist is best-effort
            logger.warning(
                "mixer_sanity_persist_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.persist_succeeded = False
        if c.persist_succeeded is False:
            c.error_token = "MIXER_SANITY_PERSIST_FAILED"
        c.decision = MixerSanityDecision.HEALED
        c.remediation = RemediationHint(
            code=(
                "remediation.mixer_zeroed"
                if c.regime == "attenuation"
                else "remediation.mixer_saturated"
            ),
            severity="info",
        )
        return _StepResult(next_step="done")

    async def _step_rollback(self) -> _StepResult:
        """Explicit rollback after validation failure."""
        c = self._ctx
        await self.rollback_if_needed()
        c.decision = MixerSanityDecision.ROLLED_BACK
        c.diagnosis_after = c.diagnosis_before
        return _StepResult(next_step="done")


# ── Pure helpers ────────────────────────────────────────────────────


def _check_validation_gates(
    metrics: MixerValidationMetrics,
    gates: ValidationGates,
) -> bool:
    """Every declared gate must pass — any single failure → False."""
    lo, hi = gates.rms_dbfs_range
    if not (lo <= metrics.rms_dbfs <= hi):
        return False
    if metrics.peak_dbfs > gates.peak_dbfs_max:
        return False
    if metrics.snr_db_vocal_band < gates.snr_db_vocal_band_min:
        return False
    if metrics.silero_max_prob < gates.silero_prob_min:
        return False
    return metrics.wake_word_stage2_prob >= gates.wake_word_stage2_prob_min


def _classify_regime_heuristically(
    snapshot: Sequence[MixerCardSnapshot],
) -> Literal["saturation", "attenuation", "mixed", "healthy", "unknown"]:
    """Fallback regime classification when no KB profile matches.

    Looks at the probe's own saturation flags + aggregated boost dB
    to split ``"saturation"`` / ``"healthy"`` / ``"unknown"``.
    Attenuation has no reliable signal without KB knowledge (a low
    Capture can be intentional), so we return ``"unknown"`` rather
    than guess.
    """
    if not snapshot:
        return "unknown"
    for card in snapshot:
        if card.saturation_warning:
            return "saturation"
    # No saturation flags → assume healthy; probe didn't surface any
    # obvious red flags.
    return "healthy"


def _diagnosis_for_regime(
    regime: Literal["saturation", "attenuation", "mixed", "healthy", "unknown"],
) -> Diagnosis:
    """Map a regime label to the L2.5 Diagnosis value."""
    if regime == "attenuation":
        return Diagnosis.MIXER_ZEROED
    if regime == "saturation":
        return Diagnosis.MIXER_SATURATED
    if regime == "mixed":
        return Diagnosis.MIXER_SATURATED  # bias to the more actionable side
    if regime == "healthy":
        return Diagnosis.HEALTHY
    return Diagnosis.MIXER_UNKNOWN_PATTERN


def _defer_platform_result() -> MixerSanityResult:
    """Build the canonical DEFERRED_PLATFORM result shape."""
    return MixerSanityResult(
        decision=MixerSanityDecision.DEFERRED_PLATFORM,
        diagnosis_before=Diagnosis.UNKNOWN,
        diagnosis_after=None,
        regime="unknown",
        matched_kb_profile=None,
        kb_match_score=0.0,
        user_customization_score=0.0,
        cards_probed=(),
        controls_modified=(),
        rollback_snapshot=None,
        probe_duration_ms=0,
        apply_duration_ms=None,
        validation_passed=None,
        validation_metrics=None,
    )


# ── Default persist (alsactl store) ─────────────────────────────────


async def default_persist_via_alsactl(
    cards: Sequence[int],
    tuning: VoiceTuningConfig,
) -> bool:
    """Persist the current mixer state for ``cards`` via ``alsactl store``.

    Runs ``alsactl store -f -c <card_index>`` per card. Uses the
    same subprocess timeout as the rest of the Linux mixer subsystem.
    Returns ``False`` on any failure (logger warns). Never raises.

    ``alsactl store`` requires the caller to have write access to
    ``/var/lib/alsa/asound.state`` — in practice this means running
    as root or via the systemd oneshot (F1.G). On failure, the
    preset survives only until reboot; re-apply on next boot.
    """
    if sys.platform != "linux":
        return False
    if shutil.which("alsactl") is None:
        logger.debug("mixer_sanity_alsactl_missing")
        return False
    timeout_s = tuning.linux_mixer_subprocess_timeout_s
    all_ok = True
    for card_index in cards:
        argv = ["alsactl", "store", "-f", "-c", str(card_index)]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,  # noqa: S603 — fixed argv, no shell, timeout enforced
                argv,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                text=True,
                errors="replace",
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning(
                "mixer_sanity_alsactl_store_subprocess_failed",
                card_index=card_index,
                detail=str(exc)[:200],
            )
            all_ok = False
            continue
        if proc.returncode != 0:
            logger.warning(
                "mixer_sanity_alsactl_store_nonzero",
                card_index=card_index,
                returncode=proc.returncode,
                stderr=(proc.stderr or "").strip()[:200],
            )
            all_ok = False
    return all_ok


__all__ = [
    "MixerApplyFn",
    "MixerProbeFn",
    "MixerRestoreFn",
    "PersistFn",
    "ValidationProbeFn",
    "check_and_maybe_heal",
    "default_persist_via_alsactl",
    "detect_user_customization",
]
