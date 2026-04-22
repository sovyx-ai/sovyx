"""OS-agnostic capture integrity probe + bypass-strategy coordinator.

This module is the public entry point for the Phase 1 resilience layer:

* :class:`CaptureIntegrityProbe` — consumes a snapshot of the live
  capture task's ring buffer (never opens a second stream) and
  classifies the signal into an :class:`IntegrityVerdict` using RMS +
  SileroVAD max probability + spectral flatness (Wiener entropy) + 85%
  energy roll-off. Detection is platform-neutral: the same probe
  classifies Windows Voice Clarity, PulseAudio ``module-echo-cancel``,
  and CoreAudio VPIO destruction patterns.

* :class:`CaptureIntegrityCoordinator` — accepts a deaf-signal
  heartbeat from the orchestrator and iterates the registered
  platform-specific :class:`PlatformBypassStrategy` list. The
  coordinator probes before and after each apply, advances on
  ``APPLIED_STILL_DEAD``, and quarantines the endpoint via
  :class:`EndpointQuarantine` when the strategy list is exhausted.

The separation between probe and coordinator is intentional: tests can
swap in a deterministic fake probe to exercise the coordinator's
state machine end-to-end without wiring a real capture pipeline.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import (
    record_apo_degraded_event,
    record_bypass_strategy_verdict,
    record_capture_integrity_verdict,
)
from sovyx.voice.health._quarantine import get_default_quarantine
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import (
    BypassContext,
    BypassOutcome,
    BypassVerdict,
    IntegrityResult,
    IntegrityVerdict,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._quarantine import EndpointQuarantine
    from sovyx.voice.health.bypass._strategy import PlatformBypassStrategy
    from sovyx.voice.health.contract import CaptureTaskProto
    from sovyx.voice.vad import SileroVAD

logger = get_logger(__name__)

# Silero v5 pipeline window — hard-coded by the model itself. Keeping
# the constant local rather than importing from ``vad`` breaks a
# circular-import risk (vad can stay leaf, capture_integrity can
# depend on it without re-export).
_VAD_WINDOW_SAMPLES = 512
_SAMPLE_RATE = 16_000

# Minimum int16 samples required for the probe to produce anything
# other than INCONCLUSIVE. At 16 kHz one VAD window is 32 ms; we
# require 4 windows ≈ 128 ms so spectral analysis has enough support.
_MIN_SAMPLES_FOR_ANALYSIS = _VAD_WINDOW_SAMPLES * 4

# Floor for log10(RMS) — matches :mod:`sovyx.voice._capture_task`. Kept
# private so the two modules can diverge if a future bugfix needs it.
_RMS_FLOOR_DB = -120.0

# Rolloff cumulative-energy fraction. 0.85 is the standard MIR choice
# (librosa default); tightening below 0.85 makes the probe more
# sensitive to low-pass filtering, loosening pushes toward false
# negatives. Left at the canonical 0.85.
_ROLLOFF_CUM_ENERGY = 0.85


def _compute_rms_db(frames: npt.NDArray[np.int16]) -> float:
    """Compute dBFS RMS of an int16 buffer — safe for silent / empty input."""
    import math

    import numpy as np

    if frames.size == 0:
        return _RMS_FLOOR_DB
    sample_sq = float(np.mean(np.square(frames.astype(np.float32) / 32768.0)))
    if sample_sq <= 0 or not math.isfinite(sample_sq):
        return _RMS_FLOOR_DB
    return 10.0 * math.log10(sample_sq)


def _compute_spectral_flatness(frames: npt.NDArray[np.int16]) -> float:
    """Wiener entropy of the magnitude spectrum in ``[0.0, 1.0]``.

    Defined as ``geometric_mean(|S|) / arithmetic_mean(|S|)`` with a
    small floor added before the logarithm to keep the geometric mean
    finite on bins with zero magnitude. Computed over the full
    concatenated snapshot (not per-frame) so a single noisy frame in a
    3-second window does not dominate.

    White noise → 1.0, pure tone → 0.0. Voice Clarity's post-destruction
    spectrum sits at 0.28–0.35; clean speech at 0.10–0.15.
    """
    import numpy as np

    if frames.size < _VAD_WINDOW_SAMPLES:
        return 0.0
    # float32 in [-1, 1] gives finite-energy magnitude spectrum.
    x = frames.astype(np.float32) / 32768.0
    # rfft picks out non-negative frequency bins — one-sided spectrum.
    mag = np.abs(np.fft.rfft(x))
    if mag.size == 0:
        return 0.0
    # Drop the DC bin — ambient DC offsets would otherwise inflate the
    # arithmetic mean and depress the ratio.
    mag = mag[1:]
    if mag.size == 0:
        return 0.0
    # Floor magnitudes so log(0) doesn't produce -inf in the geo mean.
    # 1e-10 is well below the int16 quantisation noise floor.
    mag_clipped = np.maximum(mag, 1e-10)
    geo_mean = float(np.exp(np.mean(np.log(mag_clipped))))
    arith_mean = float(np.mean(mag_clipped))
    if arith_mean <= 0:
        return 0.0
    flatness = geo_mean / arith_mean
    return max(0.0, min(1.0, flatness))


def _compute_spectral_rolloff_hz(frames: npt.NDArray[np.int16]) -> float:
    """Return the 85 %-cumulative-energy roll-off frequency in Hz.

    Iterates the one-sided magnitude-squared spectrum from DC upward
    and picks the frequency bin at which the cumulative energy crosses
    ``_ROLLOFF_CUM_ENERGY`` of the total. Voice Clarity's aggressive
    low-pass pulls this below 4 kHz; clean speech at the same vocal
    energy sits at 6–8 kHz.
    """
    import numpy as np

    if frames.size < _VAD_WINDOW_SAMPLES:
        return 0.0
    x = frames.astype(np.float32) / 32768.0
    mag = np.abs(np.fft.rfft(x))
    if mag.size < 2:
        return 0.0
    energy = np.square(mag)
    total = float(np.sum(energy))
    if total <= 0:
        return 0.0
    cumulative = np.cumsum(energy)
    target = _ROLLOFF_CUM_ENERGY * total
    idx = int(np.searchsorted(cumulative, target))
    idx = max(0, min(idx, mag.size - 1))
    # FFT bin i corresponds to i * sample_rate / N_fft. rfft on N real
    # samples yields N // 2 + 1 bins covering [0, Nyquist]; the mapping
    # idx → freq is linear.
    n_fft = (mag.size - 1) * 2
    if n_fft <= 0:
        return 0.0
    return float(idx) * _SAMPLE_RATE / float(n_fft)


class CaptureIntegrityProbe:
    """Warm integrity probe against a live :class:`CaptureTaskProto`.

    Stateless across probes (other than the injected VAD instance's
    LSTM state, which is reset at the start of each run). Cheap enough
    to invoke on every orchestrator deaf-heartbeat; 3 seconds of audio
    @ 16 kHz mono int16 is ~100 KB, the FFT is ~1 ms on a Pi 5, and
    SileroVAD costs ≈1 ms per window.

    Args:
        vad: A :class:`SileroVAD` instance reserved for probing. The
            probe calls :meth:`SileroVAD.reset` on every invocation so
            state does not leak between calls. Must not be the same
            instance the live pipeline uses — LSTM state interference
            would poison both.
        tuning: Frozen tuning snapshot. Re-read on every probe so a
            user re-tuning thresholds via ``SOVYX_TUNING__VOICE__*``
            takes effect on the next probe without a restart.
    """

    def __init__(
        self,
        *,
        vad: SileroVAD,
        tuning: VoiceTuningConfig | None = None,
    ) -> None:
        self._vad = vad
        self._tuning = tuning

    async def probe_warm(
        self,
        capture_task: CaptureTaskProto,
    ) -> IntegrityResult:
        """Sample the live capture ring and classify the signal.

        Returns an :class:`IntegrityResult` with every metric populated
        even on the INCONCLUSIVE branch so dashboards don't have to
        treat that verdict specially. Never raises — probe failures
        collapse into ``INCONCLUSIVE`` with a diagnostic ``detail``
        string for the observability trail.

        Args:
            capture_task: The live capture task. Must already be
                running; a stopped task produces INCONCLUSIVE.
        """
        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        duration_s = max(0.0, float(tuning.integrity_probe_duration_s))
        endpoint_guid = capture_task.active_device_guid or ""

        try:
            frames = await capture_task.tap_recent_frames(duration_s)
        except Exception as exc:  # noqa: BLE001 — probe must never raise upward
            logger.warning(
                "capture_integrity_probe_tap_failed",
                endpoint_guid=endpoint_guid,
                error=str(exc),
            )
            return self._inconclusive(endpoint_guid, duration_s, detail=f"tap_failed: {exc}")

        if frames.size < _MIN_SAMPLES_FOR_ANALYSIS:
            return self._inconclusive(
                endpoint_guid,
                duration_s,
                raw_frames=int(frames.size),
                detail="ring_buffer_underrun",
            )

        # The analysis itself is CPU-bound (NumPy FFT + SileroVAD ONNX
        # inference over up to ~90 windows). Keep the event loop free
        # for other coroutines — CLAUDE.md anti-pattern #14.
        return await asyncio.to_thread(
            self._analyse_sync,
            frames,
            endpoint_guid,
            duration_s,
            tuning,
        )

    # -- internals ------------------------------------------------------

    def _analyse_sync(
        self,
        frames: npt.NDArray[np.int16],
        endpoint_guid: str,
        duration_s: float,
        tuning: VoiceTuningConfig,
    ) -> IntegrityResult:
        import numpy as np

        rms_db = _compute_rms_db(frames)
        flatness = _compute_spectral_flatness(frames)
        rolloff_hz = _compute_spectral_rolloff_hz(frames)

        # Walk the snapshot in non-overlapping 512-sample windows. Any
        # residual tail is dropped — the spectral metrics already
        # cover the full buffer.
        self._vad.reset()
        vad_max = 0.0
        n_windows = frames.size // _VAD_WINDOW_SAMPLES
        for i in range(n_windows):
            start = i * _VAD_WINDOW_SAMPLES
            window = frames[start : start + _VAD_WINDOW_SAMPLES]
            if window.shape != (_VAD_WINDOW_SAMPLES,):
                continue
            try:
                event = self._vad.process_frame(window)
            except Exception as exc:  # noqa: BLE001 — ONNX hiccups must not crash probe
                logger.debug(
                    "capture_integrity_probe_vad_frame_failed",
                    error=str(exc),
                )
                continue
            if event.probability > vad_max:
                vad_max = float(event.probability)

        # Always reset after use so a subsequent probe starts clean.
        self._vad.reset()

        verdict = self._classify(
            rms_db=rms_db,
            vad_max=vad_max,
            flatness=flatness,
            rolloff_hz=rolloff_hz,
            tuning=tuning,
        )
        result = IntegrityResult(
            verdict=verdict,
            endpoint_guid=endpoint_guid,
            rms_db=float(rms_db),
            vad_max_prob=float(np.clip(vad_max, 0.0, 1.0)),
            spectral_flatness=float(flatness),
            spectral_rolloff_hz=float(rolloff_hz),
            duration_s=float(frames.size) / _SAMPLE_RATE,
            probed_at_utc=datetime.now(UTC),
            raw_frames=int(frames.size),
            detail="",
        )
        logger.info(
            "capture_integrity_probe_complete",
            endpoint_guid=endpoint_guid,
            verdict=verdict.value,
            rms_db=round(result.rms_db, 1),
            vad_max_prob=round(result.vad_max_prob, 3),
            spectral_flatness=round(result.spectral_flatness, 3),
            spectral_rolloff_hz=int(result.spectral_rolloff_hz),
            raw_frames=result.raw_frames,
        )
        return result

    def _classify(
        self,
        *,
        rms_db: float,
        vad_max: float,
        flatness: float,
        rolloff_hz: float,
        tuning: VoiceTuningConfig,
    ) -> IntegrityVerdict:
        """Decision tree over the four metrics.

        The ordering matters: the HEALTHY early-exit avoids even
        looking at spectral metrics when the VAD already proves the
        pipeline is intact, which keeps the probe robust against
        spurious spectral-envelope false positives in noisy offices.
        """
        # Active speech through a clean pipeline — nothing to bypass.
        if vad_max >= tuning.integrity_vad_healthy_max_prob_floor:
            return IntegrityVerdict.HEALTHY

        # Below the RMS floor — driver is open but not delivering audio.
        # Distinct fix path: the watchdog must re-cascade, not bypass.
        if rms_db < tuning.integrity_driver_silent_rms_ceiling_db:
            return IntegrityVerdict.DRIVER_SILENT

        # RMS audible, VAD not responsive. Is the spectrum destroyed?
        spectrum_degraded = (
            flatness > tuning.integrity_spectral_flatness_apo_ceiling
            or rolloff_hz < tuning.integrity_spectral_rolloff_apo_ceiling_hz
        )
        apo_signature = (
            rms_db >= tuning.integrity_apo_rms_floor_db
            and vad_max < tuning.integrity_vad_dead_max_prob_ceiling
            and spectrum_degraded
        )
        if apo_signature:
            return IntegrityVerdict.APO_DEGRADED

        # RMS present but user is genuinely not speaking (noise floor
        # band with VAD quiet). Benign; re-probe later.
        return IntegrityVerdict.VAD_MUTE

    def _inconclusive(
        self,
        endpoint_guid: str,
        duration_s: float,
        *,
        raw_frames: int = 0,
        detail: str = "",
    ) -> IntegrityResult:
        return IntegrityResult(
            verdict=IntegrityVerdict.INCONCLUSIVE,
            endpoint_guid=endpoint_guid,
            rms_db=_RMS_FLOOR_DB,
            vad_max_prob=0.0,
            spectral_flatness=0.0,
            spectral_rolloff_hz=0.0,
            duration_s=duration_s,
            probed_at_utc=datetime.now(UTC),
            raw_frames=raw_frames,
            detail=detail,
        )


class CaptureIntegrityCoordinator:
    """Drive the apply → probe → advance / quarantine state machine.

    Owned by :mod:`sovyx.voice.factory`; the orchestrator delegates
    every sustained-deaf heartbeat to :meth:`handle_deaf_signal`. The
    coordinator is one-shot per session: once it reaches a terminal
    verdict (APPLIED_HEALTHY / quarantined), later calls short-circuit
    via ``is_resolved``.

    Args:
        probe: The integrity probe used for before + after classification.
        strategies: Ordered list of platform-specific strategies.
            Strategies whose ``probe_eligibility`` returns
            ``applicable=False`` are skipped without counting toward
            the attempt budget.
        capture_task: The live capture task the coordinator operates
            on. Captured at construction so :meth:`handle_deaf_signal`
            can reconstruct a fresh :class:`BypassContext` per attempt.
        platform_key: Normalised ``sys.platform`` bucket. Pre-resolved
            by the factory so tests can pin it via constructor.
        tuning: Frozen tuning snapshot — re-read per call so live env
            overrides take effect without a restart.
        quarantine: Optional override for the endpoint quarantine.
            Defaults to the module-global quarantine shared with the
            KERNEL_INVALIDATED fail-over path.
    """

    def __init__(
        self,
        *,
        probe: CaptureIntegrityProbe,
        strategies: Sequence[PlatformBypassStrategy],
        capture_task: CaptureTaskProto,
        platform_key: str,
        tuning: VoiceTuningConfig | None = None,
        quarantine: EndpointQuarantine | None = None,
    ) -> None:
        self._probe = probe
        self._strategies = tuple(strategies)
        self._capture_task = capture_task
        self._platform_key = platform_key
        self._tuning = tuning
        self._quarantine = quarantine if quarantine is not None else get_default_quarantine()
        self._is_resolved = False
        self._lock = asyncio.Lock()

    @property
    def is_resolved(self) -> bool:
        """``True`` once a terminal outcome has been reached this session."""
        return self._is_resolved

    async def handle_deaf_signal(self) -> list[BypassOutcome]:
        """Run the bypass state machine once and return the outcome log.

        Idempotent within a session: a second call after resolution is
        a no-op returning an empty list. The method serialises via an
        internal lock so overlapping deaf heartbeats from the
        orchestrator collapse into a single iteration.
        """
        async with self._lock:
            if self._is_resolved:
                return []

            tuning = self._tuning if self._tuning is not None else _VoiceTuning()
            context = self._build_context()

            # Short-circuit — if the probe already classifies HEALTHY,
            # the deaf heartbeat was a false alarm.
            before = await self._probe.probe_warm(self._capture_task)
            record_capture_integrity_verdict(
                verdict=before.verdict.value,
                phase="pre_bypass",
            )
            if before.verdict is IntegrityVerdict.HEALTHY:
                logger.info(
                    "capture_integrity_coordinator_false_alarm",
                    endpoint_guid=context.endpoint_guid,
                )
                self._is_resolved = True
                return []

            outcomes: list[BypassOutcome] = []
            max_attempts = max(1, int(tuning.bypass_strategy_max_attempts))
            settle_s = max(0.0, float(tuning.bypass_strategy_post_apply_settle_s))
            attempts_counted = 0

            for idx, strategy in enumerate(self._strategies):
                if attempts_counted >= max_attempts:
                    logger.warning(
                        "capture_integrity_coordinator_max_attempts_reached",
                        endpoint_guid=context.endpoint_guid,
                        max_attempts=max_attempts,
                    )
                    break

                eligibility = await strategy.probe_eligibility(context)
                if not eligibility.applicable:
                    outcomes.append(
                        BypassOutcome(
                            strategy_name=strategy.name,
                            attempt_index=idx,
                            verdict=BypassVerdict.NOT_APPLICABLE,
                            integrity_before=before,
                            integrity_after=None,
                            elapsed_ms=0.0,
                            detail=eligibility.reason,
                        ),
                    )
                    record_bypass_strategy_verdict(
                        strategy=strategy.name,
                        verdict=BypassVerdict.NOT_APPLICABLE.value,
                        reason=eligibility.reason,
                    )
                    continue

                attempts_counted += 1
                t0 = time.monotonic()
                try:
                    apply_tag = await strategy.apply(context)
                except BypassApplyError as exc:
                    elapsed_ms = (time.monotonic() - t0) * 1000.0
                    outcomes.append(
                        BypassOutcome(
                            strategy_name=strategy.name,
                            attempt_index=idx,
                            verdict=BypassVerdict.FAILED_TO_APPLY,
                            integrity_before=before,
                            integrity_after=None,
                            elapsed_ms=elapsed_ms,
                            detail=f"{exc.reason}: {exc}",
                        ),
                    )
                    record_bypass_strategy_verdict(
                        strategy=strategy.name,
                        verdict=BypassVerdict.FAILED_TO_APPLY.value,
                        reason=exc.reason,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 — unclassified strategy error
                    elapsed_ms = (time.monotonic() - t0) * 1000.0
                    logger.exception(
                        "capture_integrity_coordinator_strategy_crashed",
                        strategy=strategy.name,
                        endpoint_guid=context.endpoint_guid,
                    )
                    outcomes.append(
                        BypassOutcome(
                            strategy_name=strategy.name,
                            attempt_index=idx,
                            verdict=BypassVerdict.FAILED_TO_APPLY,
                            integrity_before=before,
                            integrity_after=None,
                            elapsed_ms=elapsed_ms,
                            detail=f"strategy_crashed: {exc}",
                        ),
                    )
                    record_bypass_strategy_verdict(
                        strategy=strategy.name,
                        verdict=BypassVerdict.FAILED_TO_APPLY.value,
                        reason="strategy_crashed",
                    )
                    continue

                # Give the driver a moment to settle — Windows APO
                # teardown on an exclusive-mode switch takes ~200 ms.
                if settle_s > 0:
                    await asyncio.sleep(settle_s)
                after = await self._probe.probe_warm(self._capture_task)
                record_capture_integrity_verdict(
                    verdict=after.verdict.value,
                    phase="post_bypass",
                )
                elapsed_ms = (time.monotonic() - t0) * 1000.0

                if after.verdict is IntegrityVerdict.HEALTHY:
                    outcomes.append(
                        BypassOutcome(
                            strategy_name=strategy.name,
                            attempt_index=idx,
                            verdict=BypassVerdict.APPLIED_HEALTHY,
                            integrity_before=before,
                            integrity_after=after,
                            elapsed_ms=elapsed_ms,
                            detail=apply_tag,
                        ),
                    )
                    record_bypass_strategy_verdict(
                        strategy=strategy.name,
                        verdict=BypassVerdict.APPLIED_HEALTHY.value,
                        reason=apply_tag,
                    )
                    logger.info(
                        "capture_integrity_coordinator_resolved",
                        strategy=strategy.name,
                        endpoint_guid=context.endpoint_guid,
                        elapsed_ms=round(elapsed_ms, 1),
                    )
                    self._is_resolved = True
                    return outcomes

                outcomes.append(
                    BypassOutcome(
                        strategy_name=strategy.name,
                        attempt_index=idx,
                        verdict=BypassVerdict.APPLIED_STILL_DEAD,
                        integrity_before=before,
                        integrity_after=after,
                        elapsed_ms=elapsed_ms,
                        detail=f"post_probe_verdict={after.verdict.value}",
                    ),
                )
                record_bypass_strategy_verdict(
                    strategy=strategy.name,
                    verdict=BypassVerdict.APPLIED_STILL_DEAD.value,
                    reason=after.verdict.value,
                )
                # Revert so the next strategy starts from the pre-apply
                # state rather than an opaque mix of A and B.
                try:
                    await strategy.revert(context)
                except Exception:  # noqa: BLE001 — revert is best-effort
                    logger.exception(
                        "capture_integrity_coordinator_revert_crashed",
                        strategy=strategy.name,
                        endpoint_guid=context.endpoint_guid,
                    )

            # Strategy list exhausted without a HEALTHY outcome —
            # quarantine the endpoint so the factory fails over.
            self._quarantine_endpoint(before, tuning)
            self._is_resolved = True
            return outcomes

    # -- internals ------------------------------------------------------

    def _build_context(self) -> BypassContext:
        return BypassContext(
            endpoint_guid=self._capture_task.active_device_guid or "",
            endpoint_friendly_name=self._capture_task.active_device_name,
            host_api_name=self._capture_task.host_api_name or "",
            platform_key=self._platform_key,
            capture_task=self._capture_task,
            probe_fn=lambda: self._probe.probe_warm(self._capture_task),
            current_device_index=self._capture_task.active_device_index,
            current_device_kind=self._capture_task.active_device_kind,
        )

    def _quarantine_endpoint(
        self,
        last_probe: IntegrityResult,
        tuning: VoiceTuningConfig,
    ) -> None:
        if not tuning.apo_quarantine_enabled:
            logger.warning(
                "capture_integrity_coordinator_quarantine_disabled",
                endpoint_guid=last_probe.endpoint_guid,
            )
            return
        if not last_probe.endpoint_guid:
            # Can't key a quarantine without a GUID — fail loudly in
            # observability rather than silently ignore the event.
            logger.error(
                "capture_integrity_coordinator_quarantine_missing_guid",
                device_name=self._capture_task.active_device_name,
            )
            return
        # The global quarantine's TTL is shared with KERNEL_INVALIDATED
        # (`kernel_invalidated_quarantine_s`). The APO-specific knob
        # :attr:`apo_quarantine_s` is consulted by the watchdog's
        # APO-recheck loop — the coordinator just tags the entry with
        # the ``"apo_degraded"`` reason so the watchdog can distinguish
        # APO entries from kernel-invalidated entries.
        self._quarantine.add(
            endpoint_guid=last_probe.endpoint_guid,
            device_friendly_name=self._capture_task.active_device_name,
            host_api=self._capture_task.host_api_name or "",
            reason="apo_degraded",
        )
        record_apo_degraded_event(
            platform=self._platform_key,
            action="quarantine",
        )
        logger.warning(
            "capture_integrity_coordinator_quarantined",
            endpoint_guid=last_probe.endpoint_guid,
            reason="apo_degraded",
        )


__all__ = [
    "CaptureIntegrityCoordinator",
    "CaptureIntegrityProbe",
]
