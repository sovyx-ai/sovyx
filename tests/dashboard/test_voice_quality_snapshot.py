"""Tests for /api/voice/quality-snapshot [Phase 4 T4.26 + T4.37].

Coverage:

* 503 when the engine registry isn't yet wired.
* "no_signal" verdict when the rolling SNR buffer is empty
  (cold boot or sustained silence).
* Verdict mapping: ≥17 dB excellent, 9-17 good, 3-9 degraded,
  <3 poor.
* Noise-floor block reports ``ready=False`` when the long
  window hasn't filled, with the per-window counts surfaced
  for the dashboard's "warming up" state.
* ``agc2: null`` when no capture task is wired (cold boot
  before voice enable).
* ``dnsmos_extras_installed`` boolean reflects probe outcome.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.health._noise_floor_trending import reset_for_tests as floor_reset
from sovyx.voice.health._recent_snr import (
    record_sample,
)
from sovyx.voice.health._recent_snr import (
    reset_for_tests as snr_reset,
)

_TOKEN = "test-token-quality"


@pytest.fixture()
def app():
    application = create_app(token=_TOKEN)
    registry = MagicMock()
    registry.voice_capture_task = None
    application.state.registry = registry
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


@pytest.fixture(autouse=True)
def _clear_buffers() -> None:
    snr_reset()
    floor_reset()
    yield
    snr_reset()
    floor_reset()


class TestRegistryGate:
    def test_503_without_registry(self) -> None:
        app = create_app(token=_TOKEN)
        # No registry attached → endpoint returns 503.
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        resp = client.get("/api/voice/quality-snapshot")
        assert resp.status_code == 503  # noqa: PLR2004


class TestSnrVerdictMapping:
    def test_no_signal_when_buffer_empty(self, client: TestClient) -> None:
        resp = client.get("/api/voice/quality-snapshot")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["snr_verdict"] == "no_signal"
        assert data["snr_p50_db"] is None
        assert data["snr_sample_count"] == 0

    def test_excellent_at_high_snr(self, client: TestClient) -> None:
        for _ in range(11):
            record_sample(snr_db=20.0)
        resp = client.get("/api/voice/quality-snapshot")
        data = resp.json()
        assert data["snr_verdict"] == "excellent"
        assert data["snr_p50_db"] == 20.0  # noqa: PLR2004
        assert data["snr_sample_count"] == 11  # noqa: PLR2004

    def test_good_in_mid_range(self, client: TestClient) -> None:
        for _ in range(11):
            record_sample(snr_db=12.0)
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["snr_verdict"] == "good"

    def test_degraded_below_9_db(self, client: TestClient) -> None:
        for _ in range(11):
            record_sample(snr_db=5.0)
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["snr_verdict"] == "degraded"

    def test_poor_below_3_db(self, client: TestClient) -> None:
        for _ in range(11):
            record_sample(snr_db=1.0)
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["snr_verdict"] == "poor"

    def test_boundary_9_db_is_good(self, client: TestClient) -> None:
        # Exact boundary: 9 dB → "good" (>=9 dB inclusive).
        for _ in range(11):
            record_sample(snr_db=9.0)
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["snr_verdict"] == "good"

    def test_boundary_17_db_is_excellent(self, client: TestClient) -> None:
        for _ in range(11):
            record_sample(snr_db=17.0)
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["snr_verdict"] == "excellent"


class TestNoiseFloorBlock:
    def test_not_ready_when_long_window_empty(self, client: TestClient) -> None:
        data = client.get("/api/voice/quality-snapshot").json()
        nf = data["noise_floor"]
        assert nf["ready"] is False
        assert nf["short_avg_db"] is None
        assert nf["long_avg_db"] is None
        assert nf["drift_db"] is None
        assert nf["short_sample_count"] == 0
        assert nf["long_sample_count"] == 0


class TestAgc2Block:
    def test_agc2_null_without_capture_task(self, client: TestClient) -> None:
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["agc2"] is None

    def test_agc2_payload_when_normalizer_wired(self, app, client: TestClient) -> None:
        # LIVE-2 P1-6: AGC2 stats are read by resolving the REGISTERED
        # AudioCaptureTask service (not a ``registry.voice_capture_task``
        # attribute, which was never set in production). Register the mock
        # capture task the way the producer actually resolves it.
        from sovyx.voice._capture_task import AudioCaptureTask

        capture_task = MagicMock()
        normalizer = MagicMock()
        agc2 = MagicMock()
        agc2.frames_processed = 1234
        agc2.frames_silenced = 200
        agc2.frames_vad_silenced = 50
        agc2.current_gain_db = 4.5
        agc2.speech_level_dbfs = -22.5
        normalizer.agc2 = agc2
        capture_task._normalizer = normalizer
        app.state.registry.is_registered = lambda cls: cls is AudioCaptureTask
        app.state.registry.resolve = AsyncMock(return_value=capture_task)

        data = client.get("/api/voice/quality-snapshot").json()
        assert data["agc2"] == {
            "frames_processed": 1234,
            "frames_silenced": 200,
            "frames_vad_silenced": 50,
            "current_gain_db": 4.5,
            "speech_level_dbfs": -22.5,
        }


class TestDnsmosFlag:
    def test_dnsmos_flag_present_and_boolean(self, client: TestClient) -> None:
        data = client.get("/api/voice/quality-snapshot").json()
        assert isinstance(data["dnsmos_extras_installed"], bool)

    def test_probe_targets_speechmos_package(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LIVE-2 P1-4: the probe must check ``speechmos`` (the real
        importable package shipped via the ``voice-quality`` extra), not
        the non-existent top-level ``dnsmos`` module.

        Fake ``find_spec`` returns a spec only for ``speechmos`` → the
        flag must be True, proving the producer probes the right name.
        """
        import importlib.util

        def fake_find_spec(name: str, *args: object, **kwargs: object) -> object | None:
            return object() if name == "speechmos" else None

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["dnsmos_extras_installed"] is True

    def test_probe_does_not_target_legacy_dnsmos_name(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LIVE-2 P1-4: presence of a bogus top-level ``dnsmos`` module
        must NOT flip the flag — only ``speechmos`` counts. Locks the fix
        against a regression back to the wrong module name.
        """
        import importlib.util

        def fake_find_spec(name: str, *args: object, **kwargs: object) -> object | None:
            return object() if name == "dnsmos" else None

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["dnsmos_extras_installed"] is False


class TestQualityMode:
    """LIVE-2 DNSMOS wire-up — quality_mode is the SSoT the panel uses to
    decide whether it may claim live DNN inference."""

    @staticmethod
    def _spec_for(*present: str):
        def fake_find_spec(name: str, *args: object, **kwargs: object) -> object | None:
            return object() if name in present else None

        return fake_find_spec

    def test_unavailable_when_speechmos_absent(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", self._spec_for())
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["quality_mode"] == "dnsmos_unavailable"
        assert data["dnsmos_ovrl_mos"] is None
        assert data["dnsmos_extras_installed"] is False

    def test_inactive_when_installed_but_engine_off(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # speechmos present, but no engine_config / engine off → installed
        # but NOT producing live scores. The panel must show the SNR proxy.
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", self._spec_for("speechmos"))
        data = client.get("/api/voice/quality-snapshot").json()
        assert data["quality_mode"] == "dnsmos_inactive"
        assert data["dnsmos_ovrl_mos"] is None
        assert data["dnsmos_extras_installed"] is True

    def test_live_when_engine_on_and_estimator_scores(
        self, app, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.util
        from types import SimpleNamespace

        import numpy as np

        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice._quality_metrics import QualityScore

        monkeypatch.setattr(importlib.util, "find_spec", self._spec_for("speechmos"))
        # Opt-in gate ON (default is OFF — staged adoption).
        app.state.engine_config = SimpleNamespace(
            tuning=SimpleNamespace(
                voice=SimpleNamespace(
                    voice_quality_metrics_enabled=True,
                    voice_quality_engine="dnsmos",
                ),
            ),
        )
        # Capture task registered with recent audio; ``_normalizer=None`` so
        # the AGC2 block stays null and doesn't touch a MagicMock.
        capture_task = MagicMock()
        capture_task._normalizer = None
        capture_task.tap_recent_frames = AsyncMock(
            return_value=np.zeros(16_000, dtype=np.int16),
        )
        # Pre-seed a ready estimator so the background build is skipped.
        estimator = MagicMock()
        estimator.score = MagicMock(return_value=QualityScore(ovrl=4.1))
        app.state.voice_dnsmos_estimator = estimator
        app.state.registry.is_registered = lambda cls: cls is AudioCaptureTask
        app.state.registry.resolve = AsyncMock(return_value=capture_task)

        data = client.get("/api/voice/quality-snapshot").json()
        assert data["quality_mode"] == "dnsmos_live"
        assert data["dnsmos_ovrl_mos"] == 4.1  # noqa: PLR2004

    def test_live_falls_back_to_inactive_when_estimator_not_ready(
        self, app, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Engine on + extras present but the estimator is still building
        # (background) → inactive this poll, NOT a fabricated live score.
        import importlib.util
        from types import SimpleNamespace

        from sovyx.voice._capture_task import AudioCaptureTask

        monkeypatch.setattr(importlib.util, "find_spec", self._spec_for("speechmos"))
        app.state.engine_config = SimpleNamespace(
            tuning=SimpleNamespace(
                voice=SimpleNamespace(
                    voice_quality_metrics_enabled=True,
                    voice_quality_engine="dnsmos",
                ),
            ),
        )
        capture_task = MagicMock()
        capture_task._normalizer = None
        app.state.registry.is_registered = lambda cls: cls is AudioCaptureTask
        app.state.registry.resolve = AsyncMock(return_value=capture_task)
        # No app.state.voice_dnsmos_estimator → build kicked off, None returned.

        data = client.get("/api/voice/quality-snapshot").json()
        assert data["quality_mode"] == "dnsmos_inactive"
        assert data["dnsmos_ovrl_mos"] is None
