"""Tests for the dashboard frontend-error telemetry endpoint."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.routes import telemetry as telemetry_routes
from sovyx.dashboard.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clean_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect token file to tmp_path for isolation."""
    token_file = tmp_path / "token"
    monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)


@pytest.fixture()
def token(tmp_path: Path) -> str:
    t = secrets.token_urlsafe(32)
    (tmp_path / "token").write_text(t)
    return t


@pytest.fixture()
def client(token: str) -> TestClient:  # noqa: ARG001 — ensures token file exists
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> None:
    """Clear the shared rate-limit deque between tests."""
    telemetry_routes._recent.clear()


@pytest.fixture()
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestFrontendErrorEndpoint:
    def test_rejects_unauthenticated(self, client: TestClient) -> None:
        res = client.post("/api/telemetry/frontend-error", json={"message": "boom"})
        assert res.status_code == 401

    def test_accepts_minimal_payload(self, client: TestClient, auth: dict[str, str]) -> None:
        res = client.post(
            "/api/telemetry/frontend-error",
            headers=auth,
            json={"message": "render crash"},
        )
        assert res.status_code == 200
        assert res.json() == {"ok": True, "dropped": False}

    def test_accepts_full_payload(self, client: TestClient, auth: dict[str, str]) -> None:
        res = client.post(
            "/api/telemetry/frontend-error",
            headers=auth,
            json={
                "name": "TypeError",
                "message": "cannot read properties of undefined",
                "stack": "Error: x\n  at App (app.tsx:12)",
                "component_stack": "  at App\n    at Router",
                "url": "https://example.com/logs",
                "user_agent": "Mozilla/5.0",
            },
        )
        assert res.status_code == 200
        assert res.json()["dropped"] is False

    def test_rejects_oversized_message(self, client: TestClient, auth: dict[str, str]) -> None:
        # message field is capped at 1000 chars by pydantic Field
        res = client.post(
            "/api/telemetry/frontend-error",
            headers=auth,
            json={"message": "x" * 2_000},
        )
        assert res.status_code == 422

    def test_rate_limit_drops_excess_reports(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        # Send enough reports to fill the per-window budget + 1 extra.
        for _ in range(telemetry_routes._MAX_PER_WINDOW):
            res = client.post(
                "/api/telemetry/frontend-error",
                headers=auth,
                json={"message": "loop"},
            )
            assert res.status_code == 200
            assert res.json()["dropped"] is False
        # The next one should be dropped (but still return 200).
        res = client.post(
            "/api/telemetry/frontend-error",
            headers=auth,
            json={"message": "loop"},
        )
        assert res.status_code == 200
        assert res.json()["dropped"] is True
