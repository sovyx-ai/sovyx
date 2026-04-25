"""Layer 5 — Stack-wide pre-flight (ADR §4.5).

Before declaring the daemon ready, run the eight steps from ADR §4.5
sequentially. Each step reports :class:`PreflightStep` with a pass /
fail flag, an identity code (:class:`PreflightStepCode`), a human-
readable hint, elapsed time, and an optional details mapping. The
orchestrator emits :class:`PreflightReport` with the full sequence,
whether the whole run passed, the first failure (if any), and the
total wall-clock duration.

Design intent
-------------
This module owns *only the orchestration and contract*. The actual
check implementations come from the caller. That keeps the preflight
testable without mocking PortAudio / LLM / OS APIs, and lets each
caller (bootstrap, CLI doctor, setup wizard) inject exactly the
checks that make sense for its invocation context.

Two default factory helpers are provided for checks the voice
subpackage *does* own:

* :func:`check_portaudio` — succeeds when ``sounddevice`` enumerates
  at least one input-capable host API.
* :func:`check_wake_word_smoke` — runs one second of silence through
  a :class:`WakeWordDetector` and asserts no spurious detection.

Mute, microphone permission, LLM reachability, TTS open, and the L2
cold cascade are caller-owned because each wants specific state
(MMDevice client, TCC status, LLM router, TTS engine instance,
``combo_store`` + endpoint GUID) that preflight has no business
constructing.

Short-circuit policy
--------------------
By default ``run_preflight`` stops at the first failure — later steps
either depend on the earlier one (step 5 needs PortAudio from step 4)
or their failure would be misleading without the cause being visible.
Callers that want a full report (doctor CLI with ``--json`` for
characterization) pass ``stop_on_first_failure=False``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import record_preflight_failure

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sovyx.voice.wake_word import WakeWordDetector

logger = get_logger(__name__)


class PreflightStepCode(StrEnum):
    """Machine-readable identity for each preflight step (ADR §4.5)."""

    MIC_MUTED = "mic_muted"
    """Step 1. Default mic reports the OS mute flag as on."""

    MIC_PERMISSION_DENIED = "mic_permission_denied"
    """Step 2. Windows MicrophoneAccess / macOS TCC / Linux n/a denied."""

    MODELS_CORRUPT = "models_corrupt"
    """Step 3. Silero / Moonshine / Piper / Kokoro ONNX model missing or SHA mismatch."""

    PORTAUDIO_UNAVAILABLE = "portaudio_unavailable"
    """Step 4. ``sounddevice`` fails to enumerate host APIs (``audiosrv`` down)."""

    CAPTURE_UNHEALTHY = "capture_unhealthy"
    """Step 5. L2 cold cascade did not return HEALTHY within the budget."""

    TTS_UNAVAILABLE = "tts_unavailable"
    """Step 6. TTS engine cannot synthesize the 200 ms test buffer, or
    output device refuses to open."""

    LLM_UNREACHABLE = "llm_unreachable"
    """Step 7. No configured LLM provider answered a 3 s HEAD ping."""

    WAKE_WORD_MISBEHAVING = "wake_word_misbehaving"
    """Step 8. Silence produced a wake-word score above the threshold —
    the model is broken, miscalibrated, or the wrong file was loaded."""

    LINUX_MIXER_SATURATED = "linux_mixer_saturated"
    """Step 9 (Linux only). One or more ALSA mixer gain controls
    (typically ``Internal Mic Boost`` + ``Capture``) sits near max,
    summing to a pre-ADC gain that clips the ADC on every peak of
    speech. The matching remediation is
    :class:`sovyx.voice.health.bypass.LinuxALSAMixerResetBypass`; the
    dashboard surfaces a ``LinuxMicGainCard`` explainer with a reset
    button."""


@dataclass(frozen=True, slots=True)
class PreflightStep:
    """Outcome of one preflight step."""

    step: int
    """1-based step number per ADR §4.5."""

    name: str
    """Human-readable step name (shown by doctor CLI / wizard UI)."""

    code: PreflightStepCode
    """Machine-readable step identity. Stable across releases."""

    passed: bool
    """``True`` when the step succeeded. ``False`` means the daemon
    should refuse to start (unless ``--allow-degraded`` is set)."""

    hint: str
    """Actionable message surfaced to the user on failure. Empty on
    pass unless the step recorded a warning (e.g. "mic input level
    is marginal") that the caller wants to relay."""

    duration_ms: float
    """Wall-clock milliseconds the step took. Emitted on the
    ``preflight_step_completed`` log line and queryable by the
    dashboard to highlight slow steps."""

    details: Mapping[str, Any] = field(default_factory=dict)
    """Step-specific extra context (winning combo, detected APOs,
    reachable providers, etc.). The orchestrator never inspects this."""

    skipped_due_to: tuple[PreflightStepCode, ...] = ()
    """Step codes whose failure caused this step to be skipped (H1).

    Empty tuple means the step's ``check`` actually executed and
    produced ``passed`` / ``hint`` / ``details`` directly. A non-empty
    tuple means the step was preemptively marked failed because at
    least one of its declared :attr:`PreflightStepSpec.depends_on`
    codes was in the failed set when execution reached it. The
    orchestrator skips the ``check`` callable entirely (preserving
    its preconditions — the whole reason dependencies exist), sets
    ``passed=False``, ``duration_ms=0.0``, and populates this field
    so the dashboard can distinguish a real failure from a cascaded
    one. Operators reading the report see a clear ``skipped because
    step X failed`` lineage instead of confusing repeat-failure
    messages from a check that depends on a broken precondition.
    """


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Aggregate result of a preflight run."""

    steps: tuple[PreflightStep, ...]
    """All steps actually executed, in order. When
    ``stop_on_first_failure=True`` and step N failed, steps > N are
    absent; when ``False``, every step from the input list appears."""

    passed: bool
    """``True`` iff every executed step passed."""

    first_failure: PreflightStep | None
    """The first failed step, if any. Populated even when later steps
    were run with ``stop_on_first_failure=False``."""

    total_duration_ms: float
    """Wall-clock duration of the full orchestrator call."""


class PreflightCheck(Protocol):
    """Async callable returning ``(passed, hint, details)``.

    * ``passed``: whether the check succeeded.
    * ``hint``: actionable message surfaced on failure. Pass an empty
      string on success, or a non-empty warning that the caller may
      relay.
    * ``details``: optional extra context. Use an empty ``dict`` when
      nothing needs to be surfaced.
    """

    async def __call__(self) -> tuple[bool, str, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class PreflightStepSpec:
    """Declaration of one step the orchestrator should run.

    Attributes:
        step: 1-based ordinal — also drives execution order.
        name: Human-readable name surfaced in logs / wizard UI.
        code: Stable machine-readable identity.
        check: Async callable returning ``(passed, hint, details)``.
        depends_on: Step codes this step structurally depends on (H1).
            When :func:`run_preflight` runs with
            ``stop_on_first_failure=False`` (the doctor CLI / dashboard
            path that wants the full report) and any code in this tuple
            failed earlier in the run, the orchestrator skips this
            step's ``check`` and emits a ``passed=False`` outcome with
            :attr:`PreflightStep.skipped_due_to` populated. Empty by
            default — pre-H1 specs continue to work unchanged. Use this
            to express real preconditions (e.g. step 5 capture-cascade
            cannot run if step 4 PortAudio enumeration is broken — a
            cascade attempt would just produce a confusing duplicate
            failure with the same root cause).
    """

    step: int
    name: str
    code: PreflightStepCode
    check: PreflightCheck
    depends_on: tuple[PreflightStepCode, ...] = ()


async def run_preflight(
    *,
    steps: Sequence[PreflightStepSpec],
    stop_on_first_failure: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> PreflightReport:
    """Execute ``steps`` sequentially and return the aggregate report.

    Each check is wrapped so that exceptions become a failed step with
    ``hint`` set to the string form of the exception. A check that
    raises MUST NOT kill the orchestrator — preflight is a diagnostic
    surface; it is more valuable to emit "step N crashed: X" than to
    bail silently.

    Args:
        steps: Ordered list of specs. The orchestrator validates no
            two specs share the same ``step`` number.
        stop_on_first_failure: When ``True`` (default, matches
            ``daemon ready`` semantics), the first failure short-
            circuits. When ``False`` (doctor CLI ``--json``), every
            step runs even after a failure.
        clock: Monotonic clock injection for deterministic tests.

    Returns:
        :class:`PreflightReport`.

    Raises:
        ValueError: If ``steps`` is empty or has a duplicate step
            number. Programmer errors surface here; the orchestrator
            never raises for runtime check failures.
    """
    if not steps:
        msg = "run_preflight requires at least one step"
        raise ValueError(msg)
    seen: set[int] = set()
    for spec in steps:
        if spec.step in seen:
            msg = f"duplicate preflight step number: {spec.step}"
            raise ValueError(msg)
        seen.add(spec.step)

    run_start = clock()
    results: list[PreflightStep] = []
    first_failure: PreflightStep | None = None
    failed_codes: set[PreflightStepCode] = set()

    for spec in steps:
        # H1 dependency gate: skip the check entirely when a declared
        # precondition has already failed in this run. Without this,
        # downstream steps run on a broken precondition and produce
        # confusing duplicate failures with the same root cause (e.g.
        # ``capture cascade unhealthy`` after ``PortAudio unavailable``
        # — the cascade was always going to fail; logging it pretends
        # the user has two problems to fix when they have one).
        unmet_deps = tuple(dep for dep in spec.depends_on if dep in failed_codes)
        if unmet_deps:
            outcome = PreflightStep(
                step=spec.step,
                name=spec.name,
                code=spec.code,
                passed=False,
                hint=(
                    f"skipped — depends on "
                    f"{', '.join(d.value for d in unmet_deps)} which failed earlier"
                ),
                duration_ms=0.0,
                details={"skipped_due_to": [d.value for d in unmet_deps]},
                skipped_due_to=unmet_deps,
            )
            results.append(outcome)
            failed_codes.add(outcome.code)
            logger.info(
                "voice.preflight.step_skipped_dependency",
                **{
                    "voice.step": outcome.step,
                    "voice.name": outcome.name,
                    "voice.code": outcome.code.value,
                    "voice.skipped_due_to": [d.value for d in unmet_deps],
                },
            )
            record_preflight_failure(step=outcome.name, code=outcome.code.value)
            if first_failure is None:
                first_failure = outcome
            # Skipped steps don't honour stop_on_first_failure: by
            # construction they didn't *run*, so there's nothing to halt.
            # They DO populate first_failure for the report shape, but
            # the loop continues so subsequent independent steps still
            # produce diagnostics for the operator.
            continue

        step_start = clock()
        try:
            passed, hint, details = await spec.check()
        except Exception as exc:  # noqa: BLE001 — diagnostics must not crash preflight
            logger.warning(
                "voice_preflight_step_crashed",
                step=spec.step,
                name=spec.name,
                code=spec.code.value,
                error=str(exc),
                exc_info=True,
            )
            passed = False
            hint = f"internal error: {exc}"
            details = {"exception_type": type(exc).__name__}
        step_duration = (clock() - step_start) * 1000.0

        outcome = PreflightStep(
            step=spec.step,
            name=spec.name,
            code=spec.code,
            passed=passed,
            hint=hint,
            duration_ms=step_duration,
            details=dict(details),
        )
        results.append(outcome)
        logger.info(
            "voice_preflight_step_completed",
            step=outcome.step,
            name=outcome.name,
            code=outcome.code.value,
            passed=outcome.passed,
            duration_ms=round(outcome.duration_ms, 1),
        )
        if not outcome.passed:
            failed_codes.add(outcome.code)
            record_preflight_failure(step=outcome.name, code=outcome.code.value)
            if first_failure is None:
                first_failure = outcome
                logger.error(
                    "voice_preflight_failed",
                    step=outcome.step,
                    code=outcome.code.value,
                    hint=outcome.hint,
                )
            if stop_on_first_failure:
                break

    total_duration_ms = (clock() - run_start) * 1000.0
    passed = first_failure is None
    report = PreflightReport(
        steps=tuple(results),
        passed=passed,
        first_failure=first_failure,
        total_duration_ms=total_duration_ms,
    )
    logger.info(
        "voice_preflight_completed",
        passed=report.passed,
        steps_run=len(report.steps),
        first_failure_code=(first_failure.code.value if first_failure is not None else None),
        total_duration_ms=round(total_duration_ms, 1),
    )
    return report


# ---------------------------------------------------------------------------
# Default check factories for steps the voice subpackage owns.
# ---------------------------------------------------------------------------


def check_portaudio(*, sd_module: Any | None = None) -> PreflightCheck:  # noqa: ANN401 — sounddevice is not typed
    """Step 4 default — ``sounddevice`` initialization + host API sanity.

    Succeeds when at least one host API is enumerable and at least one
    input-capable device is visible. Failure means ``audiosrv``
    (Windows) / PulseAudio (Linux) / coreaudiod (macOS) is down or
    PortAudio is unable to initialize — the cascade and every
    downstream capture step will fail the same way, so we short-
    circuit here with a clear message.

    Args:
        sd_module: Optional ``sounddevice`` module for tests. Production
            callers pass ``None`` and the module is imported lazily.

    Returns:
        A :class:`PreflightCheck` closure.
    """

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        sd: Any = sd_module
        if sd is None:
            import sounddevice as _sd  # noqa: PLC0415 — lazy so tests can skip

            sd = _sd
        try:
            host_apis = sd.query_hostapis()
            devices = sd.query_devices()
        except Exception as exc:  # noqa: BLE001 — PortAudio surfaces a zoo of error types
            return (
                False,
                "Audio service appears to be down. Try restarting it "
                "(Windows: restart 'Windows Audio' service; Linux: "
                "`systemctl --user restart pulseaudio`; "
                "macOS: `sudo killall coreaudiod`).",
                {"error": str(exc), "exception_type": type(exc).__name__},
            )

        def _input_channels(device: Any) -> int:  # noqa: ANN401 — sounddevice device entries are untyped
            raw = getattr(device, "max_input_channels", None)
            if raw is None and hasattr(device, "get"):
                raw = device.get("max_input_channels", 0)
            try:
                return int(raw or 0)
            except (TypeError, ValueError):
                return 0

        input_capable = [d for d in devices if _input_channels(d) > 0]
        if not host_apis or not input_capable:
            return (
                False,
                "PortAudio reports no input-capable devices. Check "
                "that a microphone is connected and the audio service "
                "is running.",
                {
                    "host_api_count": len(host_apis) if host_apis else 0,
                    "input_device_count": len(input_capable),
                },
            )
        return (
            True,
            "",
            {
                "host_api_count": len(host_apis),
                "input_device_count": len(input_capable),
            },
        )

    return _check


def check_wake_word_smoke(
    *,
    detector: WakeWordDetector,
    max_score: float = 0.3,
    duration_ms: float = 1000.0,
    frame_samples: int = 512,
    sample_rate: int = 16_000,
) -> PreflightCheck:
    """Step 8 default — wake-word silence sanity.

    Runs ``duration_ms`` of int16 zeros through the detector and
    asserts every frame score stays below ``max_score``. A score
    above the threshold on silence means the model is broken,
    miscalibrated for the wrong sample rate, or the weight file
    loaded was not the one we expected.

    The default ``max_score=0.3`` matches ADR §4.5 step 8. Use a
    tighter value if you know the model is well-behaved and want
    to guard against subtle regressions.

    Args:
        detector: A started :class:`WakeWordDetector`.
        max_score: Upper bound on per-frame score. Any frame exceeding
            this flips the check to failed.
        duration_ms: How much silence to feed.
        frame_samples: Detector frame size. Matches the pipeline's
            512-sample window at 16 kHz.
        sample_rate: Sample rate (for bookkeeping only; the detector
            is configured independently).

    Returns:
        A :class:`PreflightCheck` closure.
    """

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        total_frames = max(1, int((duration_ms / 1000.0) * sample_rate / frame_samples))
        max_observed = 0.0
        for _ in range(total_frames):
            frame_f32 = np.zeros(frame_samples, dtype=np.float32)
            event = detector.process_frame(frame_f32)
            if event.score > max_observed:
                max_observed = event.score
        if max_observed > max_score:
            return (
                False,
                f"Wake-word model produced score {max_observed:.2f} on "
                f"silence (max allowed {max_score:.2f}). The model may "
                "be miscalibrated or the wrong file was downloaded.",
                {
                    "max_observed_score": max_observed,
                    "max_allowed_score": max_score,
                    "frames_tested": total_frames,
                },
            )
        return (
            True,
            "",
            {
                "max_observed_score": max_observed,
                "frames_tested": total_frames,
            },
        )

    return _check


def check_tts_synthesize(
    *,
    tts: Any,  # noqa: ANN401 — accepts any engine exposing async synthesize(text)
    phrase: str = "ok",
    min_samples: int = 100,
) -> PreflightCheck:
    """Step 6 default — TTS engine can synthesize a short test buffer.

    Calls ``tts.synthesize(phrase)`` and asserts the returned chunk
    contains at least ``min_samples`` audio samples. The check does
    *not* push audio through the output device — opening the device
    is the AudioOutputQueue's responsibility and happens at first
    ``speak()`` call. We only verify the synthesis path because a
    broken Piper/Kokoro model corrupts every utterance while the
    output device would fail the same way for any application.

    Args:
        tts: A started TTS engine with an async ``synthesize(text)``
            method returning an ``AudioChunk``-compatible object.
        phrase: Short text to synthesize. Kept tiny by default so
            preflight doesn't burn tokens on cloud TTS.
        min_samples: Minimum sample count in the returned chunk.

    Returns:
        A :class:`PreflightCheck` closure.
    """

    async def _check() -> tuple[bool, str, dict[str, Any]]:
        try:
            chunk = await tts.synthesize(phrase)
        except Exception as exc:  # noqa: BLE001 — TTS surfaces model / ONNX / cloud errors
            return (
                False,
                "Text-to-speech engine failed to synthesize a test "
                "phrase. Check that the TTS model is installed and "
                "your output device is available.",
                {"error": str(exc), "exception_type": type(exc).__name__},
            )
        sample_count = int(getattr(chunk, "audio", np.zeros(0)).size)
        if sample_count < min_samples:
            return (
                False,
                f"TTS returned only {sample_count} samples "
                f"(minimum {min_samples}). The model may be corrupted.",
                {"sample_count": sample_count, "min_samples": min_samples},
            )
        return (
            True,
            "",
            {
                "sample_count": sample_count,
                "sample_rate": int(getattr(chunk, "sample_rate", 0)),
            },
        )

    return _check


# ---------------------------------------------------------------------------
# Bootstrap helper — assemble the canonical 8-step spec list.
# ---------------------------------------------------------------------------


def default_step_names() -> Mapping[int, tuple[str, PreflightStepCode]]:
    """Return the canonical (name, code) for each of the 9 ADR steps.

    Callers that want to run a subset (e.g. CLI doctor with the four
    checks the voice subpackage owns) use this mapping to fill in
    the ``name`` / ``code`` fields without hard-coding strings. Step 9
    (``linux_mixer_saturated``) is Linux-only; on non-Linux hosts the
    check short-circuits to a ``skipped=True`` pass.
    """
    return {
        1: ("OS mic mute flag", PreflightStepCode.MIC_MUTED),
        2: ("Mic permission", PreflightStepCode.MIC_PERMISSION_DENIED),
        3: ("Voice models integrity", PreflightStepCode.MODELS_CORRUPT),
        4: ("PortAudio host APIs", PreflightStepCode.PORTAUDIO_UNAVAILABLE),
        5: ("Capture cold cascade", PreflightStepCode.CAPTURE_UNHEALTHY),
        6: ("TTS synthesis", PreflightStepCode.TTS_UNAVAILABLE),
        7: ("LLM provider reachable", PreflightStepCode.LLM_UNREACHABLE),
        8: ("Wake-word silence sanity", PreflightStepCode.WAKE_WORD_MISBEHAVING),
        9: ("Linux ALSA mixer sanity", PreflightStepCode.LINUX_MIXER_SATURATED),
    }


def current_platform_key() -> str:
    """Return the ``sys.platform`` prefix the cascade uses."""
    return sys.platform


# ---------------------------------------------------------------------------
# v1.3 §4.6 L6 — Boot preflight warnings registry-backed store.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BootPreflightWarningsStore:
    """In-memory holder for boot preflight warnings, resolvable via ServiceRegistry.

    Populated by :func:`sovyx.voice.factory.create_voice_pipeline`
    (wired via ``dashboard.routes.voice.enable_voice``) when the step 9
    mixer sanity check fails at boot. Consumed by
    :func:`sovyx.dashboard.voice_status.get_voice_status` so
    ``GET /api/voice/status`` can surface the warning list — keyed via
    :meth:`sovyx.engine.registry.ServiceRegistry.resolve` for
    consistency with every other voice service (VoicePipeline,
    STTEngine, TTSEngine, …).

    The store is mutable on purpose: disable + re-enable creates a
    fresh instance (see ``disable_voice`` deregister path), while a
    subsequent enable on the same registry replaces the warnings
    snapshot via :meth:`set_warnings` rather than appending — so
    repeated toggles never accumulate stale state.

    L7 (:func:`write_preflight_warnings_file`) persists the same data
    to ``~/.sovyx/preflight_warnings.json`` so CLI-first users see the
    warning on ``sovyx start`` / ``sovyx status`` even without opening
    the dashboard. The two channels are written in parallel by the
    factory; neither depends on the other for correctness.
    """

    warnings: list[dict[str, object]] = field(default_factory=list)

    def set_warnings(self, warnings: list[dict[str, object]]) -> None:
        """Replace the snapshot with a defensive copy of ``warnings``."""
        self.warnings = list(warnings)

    def clear(self) -> None:
        """Drop every stored warning. Called on pipeline disable."""
        self.warnings = []

    def snapshot(self) -> list[dict[str, object]]:
        """Return a defensive copy for caller read-only consumption.

        Callers (``get_voice_status``) serialise this into JSON; a
        defensive copy guarantees a later ``set_warnings`` / ``clear``
        cannot mutate the response mid-flight.
        """
        return list(self.warnings)


# ---------------------------------------------------------------------------
# v1.3 §4.8 L7 — Boot preflight warnings marker file.
# ---------------------------------------------------------------------------


_PREFLIGHT_WARNINGS_FILENAME = "preflight_warnings.json"
_PREFLIGHT_WARNINGS_SCHEMA_VERSION = 1


def preflight_warnings_file_path(data_dir: Path | None = None) -> Path:
    """Return the absolute path to the boot preflight warnings marker.

    The file lives under the user's Sovyx data directory so it is
    user-scoped (multi-user hosts cannot cross-read) and mirrors every
    other persistent artefact Sovyx writes. Callers that need a
    test-scoped directory pass ``data_dir`` explicitly; production
    callers pass ``None`` and the path resolves to ``~/.sovyx/``.

    Args:
        data_dir: Optional override for the base directory. Tests pass
            ``tmp_path / ".sovyx"``; production omits the argument.

    Returns:
        Absolute :class:`~pathlib.Path` to the marker file (may or may
        not exist — callers use :func:`read_preflight_warnings_file`
        to observe contents).
    """
    base = data_dir if data_dir is not None else (Path.home() / ".sovyx")
    return base / _PREFLIGHT_WARNINGS_FILENAME


def write_preflight_warnings_file(
    warnings: list[dict[str, object]],
    *,
    data_dir: Path | None = None,
) -> None:
    """Atomically persist boot preflight warnings to the marker file.

    Writes to a sibling ``.tmp`` file and renames onto the final path
    so a mid-write crash never leaves a partially-written marker for
    the next CLI invocation to read. Callers typically wrap the call
    in :func:`contextlib.suppress` with :class:`OSError` because
    filesystem failures on boot must not block the voice pipeline from
    starting — the in-memory store (L6) remains authoritative.

    Args:
        warnings: List of warning dicts (``code``, ``hint``, optional
            ``details``) — same shape the dashboard store snapshots.
        data_dir: Optional override; see
            :func:`preflight_warnings_file_path`.
    """
    path = preflight_warnings_file_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _PREFLIGHT_WARNINGS_SCHEMA_VERSION,
        "written_at_utc": datetime.now(UTC).isoformat(),
        "warnings": warnings,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_preflight_warnings_file(
    *,
    data_dir: Path | None = None,
) -> list[dict[str, object]]:
    """Read the marker file, returning ``[]`` on missing / malformed input.

    CLI consumers (``sovyx start`` / ``sovyx status`` /
    ``sovyx doctor voice``) must render something sane on every
    filesystem state — the user's boot sequence cannot hinge on a
    marker being parseable. Unknown schema versions degrade to empty
    rather than fail so an older CLI reading a newer file does not
    crash.
    """
    path = preflight_warnings_file_path(data_dir)
    if not path.exists():
        return []
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(payload, dict):
        return []
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list):
        return []
    # Keep only dict entries — defensive against a hand-edited file
    # that swapped the list shape.
    return [w for w in warnings if isinstance(w, dict)]


def clear_preflight_warnings_file(*, data_dir: Path | None = None) -> None:
    """Remove the marker file if present. Idempotent on missing files.

    Called by:

    * :func:`sovyx.voice.factory.create_voice_pipeline` when a boot
      preflight pass follows a prior saturated boot — removes the
      stale marker so the next ``sovyx start`` does not surface a
      ghost warning (v1.3 §-1C #1 alternative (e)).
    * :func:`sovyx.cli.commands.doctor._run_voice_doctor` after
      ``--fix`` successfully remediates the mixer — the user acted on
      the warning and the marker no longer reflects runtime state.
    """
    path = preflight_warnings_file_path(data_dir)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


__all__ = [
    "BootPreflightWarningsStore",
    "PreflightCheck",
    "PreflightReport",
    "PreflightStep",
    "PreflightStepCode",
    "PreflightStepSpec",
    "check_portaudio",
    "check_tts_synthesize",
    "check_wake_word_smoke",
    "clear_preflight_warnings_file",
    "current_platform_key",
    "default_step_names",
    "preflight_warnings_file_path",
    "read_preflight_warnings_file",
    "run_preflight",
    "write_preflight_warnings_file",
]
