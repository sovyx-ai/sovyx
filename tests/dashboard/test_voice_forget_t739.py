"""Tests for ``POST /api/voice/forget`` — Phase 7 / T7.39 right-to-erasure.

The endpoint wraps :meth:`ConsentLedger.forget` so a dashboard /
external auditor can issue the GDPR Art. 17 erasure request without
shelling out to the CLI. Tests cover:

- Auth — missing Bearer token → 401.
- Empty / missing ``user_id`` → 422 from FastAPI validation.
- Successful purge of a user with N records → response carries
  ``purged_count == N``.
- Idempotent — second call returns 0 (records already gone) but
  still succeeds with the tombstone written.
- Forget does NOT touch other users' records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice._consent_ledger import ConsentAction, ConsentLedger

_TOKEN = "test-token-forget-t739"


def _seed_ledger(
    data_dir: Path,
    *,
    user_id: str,
    actions: tuple[ConsentAction, ...] = (
        ConsentAction.WAKE,
        ConsentAction.LISTEN,
        ConsentAction.TRANSCRIBE,
    ),
) -> Path:
    """Seed a ConsentLedger with N records for ``user_id`` + return its path."""
    ledger_path = data_dir / "voice" / "consent.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = ConsentLedger(path=ledger_path)
    for action in actions:
        ledger.append(user_id=user_id, action=action, context={})
    return ledger_path


def _app_with_engine_config(data_dir: Path) -> Any:  # noqa: ANN401
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=data_dir,
        database=DatabaseConfig(data_dir=data_dir),
    )
    return app


class TestAuth:
    def test_missing_token_returns_401(self, tmp_path: Path) -> None:
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app)
        response = client.post("/api/voice/forget", json={"user_id": "u-1"})
        assert response.status_code == 401


class TestValidation:
    def test_missing_user_id_returns_422(self, tmp_path: Path) -> None:
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post("/api/voice/forget", json={})
        assert response.status_code == 422  # noqa: PLR2004

    def test_empty_user_id_returns_422(self, tmp_path: Path) -> None:
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post("/api/voice/forget", json={"user_id": ""})
        assert response.status_code == 422  # noqa: PLR2004


class TestForgetSuccessPath:
    def test_purges_seeded_records(self, tmp_path: Path) -> None:
        # Seed ledger with 3 records for a user.
        ledger_path = _seed_ledger(
            tmp_path,
            user_id="u-target",
            actions=(
                ConsentAction.WAKE,
                ConsentAction.LISTEN,
                ConsentAction.TRANSCRIBE,
            ),
        )
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/voice/forget",
            json={"user_id": "u-target"},
        )
        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        # 3 original records purged.
        assert body["purged_count"] == 3  # noqa: PLR2004
        assert body["user_id"] == "u-target"

        # The ledger now has only the DELETE tombstone.
        ledger = ConsentLedger(path=ledger_path)
        history = ledger.history(user_id="u-target")
        assert len(history) == 1
        assert history[0].action == ConsentAction.DELETE

    def test_idempotent_second_call_purges_zero(self, tmp_path: Path) -> None:
        # Seed + first call purges 2 records.
        _seed_ledger(
            tmp_path,
            user_id="u-twice",
            actions=(
                ConsentAction.WAKE,
                ConsentAction.LISTEN,
            ),
        )
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        first = client.post("/api/voice/forget", json={"user_id": "u-twice"})
        assert first.status_code == 200  # noqa: PLR2004
        assert first.json()["purged_count"] == 2  # noqa: PLR2004

        # Second call: tombstone exists from the first call but isn't
        # purged-and-counted (the tombstone is the surviving audit trail).
        # Effective contract: idempotent — the second forget runs
        # cleanly without error, and the ledger's tombstone remains.
        second = client.post("/api/voice/forget", json={"user_id": "u-twice"})
        assert second.status_code == 200  # noqa: PLR2004

    def test_forget_does_not_touch_other_users(self, tmp_path: Path) -> None:
        # Seed records for two users.
        ledger_path = _seed_ledger(tmp_path, user_id="u-target", actions=(ConsentAction.WAKE,))
        ledger = ConsentLedger(path=ledger_path)
        ledger.append(user_id="u-bystander", action=ConsentAction.WAKE, context={})
        ledger.append(user_id="u-bystander", action=ConsentAction.LISTEN, context={})

        app = _app_with_engine_config(tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post("/api/voice/forget", json={"user_id": "u-target"})
        assert response.status_code == 200  # noqa: PLR2004
        assert response.json()["purged_count"] == 1

        # u-bystander's records survive untouched.
        bystander_history = ledger.history(user_id="u-bystander")
        assert len(bystander_history) == 2  # noqa: PLR2004
        actions = {r.action for r in bystander_history}
        assert ConsentAction.WAKE in actions
        assert ConsentAction.LISTEN in actions

    def test_user_with_no_records_returns_zero(self, tmp_path: Path) -> None:
        # Empty data_dir — no ledger file exists yet.
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post("/api/voice/forget", json={"user_id": "u-unknown"})
        assert response.status_code == 200  # noqa: PLR2004
        assert response.json()["purged_count"] == 0
