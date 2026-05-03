"""T3 mission tests — POST /api/mind/{mind_id}/wake-word/toggle.

Mission: ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T3.

Two-phase contract:

1. PERSIST ``wake_word_enabled`` to ``<data_dir>/<mind_id>/mind.yaml``
   via :class:`ConfigEditor.set_scalar` (atomic + per-path locked).
2. HOT-APPLY to the running pipeline:
   * ``enabled=True``: resolve via the pretrained pool + register on
     the live :class:`WakeWordRouter`.
   * ``enabled=False``: unregister from the live router.

Persist always runs; hot-apply is best-effort (cold-start /
single-mind / NONE strategy → ``applied_immediately=False`` with the
operator-facing diagnostic in ``hot_apply_detail``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine.errors import VoiceError

_TOKEN = "test-token-mind-wake-word-toggle"  # noqa: S105 — test fixture token


# ── Helpers ──────────────────────────────────────────────────────────


def _write_mind_yaml(
    data_dir: Path,
    mind_id: str,
    *,
    wake_word: str = "Aria",
    wake_word_enabled: bool = False,
) -> None:
    mind_dir = data_dir / mind_id
    mind_dir.mkdir(parents=True, exist_ok=True)
    enabled_str = "true" if wake_word_enabled else "false"
    (mind_dir / "mind.yaml").write_text(
        f"id: {mind_id}\n"
        f"name: {mind_id.capitalize()}\n"
        f"wake_word: {wake_word}\n"
        f"wake_word_enabled: {enabled_str}\n",
        encoding="utf-8",
    )


def _write_pretrained_model(data_dir: Path, name: str) -> Path:
    pool = data_dir / "wake_word_models" / "pretrained"
    pool.mkdir(parents=True, exist_ok=True)
    target = pool / f"{name}.onnx"
    target.write_bytes(b"fake onnx bytes")
    return target


def _read_wake_word_enabled(data_dir: Path, mind_id: str) -> bool:
    """Read the persisted wake_word_enabled bool from mind.yaml."""
    import yaml

    mind_yaml = data_dir / mind_id / "mind.yaml"
    raw = mind_yaml.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return bool(data["wake_word_enabled"])


def _build_app(
    *,
    tmp_path: Path,
    pipeline: object | None = None,
) -> Any:
    """Build a test app with EngineConfig + optional VoicePipeline.

    When ``pipeline`` is None, the registry reports voice subsystem as
    not registered → ``applied_immediately=False`` with cold-start
    diagnostic. When it's a MagicMock, ``register_mind_wake_word`` /
    ``unregister_mind_wake_word`` are spied on for delegation checks.
    """
    from sovyx.engine.config import DatabaseConfig, EngineConfig
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
    )

    registry = MagicMock()
    if pipeline is None:
        registry.is_registered = MagicMock(return_value=False)
    else:

        def _is_registered(cls: object) -> bool:
            return cls is VoicePipeline

        registry.is_registered = MagicMock(side_effect=_is_registered)
        registry.resolve = AsyncMock(return_value=pipeline)
    app.state.registry = registry
    return app


# ── Auth ─────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_token_returns_401(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria")
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app)
        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": True},
        )
        assert response.status_code == 401  # noqa: PLR2004


# ── Validation ───────────────────────────────────────────────────────


class TestValidation:
    def test_whitespace_mind_id_returns_400(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/%20%20/wake-word/toggle",
            json={"enabled": True},
        )
        assert response.status_code == 400  # noqa: PLR2004
        assert "non-empty" in response.json()["detail"]

    def test_missing_enabled_returns_422(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria")
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post("/api/mind/aria/wake-word/toggle", json={})
        assert response.status_code == 422  # noqa: PLR2004

    def test_unknown_mind_returns_404(self, tmp_path: Path) -> None:
        # No mind.yaml on disk for "ghost".
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/ghost/wake-word/toggle",
            json={"enabled": True},
        )
        assert response.status_code == 404  # noqa: PLR2004
        assert "ghost" in response.json()["detail"]


# ── Persist (cold-start, no voice subsystem) ─────────────────────────


class TestPersistColdStart:
    """Pipeline not registered yet — persist runs, hot-apply reports
    applied_immediately=False with cold-start detail."""

    def test_persist_enables_returns_applied_false(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=False)
        # T1 of pre-wake-word-ui-hardening (2026-05-03): pre-validate
        # requires the ONNX to exist BEFORE persist. Test the cold-start
        # path with a model present so we isolate "no pipeline yet" from
        # "no model" — the latter is covered by
        # ``test_enable_with_no_model_rejects_with_422_no_persist``.
        _write_pretrained_model(tmp_path, "aria")
        app = _build_app(tmp_path=tmp_path)  # no pipeline
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": True},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["mind_id"] == "aria"
        assert body["enabled"] is True
        assert body["persisted"] is True
        assert body["applied_immediately"] is False
        assert body["hot_apply_detail"] is not None
        assert "next boot" in body["hot_apply_detail"]
        # Side-effect: yaml updated.
        assert _read_wake_word_enabled(tmp_path, "aria") is True

    def test_persist_disables_returns_applied_false(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=True)
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": False},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["enabled"] is False
        assert body["persisted"] is True
        assert body["applied_immediately"] is False
        assert _read_wake_word_enabled(tmp_path, "aria") is False


# ── Hot-apply: enable=True ───────────────────────────────────────────


class TestHotApplyEnable:
    """Enable while voice subsystem is running — resolve + register."""

    def test_enable_with_pretrained_match_calls_register(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=False)
        _write_pretrained_model(tmp_path, "aria")

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline.register_mind_wake_word = MagicMock(return_value=None)
        pipeline.unregister_mind_wake_word = MagicMock(return_value=False)

        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": True},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["applied_immediately"] is True
        assert body["hot_apply_detail"] is None
        pipeline.register_mind_wake_word.assert_called_once()
        # Confirm delegation contract: positional MindId + kwarg model_path.
        call = pipeline.register_mind_wake_word.call_args
        assert str(call.args[0]) == "aria"
        assert str(call.kwargs["model_path"]).endswith("aria.onnx")

    def test_enable_with_no_model_rejects_with_422_no_persist(self, tmp_path: Path) -> None:
        """Operator enables a mind whose wake word has no trained model.

        T1 of MISSION-pre-wake-word-ui-hardening (2026-05-03): the
        endpoint refuses-to-persist when the wake-word ONNX cannot be
        resolved. Returns HTTP 422 with the resolver's remediation
        message. The yaml is NOT touched (operator's intent does NOT
        become durable until they have a trained model OR drop the
        ONNX into the pretrained pool). The previous v0.28.2 contract
        (persist + applied_immediately=False) was a footgun: the
        persisted state would brick the next daemon boot via
        :func:`build_wake_word_router_for_enabled_minds` raising
        VoiceError on NONE strategy."""
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=False)
        # No pretrained model for aria.

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": True},
        )

        # 422: well-formed request, semantic precondition (model exists) unmet.
        assert response.status_code == 422  # noqa: PLR2004
        detail = response.json()["detail"]
        # Resolver's remediation message surfaces directly to the operator.
        assert "train-wake-word" in detail
        assert "Aria" in detail
        # YAML is NOT touched — no side-effect persistence.
        assert _read_wake_word_enabled(tmp_path, "aria") is False
        # register was NEVER called (rejected before persist + hot-apply).
        pipeline.register_mind_wake_word.assert_not_called()

    def test_enable_in_single_mind_mode_returns_applied_false(self, tmp_path: Path) -> None:
        """Pipeline is single-mind (no router) — VoiceError surfaces in
        ``hot_apply_detail``, persist still runs."""
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=False)
        _write_pretrained_model(tmp_path, "aria")

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline.register_mind_wake_word = MagicMock(
            side_effect=VoiceError("router not configured (single-mind mode)"),
        )
        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": True},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["persisted"] is True
        assert body["applied_immediately"] is False
        assert "single-mind" in body["hot_apply_detail"]


# ── Hot-apply: enable=False ──────────────────────────────────────────


class TestHotApplyDisable:
    def test_disable_calls_unregister(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=True)

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline.unregister_mind_wake_word = MagicMock(return_value=True)

        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": False},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["applied_immediately"] is True
        pipeline.unregister_mind_wake_word.assert_called_once()
        call = pipeline.unregister_mind_wake_word.call_args
        assert str(call.args[0]) == "aria"

    def test_disable_in_single_mind_mode_returns_applied_false(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=True)

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline.unregister_mind_wake_word = MagicMock(
            side_effect=VoiceError("router not configured (single-mind mode)"),
        )

        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": False},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["persisted"] is True
        assert body["applied_immediately"] is False
        assert "single-mind" in body["hot_apply_detail"]


# ── Idempotency + comment-preservation contract ──────────────────────


class TestPersistContract:
    def test_persist_preserves_other_fields(self, tmp_path: Path) -> None:
        """ConfigEditor.set_scalar must NOT clobber unrelated fields."""
        mind_dir = tmp_path / "aria"
        mind_dir.mkdir()
        (mind_dir / "mind.yaml").write_text(
            "id: aria\n"
            "name: Aria\n"
            "wake_word: Aria\n"
            "wake_word_enabled: false\n"
            "voice_id: af_heart\n"
            "voice_language: en\n",
            encoding="utf-8",
        )
        # T1 pre-validate requires the model to exist before persist.
        _write_pretrained_model(tmp_path, "aria")
        app = _build_app(tmp_path=tmp_path)  # cold-start ok
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": True},
        )

        assert response.status_code == 200  # noqa: PLR2004
        # All other fields survive.
        import yaml

        data = yaml.safe_load((tmp_path / "aria" / "mind.yaml").read_text())
        assert data["id"] == "aria"
        assert data["name"] == "Aria"
        assert data["wake_word"] == "Aria"
        assert data["wake_word_enabled"] is True
        assert data["voice_id"] == "af_heart"
        assert data["voice_language"] == "en"

    def test_repeated_toggle_is_idempotent(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=False)
        # T1 pre-validate requires the model to exist before persist.
        _write_pretrained_model(tmp_path, "aria")
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        for _ in range(3):
            response = client.post(
                "/api/mind/aria/wake-word/toggle",
                json={"enabled": True},
            )
            assert response.status_code == 200  # noqa: PLR2004
            assert response.json()["enabled"] is True

        assert _read_wake_word_enabled(tmp_path, "aria") is True


# ── T1 contract additions (MISSION-pre-wake-word-ui-hardening) ───────


class TestT1PreValidateContract:
    """T1 D1: refuse-to-persist when ``enabled=True`` but no ONNX
    resolves. Disable path NEVER runs pre-validate (nothing to resolve)."""

    def test_disable_with_no_model_still_works(self, tmp_path: Path) -> None:
        """Symmetric counterpart to refuse-to-persist on enable.

        The disable path must NOT pre-validate the wake-word model —
        an operator who's disabling the feature might be doing so
        precisely BECAUSE they realised they don't have a trained
        model, and forcing them to train one before they can disable
        would be a UX deadlock."""
        # YAML carries enabled=true but no pretrained model exists.
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        app = _build_app(tmp_path=tmp_path)  # cold-start ok
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": False},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["enabled"] is False
        assert body["persisted"] is True
        # Side-effect: yaml updated to false even though no model existed.
        assert _read_wake_word_enabled(tmp_path, "aria") is False

    def test_malformed_yaml_returns_500(self, tmp_path: Path) -> None:
        """Malformed mind.yaml during pre-validate is a server-state
        error (500), not a precondition error (422). Operator must fix
        the YAML — the toggle endpoint cannot recover.

        This is distinct from "yaml exists but wake-word doesn't
        resolve" (422). Different failure modes need different surfaces
        so the dashboard can render the right operator action."""
        mind_dir = tmp_path / "aria"
        mind_dir.mkdir()
        (mind_dir / "mind.yaml").write_text(
            "this is: not: valid: yaml: schema\nweird: [unclosed list\n",
            encoding="utf-8",
        )
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": True},
        )

        assert response.status_code == 500  # noqa: PLR2004
        # YAML untouched (the bad file remains as-is).

    def test_enable_with_match_persists_and_registers_unchanged(self, tmp_path: Path) -> None:
        """Pin the happy path: ``enabled=True`` + valid model + live
        pipeline still produces the v0.28.2 success contract
        (persist + register + applied_immediately=True)."""
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=False)
        _write_pretrained_model(tmp_path, "aria")

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline.register_mind_wake_word = MagicMock(return_value=None)

        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/wake-word/toggle",
            json={"enabled": True},
        )

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["enabled"] is True
        assert body["persisted"] is True
        assert body["applied_immediately"] is True
        assert body["hot_apply_detail"] is None
        # YAML updated.
        assert _read_wake_word_enabled(tmp_path, "aria") is True
        # Register fired with the pre-resolved path.
        pipeline.register_mind_wake_word.assert_called_once()
        call = pipeline.register_mind_wake_word.call_args
        assert str(call.args[0]) == "aria"
        assert str(call.kwargs["model_path"]).endswith("aria.onnx")
