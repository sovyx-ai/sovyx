"""T1 mission test — wake-word runtime wire-up.

Mission: ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T1.

The factory helper :func:`build_wake_word_router_for_enabled_minds`
enumerates ``data_dir`` for ``mind.yaml`` files, filters to
``wake_word_enabled=True``, and registers each enabled mind on a
fresh :class:`WakeWordRouter`. Backward-compat is bit-exact: zero
enabled minds returns ``None`` so the orchestrator falls through to
its single-mind / no-router code path (matching v0.28.1 behaviour).

Pre-T1, ``MindConfig.wake_word_enabled=True`` had ZERO runtime
effect — the factory always passed ``wake_word_router=None`` to
:class:`VoicePipeline`. T1 closes that gap so the toggle is
load-bearing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from sovyx.engine.errors import VoiceError
from sovyx.voice._wake_word_router import WakeWordRouter
from sovyx.voice.factory._wake_word_wire_up import (
    build_wake_word_router_for_enabled_minds,
)


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


def _patch_onnxruntime() -> object:
    """Return a context that patches ``onnxruntime`` so register_mind
    succeeds without loading a real model. Mirrors the pattern used in
    ``test_wake_word_router_t86.py``."""
    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99

    session = MagicMock()
    inputs_meta = MagicMock()
    inputs_meta.name = "input"
    session.get_inputs.return_value = [inputs_meta]
    session.run.side_effect = lambda *_a, **_kw: [np.array([[0.1]], dtype=np.float32)]
    mock_ort.InferenceSession.return_value = session
    return patch.dict("sys.modules", {"onnxruntime": mock_ort})


# ── Backward-compat (v0.28.1 bit-exact) ──────────────────────────────


class TestBackwardCompatV0281:
    """Zero opt-in => router=None; matches v0.28.1 behaviour."""

    def test_data_dir_does_not_exist_returns_none(self, tmp_path: Path) -> None:
        result = build_wake_word_router_for_enabled_minds(
            data_dir=tmp_path / "missing",
        )
        assert result is None

    def test_empty_data_dir_returns_none(self, tmp_path: Path) -> None:
        result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        assert result is None

    def test_mind_with_wake_word_disabled_returns_none(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=False)
        result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        assert result is None

    def test_multiple_minds_all_disabled_returns_none(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=False)
        _write_mind_yaml(tmp_path, "lucia", wake_word_enabled=False)
        _write_mind_yaml(tmp_path, "jonny", wake_word_enabled=False)
        result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        assert result is None


# ── EXACT path — wake word maps to onnx in pretrained pool ────────────


class TestExactResolution:
    def test_single_enabled_mind_with_exact_match_registers(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_pretrained_model(tmp_path, "aria")

        with _patch_onnxruntime():
            result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)

        assert result is not None
        assert isinstance(result, WakeWordRouter)
        assert len(result) == 1
        assert "aria" in result.registered_minds

    def test_multiple_enabled_minds_all_register(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_mind_yaml(tmp_path, "lucia", wake_word="Lucia", wake_word_enabled=True)
        _write_pretrained_model(tmp_path, "aria")
        _write_pretrained_model(tmp_path, "lucia")

        with _patch_onnxruntime():
            result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)

        assert result is not None
        assert len(result) == 2
        assert set(result.registered_minds) == {"aria", "lucia"}

    def test_disabled_minds_skipped_when_others_enabled(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_mind_yaml(tmp_path, "lucia", wake_word="Lucia", wake_word_enabled=False)
        _write_pretrained_model(tmp_path, "aria")
        _write_pretrained_model(tmp_path, "lucia")

        with _patch_onnxruntime():
            result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)

        assert result is not None
        assert len(result) == 1
        assert "aria" in result.registered_minds
        assert "lucia" not in result.registered_minds


# ── NONE path — refuse-to-start (D3 amendment) ────────────────────────


class TestNoneStrategyRaises:
    """When ``wake_word_enabled=True`` but no ONNX matches AND
    ``stt_fallback_for_none_strategy`` is OFF (default per
    ``feedback_staged_adoption``), the helper raises VoiceError with a
    clear remediation message. Refuse-to-start beats silent failure.

    History: STT-fallback was deferred at v0.28.3 with a
    refuse-to-start contract. Mission ``MISSION-wake-word-stt-fallback-
    2026-05-04`` shipped the opt-in wire-up at v0.30.6; the raise still
    fires when the flag is OFF, but the remediation message now lists
    the flag flip as one of the four operator paths instead of citing
    a deferred mission."""

    def test_no_pretrained_pool_raises(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        # No <data_dir>/wake_word_models/pretrained/ at all.

        with pytest.raises(VoiceError) as exc_info:  # noqa: PT012
            build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        msg = str(exc_info.value)
        assert "wake_word_enabled=True" in msg
        assert "aria" in msg.lower()
        # Either spelling is operator-readable; the remediation must
        # cite the opt-in env var so operators have a discoverable
        # one-line fix without grepping the codebase.
        assert "STT fallback" in msg or "STT-fallback" in msg
        assert "STT_FALLBACK_FOR_NONE_STRATEGY" in msg

    def test_pretrained_pool_missing_target_raises(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        # Pool exists but does not contain aria.onnx.
        _write_pretrained_model(tmp_path, "lucia")

        with pytest.raises(VoiceError) as exc_info:  # noqa: PT012
            build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        msg = str(exc_info.value)
        assert "Aria" in msg or "aria" in msg.lower()
        assert "train-wake-word" in msg

    def test_first_failing_mind_aborts_helper(self, tmp_path: Path) -> None:
        """One mind with NONE → entire build raises (no partial router).

        Refuse-to-start contract: an operator with two enabled minds
        and only one trained model needs to know immediately, not
        silently get a half-populated router."""
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_mind_yaml(tmp_path, "lucia", wake_word="Lucia", wake_word_enabled=True)
        # Only aria.onnx exists; lucia is not trained.
        _write_pretrained_model(tmp_path, "aria")

        with _patch_onnxruntime(), pytest.raises(VoiceError):
            build_wake_word_router_for_enabled_minds(data_dir=tmp_path)


# ── Best-effort enumeration — malformed yaml does not block daemon ────


class TestMalformedYamlSkipped:
    def test_malformed_yaml_logged_and_skipped(self, tmp_path: Path) -> None:
        # One valid + opted-out mind so the helper returns None cleanly.
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=False)
        # Malformed sibling: yaml that fails MindConfig validation.
        bad_dir = tmp_path / "broken"
        bad_dir.mkdir()
        (bad_dir / "mind.yaml").write_text(
            "this is: not: valid: yaml: schema\n",
            encoding="utf-8",
        )
        # Helper must not raise — broken mind is skipped.
        result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        assert result is None

    def test_directory_without_mind_yaml_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "scratch").mkdir()  # no mind.yaml inside
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=False)
        result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        assert result is None
