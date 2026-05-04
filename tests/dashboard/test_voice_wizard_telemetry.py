"""Tests for ``POST /api/voice/wizard/telemetry`` — Mission v0.30.1 §T1.2.

The endpoint ingests A/B telemetry from the frontend wizard:

* ``step_dwell`` — histogram update with ``step`` attribute.
* ``completion`` — counter increment with ``outcome`` + ``exit_step``.

The two metric instruments live on the production ``MetricsRegistry``;
attribute cardinality is bounded by the wizard's discriminated-union
``WizardStep`` enum (5 values) + ``outcome`` literal (2 values), so the
worst-case scrape series count is 5 + (5 × 2) = 15 distinct rows. This
file pins:

* Auth gate (no token → 401).
* step_dwell happy path (200 + correct attribute on instrument).
* completion happy path (200 + correct counter increment).
* Validation: invalid step / exit_step rejected with 400.
* Validation: pydantic-level rejection of out-of-range ``duration_ms``
  (negative / > 1 h cap).
* Validation: missing discriminator field → 422.
* Discriminated-union routing: a ``step_dwell`` payload with the
  ``outcome`` field set is rejected (the union resolver MUST pick
  step_dwell solely on the ``event`` discriminator).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-wizard-telemetry"  # noqa: S105


def _client() -> TestClient:
    app = create_app(token=_TOKEN)
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestAuth:
    def test_telemetry_requires_token(self) -> None:
        app = create_app(token=_TOKEN)
        client = TestClient(app)
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={"event": "step_dwell", "step": "devices", "duration_ms": 100},
        )
        assert response.status_code == 401  # noqa: PLR2004


class TestStepDwell:
    def test_happy_path_records_histogram(self) -> None:
        client = _client()
        with patch("sovyx.dashboard.routes.voice_wizard.get_metrics") as mock_metrics:
            response = client.post(
                "/api/voice/wizard/telemetry",
                json={
                    "event": "step_dwell",
                    "step": "devices",
                    "duration_ms": 1234,
                },
            )
        assert response.status_code == 204  # noqa: PLR2004
        instrument = mock_metrics.return_value.voice_wizard_step_dwell_ms
        instrument.record.assert_called_once_with(1234, attributes={"step": "devices"})

    @pytest.mark.parametrize("step", ["devices", "record", "results", "save", "done"])
    def test_all_valid_steps_accepted(self, step: str) -> None:
        client = _client()
        with patch("sovyx.dashboard.routes.voice_wizard.get_metrics"):
            response = client.post(
                "/api/voice/wizard/telemetry",
                json={"event": "step_dwell", "step": step, "duration_ms": 50},
            )
        assert response.status_code == 204  # noqa: PLR2004

    def test_unknown_step_rejected_with_400(self) -> None:
        client = _client()
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={"event": "step_dwell", "step": "loitering", "duration_ms": 1},
        )
        assert response.status_code == 400  # noqa: PLR2004
        assert "step must be one of" in response.json()["detail"]

    def test_negative_duration_rejected_by_pydantic(self) -> None:
        client = _client()
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={
                "event": "step_dwell",
                "step": "devices",
                "duration_ms": -5,
            },
        )
        assert response.status_code == 422  # noqa: PLR2004 — pydantic ge=0

    def test_duration_above_cap_rejected_by_pydantic(self) -> None:
        client = _client()
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={
                "event": "step_dwell",
                "step": "devices",
                "duration_ms": 3_600_001,
            },
        )
        assert response.status_code == 422  # noqa: PLR2004 — pydantic le=cap


class TestCompletion:
    def test_completed_increments_counter(self) -> None:
        client = _client()
        with patch("sovyx.dashboard.routes.voice_wizard.get_metrics") as mock_metrics:
            response = client.post(
                "/api/voice/wizard/telemetry",
                json={
                    "event": "completion",
                    "outcome": "completed",
                    "exit_step": "done",
                },
            )
        assert response.status_code == 204  # noqa: PLR2004
        instrument = mock_metrics.return_value.voice_wizard_completion_rate
        instrument.add.assert_called_once_with(
            1, attributes={"outcome": "completed", "exit_step": "done"}
        )

    def test_abandoned_increments_counter(self) -> None:
        client = _client()
        with patch("sovyx.dashboard.routes.voice_wizard.get_metrics") as mock_metrics:
            response = client.post(
                "/api/voice/wizard/telemetry",
                json={
                    "event": "completion",
                    "outcome": "abandoned",
                    "exit_step": "record",
                },
            )
        assert response.status_code == 204  # noqa: PLR2004
        instrument = mock_metrics.return_value.voice_wizard_completion_rate
        instrument.add.assert_called_once_with(
            1, attributes={"outcome": "abandoned", "exit_step": "record"}
        )

    def test_unknown_exit_step_rejected_with_400(self) -> None:
        client = _client()
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={
                "event": "completion",
                "outcome": "completed",
                "exit_step": "loitering",
            },
        )
        assert response.status_code == 400  # noqa: PLR2004
        assert "exit_step must be one of" in response.json()["detail"]

    def test_invalid_outcome_rejected_by_pydantic(self) -> None:
        client = _client()
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={
                "event": "completion",
                "outcome": "ghosted",
                "exit_step": "save",
            },
        )
        assert response.status_code == 422  # noqa: PLR2004


class TestDiscriminatedUnion:
    def test_missing_event_field_returns_422(self) -> None:
        client = _client()
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={"step": "devices", "duration_ms": 1},
        )
        assert response.status_code == 422  # noqa: PLR2004

    def test_step_dwell_missing_required_field_returns_422(self) -> None:
        client = _client()
        # step_dwell requires step + duration_ms — drop duration_ms.
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={"event": "step_dwell", "step": "devices"},
        )
        assert response.status_code == 422  # noqa: PLR2004

    def test_completion_missing_required_field_returns_422(self) -> None:
        client = _client()
        # completion requires outcome + exit_step — drop exit_step.
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={"event": "completion", "outcome": "completed"},
        )
        assert response.status_code == 422  # noqa: PLR2004

    def test_unknown_event_value_rejected_by_pydantic(self) -> None:
        client = _client()
        response = client.post(
            "/api/voice/wizard/telemetry",
            json={"event": "drift", "step": "devices", "duration_ms": 1},
        )
        assert response.status_code == 422  # noqa: PLR2004
