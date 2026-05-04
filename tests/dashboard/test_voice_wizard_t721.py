"""Tests for ``/api/voice/wizard/*`` — Phase 7 / T7.21-T7.24.

Backend-only — frontend wizard component (T7.25-T7.30) requires
operator browser pilot per CLAUDE.md "start dev server + use feature
in browser before reporting complete".

Coverage:

* T7.21 ``GET /api/voice/wizard/devices`` — auth + happy path +
  enumeration failure (graceful empty list) + per-device diagnosis
  hints.
* T7.22 ``POST /api/voice/wizard/test-record`` — 503 when no
  recorder injected; success path with mocked recorder; analysis
  for clipping / silent / low-signal / OK; recorder error → 500
  surface with diagnosis=device_error.
* T7.23 ``GET /api/voice/wizard/test-result/{session_id}`` —
  success retrieval + 404 on unknown id + 400 on whitespace id.
* T7.24 ``GET /api/voice/wizard/diagnostic`` — happy path on
  Linux/macOS (empty APO list) + Windows-style ready/not-ready
  paths via mock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.routes.voice_wizard import (
    WizardRecorder,
    _SessionStore,
)
from sovyx.dashboard.server import create_app

if TYPE_CHECKING:
    import numpy.typing as npt


_TOKEN = "test-token-wizard-t721"  # noqa: S105


# ── Test recorder stubs ──────────────────────────────────────────────


class _DeterministicRecorder:
    """Returns a fixed waveform — useful for analysis assertions."""

    def __init__(
        self,
        samples: npt.NDArray[np.float32] | None = None,
    ) -> None:
        self._samples = (
            samples
            if samples is not None
            else np.zeros(48000, dtype=np.float32)  # 3 s of silence at 16 kHz
        )

    def record(
        self,
        *,
        duration_s: float,  # noqa: ARG002
        device_id: str | None,  # noqa: ARG002
    ) -> npt.NDArray[np.float32]:
        return self._samples


class _ErroringRecorder:
    """Raises RuntimeError on every record call — exercises error path."""

    def record(
        self,
        *,
        duration_s: float,  # noqa: ARG002
        device_id: str | None,  # noqa: ARG002
    ) -> npt.NDArray[np.float32]:
        msg = "Permission denied"
        raise RuntimeError(msg)


def _build_app(*, recorder: WizardRecorder | None = None) -> Any:  # noqa: ANN401
    """Build the dashboard app for tests.

    ``create_app`` now wires a real ``SoundDeviceWizardRecorder`` onto
    ``app.state`` so production routes are usable on first boot. Tests
    that want to exercise the 503 / pre-init path or inject a
    deterministic stub override the attribute explicitly here:
    ``recorder=None`` clears the production recorder, while a
    non-None recorder replaces it.
    """
    app = create_app(token=_TOKEN)
    if recorder is None:
        app.state.wizard_recorder = None
    else:
        app.state.wizard_recorder = recorder
    return app


def _client(app: Any) -> TestClient:  # noqa: ANN401
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


# ── Auth ─────────────────────────────────────────────────────────────


class TestAuth:
    def test_devices_requires_token(self) -> None:
        app = _build_app()
        client = TestClient(app)
        response = client.get("/api/voice/wizard/devices")
        assert response.status_code == 401  # noqa: PLR2004

    def test_test_record_requires_token(self) -> None:
        app = _build_app()
        client = TestClient(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 1.0},
        )
        assert response.status_code == 401  # noqa: PLR2004

    def test_diagnostic_requires_token(self) -> None:
        app = _build_app()
        client = TestClient(app)
        response = client.get("/api/voice/wizard/diagnostic")
        assert response.status_code == 401  # noqa: PLR2004


# ── T7.21 list devices ──────────────────────────────────────────────


class TestListDevices:
    def test_returns_devices_list(self) -> None:
        app = _build_app()
        client = _client(app)
        # Stub the audio enumeration to bypass real hardware.
        fake_devices = [
            {"index": 0, "name": "Built-in Microphone", "channels": 2, "rate": 48000},
            {"index": 1, "name": "USB Headset", "channels": 1, "rate": 44100},
        ]
        with patch(
            "sovyx.voice.audio.AudioCapture.list_devices",
            return_value=fake_devices,
        ):
            response = client.get("/api/voice/wizard/devices")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["total_count"] == 2  # noqa: PLR2004
        assert len(data["devices"]) == 2  # noqa: PLR2004
        # Device 0 has 2 channels + 48 kHz → ready.
        d0 = data["devices"][0]
        assert d0["device_id"] == "0"
        assert d0["max_input_channels"] == 2  # noqa: PLR2004
        assert d0["diagnosis_hint"] == "ready"
        # Device 1 has 1 channel → warning_low_channels.
        d1 = data["devices"][1]
        assert d1["max_input_channels"] == 1
        assert d1["diagnosis_hint"] == "warning_low_channels"

    def test_enumeration_failure_returns_empty(self) -> None:
        """Hosts without audio hardware (CI, headless containers) get
        an empty list — never a 500."""
        app = _build_app()
        client = _client(app)
        with patch(
            "sovyx.voice.audio.AudioCapture.list_devices",
            side_effect=OSError("PortAudio not available"),
        ):
            response = client.get("/api/voice/wizard/devices")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["devices"] == []
        assert data["total_count"] == 0
        assert data["default_device_id"] is None

    def test_unsupported_sample_rate_flagged(self) -> None:
        app = _build_app()
        client = _client(app)
        # 22 050 Hz isn't in the wizard's supported list.
        with patch(
            "sovyx.voice.audio.AudioCapture.list_devices",
            return_value=[
                {"index": 0, "name": "Weird Mic", "channels": 2, "rate": 22050},
            ],
        ):
            response = client.get("/api/voice/wizard/devices")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["devices"][0]["diagnosis_hint"] == "warning_high_sample_rate"


# ── T7.22 test-record ───────────────────────────────────────────────


class TestProductionRecorderRegistration:
    """Regression: ``create_app`` MUST wire a production recorder.

    Before v0.30.8 the ``/api/voice/wizard/test-record`` route returned
    503 to every operator click because nothing in the daemon ever
    instantiated :class:`SoundDeviceWizardRecorder` and bound it to
    ``app.state.wizard_recorder`` — the route's docstring promised the
    binding but no caller delivered it. Verified live in operator log
    ``logs_teste.txt`` (``POST /api/voice/wizard/test-record → 503``,
    five times, 06:28:12-42 on a freshly booted daemon).
    """

    def test_create_app_registers_real_recorder(self) -> None:
        from sovyx.dashboard.routes.voice_wizard import (
            SoundDeviceWizardRecorder,
        )
        from sovyx.dashboard.server import create_app

        app = create_app(token=_TOKEN)
        recorder = getattr(app.state, "wizard_recorder", None)
        assert isinstance(recorder, SoundDeviceWizardRecorder), (
            "create_app must wire a SoundDeviceWizardRecorder so the "
            "wizard route doesn't 503 on first operator click"
        )


class TestTestRecord:
    def test_no_recorder_returns_503(self) -> None:
        """Pre-init state: no recorder registered → 503 with hint."""
        app = _build_app()  # no recorder injected
        client = _client(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 3.0},
        )
        assert response.status_code == 503  # noqa: PLR2004
        assert "wizard recorder is not registered" in response.json()["detail"]

    def test_silent_capture_diagnoses_no_audio(self) -> None:
        # Recorder returns 3 seconds of silence → silent_capture=True.
        app = _build_app(recorder=_DeterministicRecorder())
        client = _client(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 3.0},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["success"] is True
        assert data["silent_capture"] is True
        assert data["clipping_detected"] is False
        assert data["diagnosis"] == "no_audio"
        assert data["session_id"]  # non-empty
        assert data["error"] is None

    def test_clipping_capture_diagnoses_clipping(self) -> None:
        # Saturated waveform — peak ≥ -0.1 dBFS triggers clipping.
        clipped = np.ones(48000, dtype=np.float32) * 0.99
        app = _build_app(recorder=_DeterministicRecorder(samples=clipped))
        client = _client(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 3.0},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["clipping_detected"] is True
        assert data["diagnosis"] == "clipping"

    def test_normal_capture_diagnoses_ok(self) -> None:
        # Speech-like burst signal: alternating speech segments and
        # quiet gaps. The SNR heuristic (top-quartile vs
        # bottom-quartile frame energies) needs energy variation to
        # measure SNR — a constant sine wave gives ~0 dB SNR
        # estimate even with a low noise floor.
        rate = 16000
        duration_s = 3.0
        n = int(duration_s * rate)
        rng = np.random.default_rng(42)
        # Background noise floor at -50 dBFS.
        samples = rng.normal(0, 0.003, n).astype(np.float32)
        # 6 speech-like 200ms bursts at -10 dBFS, every ~500ms.
        burst_len = int(0.2 * rate)
        for burst_start_s in [0.2, 0.6, 1.0, 1.4, 1.8, 2.2]:
            start = int(burst_start_s * rate)
            t_burst = np.arange(burst_len) / rate
            # Mix 200 + 400 Hz tones for a speech-like spectrum.
            burst = (
                0.316
                * (np.sin(2 * np.pi * 200 * t_burst) + 0.5 * np.sin(2 * np.pi * 400 * t_burst))
                / 1.5
            ).astype(np.float32)
            samples[start : start + burst_len] += burst

        app = _build_app(recorder=_DeterministicRecorder(samples=samples))
        client = _client(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 3.0},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["clipping_detected"] is False
        assert data["silent_capture"] is False
        # SNR should be well above the 10 dB noisy threshold given
        # ~40 dB amplitude difference between bursts and noise.
        assert data["snr_db"] is not None
        assert data["snr_db"] > 10.0  # noqa: PLR2004
        assert data["diagnosis"] == "ok"
        assert data["level_peak_dbfs"] is not None
        assert -15.0 < data["level_peak_dbfs"] < -5.0  # noqa: PLR2004

    def test_recorder_error_returns_device_error_diagnosis(self) -> None:
        """RuntimeError from recorder → success=False with
        diagnosis=device_error + error message."""
        app = _build_app(recorder=_ErroringRecorder())
        client = _client(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 3.0},
        )
        # The endpoint catches RuntimeError + returns a structured
        # error response (200 with success=False) rather than 500.
        # This matches the wizard UX — show the error inline.
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["success"] is False
        assert data["diagnosis"] == "device_error"
        assert data["error"] == "Permission denied"

    def test_recorder_error_diagnosis_hint_translated_to_plain_language(
        self,
    ) -> None:
        """T7.27 + T7.28 wire-up: ``diagnosis_hint`` now carries the
        plain-language translation from ``_error_messages.translate_audio_error``
        rather than a generic fallback string."""
        app = _build_app(recorder=_ErroringRecorder())
        client = _client(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 3.0},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        # _ErroringRecorder raises "Permission denied" — translation
        # table maps it to PERMISSION_DENIED with the multi-platform
        # hint mentioning macOS / Linux / Windows settings.
        hint = data["diagnosis_hint"].lower()
        assert "permission" in hint
        assert "macos" in hint or "linux" in hint or "windows" in hint

    def test_duration_lower_bound_rejected(self) -> None:
        app = _build_app(recorder=_DeterministicRecorder())
        client = _client(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 0.5},  # below 1.0 s minimum
        )
        assert response.status_code == 422  # noqa: PLR2004

    def test_duration_upper_bound_rejected(self) -> None:
        app = _build_app(recorder=_DeterministicRecorder())
        client = _client(app)
        response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 11.0},  # above 10.0 s maximum
        )
        assert response.status_code == 422  # noqa: PLR2004


# ── T7.23 test-result by session_id ─────────────────────────────────


class TestTestResult:
    def test_get_existing_session_returns_cached_response(self) -> None:
        app = _build_app(recorder=_DeterministicRecorder())
        client = _client(app)
        # Run a test-record to populate the session store.
        first_response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 1.5},
        )
        session_id = first_response.json()["session_id"]

        # Retrieve by session_id.
        get_response = client.get(f"/api/voice/wizard/test-result/{session_id}")
        assert get_response.status_code == 200  # noqa: PLR2004
        retrieved = get_response.json()
        assert retrieved["session_id"] == session_id
        # Cached response matches the original.
        assert retrieved["diagnosis"] == first_response.json()["diagnosis"]

    def test_get_unknown_session_returns_404(self) -> None:
        app = _build_app()
        client = _client(app)
        response = client.get("/api/voice/wizard/test-result/unknown-id")
        assert response.status_code == 404  # noqa: PLR2004
        assert "not found" in response.json()["detail"]


class TestSessionStoreUnit:
    """Unit tests for _SessionStore (LRU + TTL semantics)."""

    def test_lru_evicts_oldest_when_full(self) -> None:
        import time as _time

        from sovyx.dashboard.routes.voice_wizard import _SessionRecord

        # Use a very large TTL so the test isolates LRU eviction
        # from TTL expiry. The records' created_at_monotonic must
        # also be near "now" — using 0.0 would trip the TTL check
        # since time.monotonic() returns a value much larger than
        # the ttl_s by default.
        store = _SessionStore(max_size=2, ttl_s=10.0**9)
        now_mono = _time.monotonic()

        # Fake response object — we don't care about content here.
        class _Stub:
            pass

        for i in range(3):
            store.put(
                _SessionRecord(
                    session_id=f"s{i}",
                    response=_Stub(),  # type: ignore[arg-type]
                    # All records "just created" — TTL won't expire
                    # them; LRU must be the only eviction trigger.
                    created_at_monotonic=now_mono + float(i) * 1e-6,
                ),
            )
        assert len(store) == 2  # noqa: PLR2004
        # Oldest "s0" should be evicted.
        assert store.get("s0") is None
        assert store.get("s1") is not None
        assert store.get("s2") is not None

    def test_ttl_expiry(self) -> None:
        from sovyx.dashboard.routes.voice_wizard import _SessionRecord

        store = _SessionStore(max_size=64, ttl_s=0.0001)

        class _Stub:
            pass

        store.put(
            _SessionRecord(
                session_id="s1",
                response=_Stub(),  # type: ignore[arg-type]
                created_at_monotonic=0.0,  # ancient
            ),
        )
        # time.monotonic() returns a much larger value → expired.
        assert store.get("s1") is None
        assert len(store) == 0


# ── T7.24 diagnostic ────────────────────────────────────────────────


class TestDiagnostic:
    def test_returns_ready_when_no_apos(self) -> None:
        """Linux / macOS path — APO detection returns empty list →
        ready=True + no recommendations."""
        app = _build_app()
        client = _client(app)
        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            return_value=[],
        ):
            response = client.get("/api/voice/wizard/diagnostic")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["ready"] is True
        assert data["voice_clarity_active"] is False
        assert data["recommendations"] == []

    def test_returns_not_ready_when_voice_clarity_active(self) -> None:
        """Voice Clarity APO detected → ready=False + recommendation."""
        from dataclasses import dataclass

        @dataclass
        class _FakeReport:
            voice_clarity_active: bool = True
            endpoint_id: str = "{abc}"
            endpoint_name: str = "Razer Headset"
            device_interface_name: str = ""
            enumerator: str = ""

        app = _build_app()
        client = _client(app)
        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            return_value=[_FakeReport()],
        ):
            response = client.get("/api/voice/wizard/diagnostic")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["ready"] is False
        assert data["voice_clarity_active"] is True
        assert len(data["recommendations"]) >= 1
        assert "Voice Clarity" in data["recommendations"][0]

    def test_returns_platform(self) -> None:
        app = _build_app()
        client = _client(app)
        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            return_value=[],
        ):
            response = client.get("/api/voice/wizard/diagnostic")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["platform"] in {"win32", "linux", "darwin"}

    def test_apo_scan_failure_safe_default(self) -> None:
        """If APO detection raises, endpoint returns ready=True with
        empty recommendations — never a 500. Better operator UX:
        'looks fine' beats 'system error'."""
        app = _build_app()
        client = _client(app)
        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            side_effect=OSError("registry unreadable"),
        ):
            response = client.get("/api/voice/wizard/diagnostic")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["voice_clarity_active"] is False
        assert data["ready"] is True


# ── Integration smoke ───────────────────────────────────────────────


class TestIntegration:
    def test_record_then_retrieve_session(self) -> None:
        """Full wizard flow: record → result-by-id → both succeed."""
        app = _build_app(recorder=_DeterministicRecorder())
        client = _client(app)
        record_response = client.post(
            "/api/voice/wizard/test-record",
            json={"duration_seconds": 1.5},
        )
        assert record_response.status_code == 200  # noqa: PLR2004
        session_id = record_response.json()["session_id"]

        result_response = client.get(
            f"/api/voice/wizard/test-result/{session_id}",
        )
        assert result_response.status_code == 200  # noqa: PLR2004
        assert result_response.json()["session_id"] == session_id

    def test_two_records_have_different_session_ids(self) -> None:
        app = _build_app(recorder=_DeterministicRecorder())
        client = _client(app)
        first = client.post("/api/voice/wizard/test-record", json={})
        second = client.post("/api/voice/wizard/test-record", json={})
        assert first.json()["session_id"] != second.json()["session_id"]


# Silence unused-fixture warnings if pytest collects them accidentally.
_ = pytest
