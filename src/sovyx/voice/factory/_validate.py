"""Factory validation gates + boot cascade + STT/wake-word constructors.

Split from the legacy ``factory.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T03.

Owns the factory-side validation surface:

* :class:`VoiceFactoryError` / :class:`VoicePermissionError` — public
  exception types exported via the package ``__init__``.
* Preflight gate helpers (:func:`_maybe_check_mic_permission`,
  :func:`_maybe_check_llm_reachable`, :func:`_run_boot_preflight`).
* The VCHL boot cascade orchestration
  (:func:`_run_vchl_boot_cascade` + :func:`_detect_voice_clarity_active`).
* Sync component constructors (:func:`_create_stt`,
  :func:`_create_wake_word_stub`).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.device_enum import DeviceEntry


logger = get_logger(__name__)


__all__ = [
    "VoiceFactoryError",
    "VoicePermissionError",
    "_create_stt",
    "_create_wake_word_stub",
    "_detect_voice_clarity_active",
    "_maybe_check_llm_reachable",
    "_maybe_check_mic_permission",
    "_run_boot_preflight",
    "_run_vchl_boot_cascade",
]


class VoiceFactoryError(Exception):
    """Raised when voice pipeline components can't be created."""

    def __init__(self, message: str, missing_models: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.missing_models = missing_models or []


class VoicePermissionError(VoiceFactoryError):
    """Raised when the OS denies Sovyx microphone access (band-aid
    #34 wire-up).

    Subclasses :class:`VoiceFactoryError` so callers catching the
    base class still handle it; specific catches can use this class
    to surface the OS-specific remediation hint to the dashboard
    without scraping the message string.

    Attributes:
        remediation_hint: Operator-actionable message ready to render
            verbatim in the dashboard error banner. Naming the exact
            Settings path (Win 10/11) or System Preferences pane (macOS)
            so the operator doesn't have to hunt.
        platform_status: Raw verdict from
            :func:`~sovyx.voice.health._mic_permission.check_microphone_permission`
            (``"granted"`` / ``"denied"`` / ``"unknown"``).
    """

    def __init__(
        self,
        message: str,
        *,
        remediation_hint: str = "",
        platform_status: str = "",
    ) -> None:
        super().__init__(message)
        self.remediation_hint = remediation_hint
        self.platform_status = platform_status


# ── Preflight wire-ups (opt-in factory gates) ────────────────────


def _maybe_check_mic_permission() -> None:
    """Band-aid #34 wire-up: probe OS mic permission before pipeline
    creation. Opt-in via
    :attr:`VoiceTuningConfig.voice_check_mic_permission_enabled`.
    Default OFF — operators opt in once they've validated the gate
    on their hardware.

    Raises:
        VoicePermissionError: When the gate is enabled AND the probe
            returns DENIED. Carries the OS-specific remediation hint
            so the dashboard can surface it verbatim.

    Never raises on UNKNOWN — the OS-level probe couldn't decide,
    and the cascade's own deaf-detection covers the residual case.
    Never raises on non-Windows when the gate is enabled — Linux
    has no OS gate, macOS UNKNOWN is the deferred MA2 case.
    """
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_check_mic_permission_enabled:
        return

    try:
        from sovyx.voice.health._mic_permission import (
            MicPermissionStatus,
            check_microphone_permission,
        )

        report = check_microphone_permission()
    except Exception as exc:  # noqa: BLE001 — preflight gate isolation
        # The probe itself crashed (registry permission anomaly,
        # unexpected platform behaviour). Log structured WARN but
        # do NOT block pipeline creation — the gate is best-effort
        # observability, not a hard requirement.
        logger.warning(
            "voice.factory.mic_permission_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return

    if report.status is MicPermissionStatus.DENIED:
        msg = (
            "Microphone access is denied at the OS level — capture would "
            "produce all-zero frames forever. " + report.remediation_hint
        )
        logger.error(
            "voice.factory.mic_permission_denied",
            **{
                "voice.platform_status": report.status.value,
                "voice.machine_value": report.machine_value or "",
                "voice.user_value": report.user_value or "",
                "voice.action_required": report.remediation_hint,
            },
        )
        raise VoicePermissionError(
            msg,
            remediation_hint=report.remediation_hint,
            platform_status=report.status.value,
        )

    # GRANTED + UNKNOWN both proceed. UNKNOWN logs a structured note
    # so dashboards can show "couldn't determine permission state"
    # alongside subsequent capture telemetry.
    if report.status is MicPermissionStatus.UNKNOWN:
        logger.info(
            "voice.factory.mic_permission_unknown",
            **{"voice.notes": list(report.notes)},
        )


async def _maybe_check_llm_reachable(router: object | None = None) -> None:
    """Band-aid #28 wire-up: probe LLM router reachability before
    pipeline creation. Opt-in via
    :attr:`VoiceTuningConfig.voice_check_llm_reachable_enabled`.
    Default OFF.

    Args:
        router: Pre-resolved LLM router. ``None`` = skip the gate
            silently (the factory can't resolve the router itself
            because the registry isn't a global; callers that have
            access to the registry pass the resolved router).

    On FAIL: logs structured WARN but does NOT raise. Reasoning: the
    LLM might be a process that takes a few seconds to come up after
    Sovyx boots (Ollama in particular has a documented warm-up
    window). Blocking the entire voice pipeline on it would surface
    as "voice broken" rather than "LLM not yet ready" — worse UX
    than letting voice come up + degrading gracefully when the LLM
    is queried."""
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_check_llm_reachable_enabled:
        return
    if router is None:
        logger.info(
            "voice.factory.llm_check_skipped_no_router",
            reason="caller did not provide a router; gate skipped",
        )
        return

    try:
        from sovyx.voice.health.preflight import check_llm_reachable

        check = check_llm_reachable(router=router)
        passed, hint, details = await check()
    except Exception as exc:  # noqa: BLE001 — preflight gate isolation
        logger.warning(
            "voice.factory.llm_check_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return

    if not passed:
        # Log WARN — operators see the LLM is not ready but voice
        # still comes up. The next user utterance will hit the
        # router's own retry / failover path.
        logger.warning(
            "voice.factory.llm_unreachable_at_startup",
            **{
                "voice.action_required": hint,
                "voice.details": details,
            },
        )


async def _run_boot_preflight(
    *,
    tuning: VoiceTuningConfig,
) -> list[dict[str, object]]:
    """Run the boot-time subset of :mod:`voice.health.preflight` (v1.3 §4.6 L6).

    The factory runs step 9 (Linux ALSA mixer sanity) at every pipeline
    boot so a saturated codec default surfaces before the user ever
    hits "Enable voice" — the v0.21.2 incident established that users
    who already left the dashboard by the time the first deaf
    heartbeat fires never see the diagnostic. Non-Linux hosts pass
    cheaply (the check short-circuits via ``sys.platform``); Linux
    hosts without ``amixer`` also pass (missing tooling is logged but
    not escalated to a boot warning).

    The factory itself is platform-neutral — every step the cold
    cascade touches runs on every OS. We intentionally *do not* run
    the step 1/2/3/4/5/6/7/8 checks here because most of them require
    running subprocess / ONNX sessions that duplicate work the main
    factory is already doing in parallel. Step 9 is the surgical
    addition dossier ``SVX-VOICE-LINUX-20260422`` calls for, and no
    more.

    Returns a list of warning dicts — each includes ``code``,
    ``severity``, ``hint``, and a ``details`` mapping. The
    ``severity`` tag is always ``"warning"`` at this stage; if a
    future check surfaces a hard failure the factory will raise
    instead.
    """
    from sovyx.voice.health import (
        PreflightStepSpec,
        check_linux_mixer_sanity,
        default_step_names,
        run_preflight,
    )

    names = default_step_names()
    step9_name, step9_code = names[9]
    specs = [
        PreflightStepSpec(
            step=9,
            name=step9_name,
            code=step9_code,
            check=check_linux_mixer_sanity(tuning=tuning),
        ),
    ]
    report = await run_preflight(steps=specs, stop_on_first_failure=False)
    warnings: list[dict[str, object]] = []
    for step in report.steps:
        if step.passed:
            continue
        warnings.append(
            {
                "code": step.code.value,
                "severity": "warning",
                "hint": step.hint,
                "details": dict(step.details),
            },
        )
    return warnings


async def _run_vchl_boot_cascade(
    *,
    resolved: DeviceEntry | None,
    data_dir: Path | None,
    tuning: VoiceTuningConfig,
    allow_inoperative_capture: bool = False,
) -> DeviceEntry | None:
    """Run the VCHL cold cascade once at boot to populate :class:`ComboStore`.

    ADR §5.11 + §4.4.7. The cascade is a migration side-effect (populates
    :class:`ComboStore`) *and* a fail-over signal: when the cold cascade
    diagnoses the endpoint as :attr:`Diagnosis.KERNEL_INVALIDATED`, the
    cascade short-circuits with ``source="quarantined"`` and we fail-over
    to the next-best capture endpoint via
    :func:`select_alternative_endpoint`, then re-run the cascade once
    against the alternative.

    v0.20.2 / Bug D — on the FINAL cascade result (after any §4.4.7
    fail-over re-run) the helper classifies the outcome via
    :func:`classify_cascade_boot_result`:

    * ``HEALTHY`` / ``DEGRADED`` → return the driving :class:`DeviceEntry`.
    * ``INOPERATIVE`` → raise :class:`CaptureInoperativeError` so the
      factory never constructs an :class:`AudioCaptureTask` that would
      then fall through to MME shared and boot silently deaf. The
      ``allow_inoperative_capture`` escape hatch keeps the legacy path
      available for tests and for operators who explicitly want the
      pre-v0.20.2 behaviour.

    Returns the :class:`DeviceEntry` that should drive the pipeline — the
    same one passed in for the happy path, or the fail-over device when
    the original was quarantined. Returns ``None`` only when ``resolved``
    was ``None`` to begin with (headless CI, broken audio stack) so the
    legacy opener surfaces the error path naturally.

    Silent on any unexpected failure — a corrupt :class:`ComboStore`,
    a transient ``OSError`` on ``data_dir``, or a probe-side bug must
    never block the voice pipeline from starting. The
    :class:`CaptureInoperativeError` path is the ONE exception: it is an
    intentional, structured signal that no viable microphone exists, and
    must propagate unless explicitly suppressed.
    """
    if resolved is None:
        logger.debug("voice_boot_cascade_skipped_no_resolved_device")
        return None

    effective_data_dir = data_dir
    if effective_data_dir is None:
        try:
            from sovyx.engine.config import EngineConfig

            effective_data_dir = EngineConfig().data_dir
        except Exception:  # noqa: BLE001 — config failure must not block boot
            logger.debug("voice_boot_cascade_data_dir_unavailable", exc_info=True)
            return resolved

    from sovyx.voice._capture_task import CaptureInoperativeError
    from sovyx.voice.health._factory_integration import (
        CascadeBootVerdict,
        classify_cascade_boot_result,
    )

    driving_device = resolved
    final_result = None
    try:
        import sys as _sys

        from sovyx.voice._apo_detector import detect_capture_apos
        from sovyx.voice.device_enum import enumerate_devices
        from sovyx.voice.health import current_platform_key
        from sovyx.voice.health._candidate_builder import build_capture_candidates
        from sovyx.voice.health._factory_integration import (
            derive_endpoint_guid,
            run_boot_cascade_for_candidates,
            select_alternative_endpoint,
        )
        from sovyx.voice.health._metrics import record_kernel_invalidated_event

        apo_reports = await asyncio.to_thread(detect_capture_apos)
        all_devices = await asyncio.to_thread(enumerate_devices)
        candidates = build_capture_candidates(
            resolved=resolved,
            all_devices=all_devices,
            platform_key=_sys.platform,
            apo_reports=apo_reports,
        )
        result = await run_boot_cascade_for_candidates(
            candidates=candidates,
            data_dir=effective_data_dir,
            tuning=tuning,
        )
        final_result = result
        # When the cascade-candidate-set produced a winner whose
        # device_index differs from ``resolved`` (the user-preferred one
        # was busy and a session-manager virtual won), rebind
        # ``driving_device`` so the :class:`AudioCaptureTask` opens the
        # device that actually passed the probe. The user's ``mind.yaml``
        # is NOT mutated — their preference is preserved as rank-0
        # candidate for future boots (ADR §2.6).
        if result is not None and result.winning_candidate is not None:
            winner = result.winning_candidate
            for dev in all_devices:
                if dev.index == winner.device_index and dev.host_api_name == winner.host_api_name:
                    driving_device = dev
                    break
        # §4.4.7 fail-over — all candidates ended up in kernel-invalidated
        # quarantine. Pick an alternative endpoint outside the current
        # canonical-name family and re-run the candidate-set cascade.
        if (
            result is not None
            and result.source in {"quarantined", "none"}
            and result.winning_combo is None
            and tuning.kernel_invalidated_failover_enabled
        ):
            excluded_guids = tuple(c.endpoint_guid for c in candidates)
            original_guid = derive_endpoint_guid(resolved, apo_reports=apo_reports)
            alternative = select_alternative_endpoint(
                kind="input",
                apo_reports=apo_reports,
                exclude_endpoint_guids=(original_guid, *excluded_guids),
                exclude_physical_device_ids=(resolved.canonical_name,),
            )
            if alternative is None:
                logger.error(
                    "voice_boot_cascade_no_alternative_endpoint",
                    quarantined_endpoint=original_guid,
                    quarantined_friendly_name=resolved.name,
                    tried_candidates=len(candidates),
                )
                # final_result retains source="none"/"quarantined" → INOPERATIVE
                # verdict below will raise CaptureInoperativeError.
            else:
                logger.warning(
                    "voice_boot_cascade_failover",
                    from_endpoint=original_guid,
                    from_friendly_name=resolved.name,
                    to_friendly_name=alternative.name,
                    to_host_api=alternative.host_api_name,
                )
                record_kernel_invalidated_event(
                    platform=current_platform_key(),
                    host_api=alternative.host_api_name or "unknown",
                    action="failover",
                )
                alt_candidates = build_capture_candidates(
                    resolved=alternative,
                    all_devices=all_devices,
                    platform_key=_sys.platform,
                    apo_reports=apo_reports,
                )
                alt_result = await run_boot_cascade_for_candidates(
                    candidates=alt_candidates,
                    data_dir=effective_data_dir,
                    tuning=tuning,
                )
                driving_device = alternative
                final_result = alt_result
                if alt_result is not None and alt_result.winning_candidate is not None:
                    winner = alt_result.winning_candidate
                    for dev in all_devices:
                        if (
                            dev.index == winner.device_index
                            and dev.host_api_name == winner.host_api_name
                        ):
                            driving_device = dev
                            break
    except Exception:  # noqa: BLE001 — cascade-side faults must never block voice enablement
        logger.warning("voice_boot_cascade_dispatch_failed", exc_info=True)
        return resolved

    outcome = classify_cascade_boot_result(final_result)
    if outcome.verdict is CascadeBootVerdict.INOPERATIVE and not allow_inoperative_capture:
        logger.error(
            "voice_boot_cascade_inoperative",
            reason=outcome.reason,
            attempts=outcome.attempts,
            device=driving_device.index,
            host_api=driving_device.host_api_name,
            friendly_name=driving_device.name,
        )
        msg = (
            f"Voice capture cascade declared endpoint inoperative "
            f"(reason={outcome.reason}, attempts={outcome.attempts}). "
            "No viable microphone path exists; refusing to boot a deaf pipeline."
        )
        raise CaptureInoperativeError(
            msg,
            device=driving_device.index,
            host_api=driving_device.host_api_name,
            reason=outcome.reason,
            attempts=outcome.attempts,
        )
    return driving_device


def _detect_voice_clarity_active(resolved_name: str | None) -> bool:
    """Return True when the active endpoint has a known Voice Clarity APO.

    Small synchronous helper so the async factory can offload the
    registry walk to ``asyncio.to_thread`` without duplicating error
    handling. Never raises — a broken registry read falls back to
    ``False`` (auto-bypass stays opt-in on failure).
    """
    try:
        from sovyx.voice._apo_detector import detect_capture_apos, find_endpoint_report

        report = find_endpoint_report(detect_capture_apos(), device_name=resolved_name)
    except Exception:  # noqa: BLE001 — detector must never break startup
        logger.debug("voice_clarity_detection_failed", exc_info=True)
        return False
    return bool(report is not None and report.voice_clarity_active)


def _create_stt(language: str = "en") -> Any:  # noqa: ANN401
    """Construct an uninitialized :class:`MoonshineSTT` for ``language``.

    The factory calls ``await engine.initialize()`` right after; the
    split exists so :func:`create_voice_pipeline` can keep the sync
    construction trivially test-patchable while the async model load
    stays on the factory's control-flow path.

    Pre-v0.30.9 this function silently dropped ``language`` (``ARG001``
    suppressed the lint), so MoonshineSTT always initialised with its
    default ``"en"`` regardless of the operator-configured mind language.
    The forensic case at ``c:\\Users\\guipe\\Downloads\\logs_01.txt``
    (line 855: ``voice_factory_creating_stt language=pt-br`` immediately
    followed by line 857: ``Initializing MoonshineSTT language=en``)
    pinned the bug. Mission anchor:
    ``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
    §Phase 1 T1.1.

    Critical guardrail: Moonshine v2 only ships models for the language
    set in :data:`sovyx.voice.stt.MOONSHINE_SUPPORTED_LANGUAGES`
    (``ar/en/es/ja/ko/uk/vi/zh``). A naive "remove ARG001 + pass
    language" fix would convert the silent-wrong-language failure into
    a hard ``VoiceFactoryError`` at ``await stt.initialize()`` for
    every operator running ``language=pt-br/pt/de/fr/it/nl/tr/pl/...``.
    The enterprise-grade fix coerces unsupported languages to ``"en"``
    and emits a structured WARN that points to the actionable
    remediation (install a multilingual STT engine or pick a Moonshine-
    supported language). This preserves the v0.30.8 behaviour for
    operators on unsupported languages (English voice keeps working)
    while making the gap observable in dashboards.
    """
    from sovyx.voice.stt import (
        MOONSHINE_SUPPORTED_LANGUAGES,
        MoonshineConfig,
        MoonshineSTT,
    )

    requested = (language or "en").strip().lower()
    if requested in MOONSHINE_SUPPORTED_LANGUAGES:
        logger.info(
            "voice.factory.stt_language_wired",
            **{
                "voice.language": requested,
                "voice.engine": "moonshine",
            },
        )
        return MoonshineSTT(config=MoonshineConfig(language=requested))

    logger.warning(
        "voice.factory.stt_language_unsupported",
        **{
            "voice.requested_language": requested,
            "voice.engine": "moonshine",
            "voice.engine_supported_languages": sorted(
                MOONSHINE_SUPPORTED_LANGUAGES,
            ),
            "voice.coerced_language": "en",
            "voice.action_required": (
                "Moonshine v2 has no model for the requested language. "
                "Install a multilingual STT engine (Parakeet roadmap) or "
                "set the mind language to one of: "
                + ", ".join(sorted(MOONSHINE_SUPPORTED_LANGUAGES))
                + ". Until then voice will transcribe in English."
            ),
        },
    )
    return MoonshineSTT(config=MoonshineConfig(language="en"))


def _create_wake_word_stub() -> Any:  # noqa: ANN401
    """Create a no-op wake word detector.

    The pipeline skips ``wake_word.process_frame`` when
    ``wake_word_enabled=False``, so this stub is never called at runtime.
    It exists only to satisfy the VoicePipeline constructor signature.
    """

    class _NoOpWakeWord:
        def process_frame(self, audio: Any) -> Any:  # noqa: ANN401
            class _Event:
                detected = False

            return _Event()

    return _NoOpWakeWord()
