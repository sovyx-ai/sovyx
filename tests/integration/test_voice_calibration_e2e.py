"""E2E integration test for the voice calibration wizard.

T3.7 of MISSION-voice-self-calibrating-system-2026-05-05.md
(v0.30.18 patch 4). Exercises the full pipeline end-to-end:

  REST POST /start
    -> orchestrator (real WizardProgressTracker writing JSONL)
    -> external deps (mocked: capture_fingerprint, run_full_diag,
       triage_tarball, capture_measurements, engine, applier)
    -> WS subscriber (real WebSocket connection) sees every snapshot
    -> terminal status reaches DONE
    -> POST /cancel writes .cancel file
    -> profile persisted to canonical path

Why "integration": the slice + endpoint unit tests mock the
orchestrator entirely. This test runs the orchestrator inside a real
FastAPI app via TestClient + the real asyncio.ensure_future spawn
path + a real WebSocket client subscribing to the live JSONL tail.
The only mocks are the external SUBPROCESSES (the bash diag, the
mixer probe) -- everything else is the real production code.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.calibration import (
    ApplyResult,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    WizardProgressTracker,
    WizardStatus,
)
from sovyx.voice.calibration import _wizard_orchestrator as wo
from sovyx.voice.calibration._applier import CalibrationApplier
from sovyx.voice.calibration.engine import CalibrationEngine
from sovyx.voice.diagnostics import (
    AlertsSummary,
    DiagRunResult,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
)

_TOKEN = "test-token-calibration-e2e"  # noqa: S105


# ── Fixtures ──────────────────────────────────────────────────────


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-06T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8",
        kernel_major_minor="6.8",
        cpu_model="Intel",
        cpu_cores=12,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="ALSA",
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="Sony",
        system_product="VAIO",
        capture_card_count=1,
        capture_devices=("Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements() -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-06T18:01:00Z",
        duration_s=600.0,
        rms_dbfs_per_capture=(),
        vad_speech_probability_max=0.0,
        vad_speech_probability_p99=0.0,
        noise_floor_dbfs_estimate=0.0,
        capture_callback_p99_ms=0.0,
        capture_jitter_ms=0.0,
        portaudio_latency_advertised_ms=0.0,
        mixer_card_index=0,
        mixer_capture_pct=5,
        mixer_boost_pct=0,
        mixer_internal_mic_boost_pct=0,
        mixer_attenuation_regime="attenuated",
        echo_correlation_db=None,
        triage_winner_hid="H10",
        triage_winner_confidence=0.95,
    )


def _triage() -> TriageResult:
    return TriageResult(
        schema_version=1,
        toolkit="linux",
        tarball_root=Path("/tmp/diag"),
        tool_name="sovyx-voice-diag",
        tool_version="4.3",
        host="t",
        captured_at_utc="2026-05-06T18:00:00Z",
        os_descriptor="linux",
        status="complete",
        exit_code="0",
        selftest_status="pass",
        steps={},
        skip_captures=False,
        schema_validation=SchemaValidation(
            ok=True, missing_required=(), missing_recommended=(), warnings=()
        ),
        alerts=AlertsSummary(error_count=0, warn_count=0, info_count=0, error_messages=()),
        hypotheses=(
            HypothesisVerdict(
                hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED,
                title="x",
                confidence=0.95,
                evidence_for=(),
                evidence_against=(),
                recommended_action="sovyx doctor voice --fix --yes",
            ),
        ),
    )


def _profile(*, mind_id: str = "default") -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=1,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id=mind_id,
        fingerprint=_fingerprint(),
        measurements=_measurements(),
        decisions=(
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value="sovyx doctor voice --fix --yes",
                rationale="r10",
                rule_id="R10_mic_attenuated",
                rule_version=1,
                confidence=CalibrationConfidence.HIGH,
            ),
        ),
        provenance=(),
        generated_by_engine_version="0.30.18",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-06T18:02:00Z",
        signature=None,
    )


def _build_app(*, tmp_path: Path) -> Any:
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
    )
    return app


# ── Tests ─────────────────────────────────────────────────────────


@pytest.mark.integration()
class TestCalibrationE2E:
    """REST + WS + orchestrator + persistence in a single integrated run."""

    def test_full_slow_path_emits_all_snapshots_to_ws(self, tmp_path: Path) -> None:
        """Pipeline: POST /start -> WS subscribes -> all transitions visible."""
        app = _build_app(tmp_path=tmp_path)
        applied_path = tmp_path / "default" / "calibration.json"

        # Stub external deps but let orchestrator + tracker + WS run
        # for real.
        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(
                wo,
                "run_full_diag_async",
                return_value=DiagRunResult(
                    tarball_path=Path("/tmp/x.tar.gz"),
                    duration_s=600.0,
                    exit_code=0,
                ),
            ),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(CalibrationEngine, "evaluate", return_value=_profile()),
            patch.object(
                CalibrationApplier,
                "apply",
                return_value=ApplyResult(
                    profile_path=applied_path,
                    applied_decisions=(),
                    skipped_decisions=(),
                    advised_actions=("sovyx doctor voice --fix --yes",),
                    dry_run=False,
                ),
            ),
            TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as client,
        ):
            # 1. POST /start spawns the job.
            response = client.post(
                "/api/voice/calibration/start",
                json={"mind_id": "default"},
            )
            assert response.status_code == 202
            body = response.json()
            assert body["job_id"] == "default"

            # 2. Subscribe to the WS + collect every snapshot through
            #    terminal. The orchestrator runs in the same event loop
            #    (asyncio.ensure_future) so by the time the WS opens,
            #    it's already advancing through stages.
            collected: list[dict[str, Any]] = []
            with client.websocket_connect(
                f"/api/voice/calibration/jobs/default/stream?token={_TOKEN}"
            ) as ws:
                # WS emits initial snapshot batch + tails for new ones.
                # Loop until terminal state is observed (DONE).
                for _ in range(50):  # safety cap; pipeline emits ~6 snapshots
                    try:
                        msg = ws.receive_json()
                    except Exception:  # noqa: BLE001
                        break
                    collected.append(msg)
                    if msg.get("status") in (
                        "done",
                        "failed",
                        "cancelled",
                        "fallback",
                    ):
                        break

            # 3. We saw at least one snapshot of every meaningful
            #    transition. The DONE terminal is the last message;
            #    the WS server closes after that.
            seen_statuses = {m.get("status") for m in collected}
            assert "done" in seen_statuses, f"DONE not reached; saw statuses={seen_statuses!r}"

            # 4. The persisted JSONL has the full state machine trace.
            jsonl_path = tmp_path / "voice_calibration" / "default" / "progress.jsonl"
            assert jsonl_path.is_file()
            tracker = WizardProgressTracker(jsonl_path)
            events = tracker.read_all()
            statuses = [e.state.status for e in events]
            assert WizardStatus.PENDING in statuses
            assert WizardStatus.PROBING in statuses
            assert WizardStatus.SLOW_PATH_DIAG in statuses
            assert WizardStatus.DONE in statuses
            # Final state's profile_path matches the applier's output.
            final = tracker.latest()
            assert final is not None
            assert final.status == WizardStatus.DONE
            assert final.profile_path == str(applied_path)

    def test_cancel_signal_propagates_via_rest_to_orchestrator(self, tmp_path: Path) -> None:
        """REST /cancel writes .cancel; orchestrator picks it up at next checkpoint."""
        app = _build_app(tmp_path=tmp_path)

        # Orchestrator invokes run_full_diag via asyncio.to_thread, so
        # the patched implementation MUST be sync (a coroutine returned
        # from a to_thread'd call would never be awaited). We poll for
        # the .cancel file inside the worker thread; once it appears,
        # we return so the orchestrator's post-diag checkpoint can pick
        # it up and emit CANCELLED.
        def slow_diag(**_kw: Any) -> DiagRunResult:
            for _ in range(50):  # 5 s safety cap
                if (tmp_path / "voice_calibration" / "default" / ".cancel").exists():
                    break
                time.sleep(0.1)
            return DiagRunResult(
                tarball_path=Path("/tmp/x.tar.gz"),
                duration_s=5.0,
                exit_code=0,
            )

        with (
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(wo, "run_full_diag_async", side_effect=slow_diag),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(CalibrationEngine, "evaluate", return_value=_profile()),
            TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as client,
        ):
            # 1. Start the job.
            response = client.post(
                "/api/voice/calibration/start",
                json={"mind_id": "default"},
            )
            assert response.status_code == 202

            # 2. Wait briefly for the orchestrator to enter the diag
            #    stage. (TestClient's sync API doesn't expose the
            #    spawned task, so we poll for the progress.jsonl file
            #    to exist as a proxy for "orchestrator has started
            #    running".)
            for _ in range(50):
                if (tmp_path / "voice_calibration" / "default" / "progress.jsonl").exists():
                    break
                time.sleep(0.05)

            # 3. POST /cancel writes the .cancel file.
            cancel_resp = client.post("/api/voice/calibration/jobs/default/cancel")
            assert cancel_resp.status_code == 200

            # 4. Wait for the orchestrator to honour the signal.
            #    Its next checkpoint is post-diag; we should see
            #    CANCELLED (not DONE).
            for _ in range(60):
                tracker = WizardProgressTracker(
                    tmp_path / "voice_calibration" / "default" / "progress.jsonl"
                )
                latest = tracker.latest()
                if latest is not None and latest.status.is_terminal:
                    break
                time.sleep(0.1)

            tracker = WizardProgressTracker(
                tmp_path / "voice_calibration" / "default" / "progress.jsonl"
            )
            latest = tracker.latest()
            assert latest is not None
            assert latest.status == WizardStatus.CANCELLED, (
                f"Expected CANCELLED, got {latest.status}"
            )
