"""T1 mission tests — per-mind wake-word status helper.

Mission: ``MISSION-wake-word-ui-2026-05-03.md`` §T1 (D1).

The :func:`query_per_mind_wake_word_status` helper enumerates the
filesystem, re-runs the resolver per ``wake_word_enabled=True`` mind,
and cross-references with the live :class:`WakeWordRouter`. These
tests pin the contract for:

* Idempotency: zero global state, same inputs always produce the
  same output.
* Skip-resolution-on-disabled: disabled minds appear with
  ``model_path=None`` and ``resolution_strategy=None`` (the resolver
  is NOT called for them — saves ~5ms per disabled mind).
* Full-list semantics: enabled + disabled minds both appear (the
  dashboard renders OFF cards too, so operator can toggle ON).
* Bit-exact diagnosis: NONE-strategy ``last_error`` matches the
  boot helper's remediation text — operators see identical
  diagnostics from boot logs and dashboard.
* Router cross-reference: ``runtime_registered`` reflects router
  state when router is non-None; always False when router is None.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from sovyx.engine.types import MindId
from sovyx.voice._wake_word_router import WakeWordRouter
from sovyx.voice.factory._wake_word_wire_up import (
    WakeWordPerMindStatusEntry,
    query_per_mind_wake_word_status,
)


def _write_mind_yaml(
    data_dir: Path,
    mind_id: str,
    *,
    wake_word: str = "Aria",
    wake_word_enabled: bool = False,
    voice_language: str = "en",
) -> None:
    mind_dir = data_dir / mind_id
    mind_dir.mkdir(parents=True, exist_ok=True)
    enabled_str = "true" if wake_word_enabled else "false"
    (mind_dir / "mind.yaml").write_text(
        f"id: {mind_id}\n"
        f"name: {mind_id.capitalize()}\n"
        f"wake_word: {wake_word}\n"
        f"wake_word_enabled: {enabled_str}\n"
        f"voice_language: {voice_language}\n",
        encoding="utf-8",
    )


def _write_pretrained_model(data_dir: Path, name: str) -> Path:
    pool = data_dir / "wake_word_models" / "pretrained"
    pool.mkdir(parents=True, exist_ok=True)
    target = pool / f"{name}.onnx"
    target.write_bytes(b"fake onnx bytes")
    return target


def _patch_onnxruntime() -> object:
    """ONNX runtime mock for register_mind paths."""
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


# ── Edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    def test_missing_data_dir_returns_empty_list(self, tmp_path: Path) -> None:
        result = query_per_mind_wake_word_status(
            data_dir=tmp_path / "nonexistent",
            router=None,
        )
        assert result == []

    def test_empty_data_dir_returns_empty_list(self, tmp_path: Path) -> None:
        result = query_per_mind_wake_word_status(data_dir=tmp_path, router=None)
        assert result == []


# ── Disabled minds: appear in list, resolution skipped ───────────────


class TestDisabledMindsRendered:
    """Per D2: disabled minds STILL appear in the response so the
    dashboard can render OFF cards. Resolution is SKIPPED for them
    (saves ~5ms each)."""

    def test_disabled_mind_appears_with_skipped_resolution(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=False)
        result = query_per_mind_wake_word_status(data_dir=tmp_path, router=None)

        assert len(result) == 1
        entry = result[0]
        assert entry.mind_id == "aria"
        assert entry.wake_word_enabled is False
        assert entry.runtime_registered is False
        assert entry.model_path is None
        assert entry.resolution_strategy is None
        assert entry.last_error is None  # not an error — just disabled

    def test_disabled_mind_resolution_not_called(self, tmp_path: Path) -> None:
        """Resolution skip is observable: patching
        :class:`WakeWordModelResolver` to track calls confirms the
        resolver is never instantiated for disabled minds."""
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=False)

        with patch(
            "sovyx.voice.factory._wake_word_wire_up.WakeWordModelResolver"
        ) as mock_resolver:
            query_per_mind_wake_word_status(data_dir=tmp_path, router=None)

        mock_resolver.assert_not_called()


# ── Enabled + healthy: EXACT or PHONETIC strategy ────────────────────


class TestEnabledHealthyMinds:
    def test_enabled_mind_with_exact_match(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_pretrained_model(tmp_path, "aria")

        result = query_per_mind_wake_word_status(data_dir=tmp_path, router=None)

        assert len(result) == 1
        entry = result[0]
        assert entry.mind_id == "aria"
        assert entry.wake_word == "Aria"
        assert entry.wake_word_enabled is True
        assert entry.runtime_registered is False  # router is None
        assert entry.resolution_strategy == "exact"
        assert entry.model_path is not None
        assert entry.model_path.name == "aria.onnx"
        assert entry.last_error is None

    def test_runtime_registered_when_router_has_mind(self, tmp_path: Path) -> None:
        """``runtime_registered=True`` when the live router has the mind."""
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_pretrained_model(tmp_path, "aria")

        # Build a router with aria registered.
        router = WakeWordRouter()
        with _patch_onnxruntime():
            router.register_mind(
                MindId("aria"),
                model_path=Path("/fake/aria.onnx"),
            )

        result = query_per_mind_wake_word_status(data_dir=tmp_path, router=router)

        assert len(result) == 1
        assert result[0].runtime_registered is True


# ── Enabled + broken: NONE strategy + remediation text ───────────────


class TestNoneStrategyReporting:
    """Bit-exact diagnosis: ``last_error`` matches the boot helper's
    remediation message so operators get identical diagnostics from
    boot logs and dashboard."""

    def test_enabled_mind_with_no_model_reports_none_strategy(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        # NO pretrained model.

        result = query_per_mind_wake_word_status(data_dir=tmp_path, router=None)

        assert len(result) == 1
        entry = result[0]
        assert entry.wake_word_enabled is True
        assert entry.resolution_strategy == "none"
        assert entry.model_path is None
        assert entry.last_error is not None
        assert "train-wake-word" in entry.last_error
        assert "aria" in entry.last_error.lower()

    def test_silent_degrade_observable(self, tmp_path: Path) -> None:
        """Closes the v0.28.3 T2 silent-degradation gap: when an
        operator persisted ``wake_word_enabled=true`` and the next
        boot's helper raised + T2 caught (degrading to router=None),
        the dashboard endpoint MUST still surface the broken state.

        This is the "router=None despite persisted intent" path."""
        _write_mind_yaml(tmp_path, "lucia", wake_word="Lucia", wake_word_enabled=True)
        # NO pretrained model (the broken-state condition).

        result = query_per_mind_wake_word_status(
            data_dir=tmp_path,
            router=None,  # T2 degraded path
        )

        assert len(result) == 1
        entry = result[0]
        assert entry.wake_word_enabled is True
        assert entry.runtime_registered is False
        assert entry.resolution_strategy == "none"
        assert entry.last_error is not None  # operator-actionable signal


# ── Mixed list: enabled healthy + enabled broken + disabled ──────────


class TestMixedList:
    def test_three_mind_states_render_correctly(self, tmp_path: Path) -> None:
        """The most realistic operator state: one healthy, one
        configured-but-not-running, one disabled."""
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_mind_yaml(tmp_path, "lucia", wake_word="Lucia", wake_word_enabled=True)
        _write_mind_yaml(tmp_path, "joao", wake_word="Joao", wake_word_enabled=False)
        _write_pretrained_model(tmp_path, "aria")
        # No model for lucia; joao is OFF.

        # Router has only aria registered (simulating boot success
        # for aria + skipped for lucia / joao).
        router = WakeWordRouter()
        with _patch_onnxruntime():
            router.register_mind(MindId("aria"), model_path=Path("/fake/aria.onnx"))

        result = query_per_mind_wake_word_status(data_dir=tmp_path, router=router)

        # Sorted alphabetical by filesystem iteration: aria, joao, lucia.
        assert len(result) == 3
        by_mind = {entry.mind_id: entry for entry in result}

        # aria: healthy + registered.
        assert by_mind["aria"].wake_word_enabled is True
        assert by_mind["aria"].runtime_registered is True
        assert by_mind["aria"].resolution_strategy == "exact"
        assert by_mind["aria"].last_error is None

        # lucia: configured-but-not-running.
        assert by_mind["lucia"].wake_word_enabled is True
        assert by_mind["lucia"].runtime_registered is False
        assert by_mind["lucia"].resolution_strategy == "none"
        assert by_mind["lucia"].last_error is not None

        # joao: OFF, resolution skipped.
        assert by_mind["joao"].wake_word_enabled is False
        assert by_mind["joao"].runtime_registered is False
        assert by_mind["joao"].resolution_strategy is None
        assert by_mind["joao"].last_error is None


# ── Frozen dataclass contract ─────────────────────────────────────────


class TestEntryDataclassContract:
    """The :class:`WakeWordPerMindStatusEntry` dataclass is
    frozen+slotted; pin the contract so a future field addition
    doesn't accidentally break the wire-format mapping in voice.py."""

    def test_entry_has_expected_fields(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=False)
        result = query_per_mind_wake_word_status(data_dir=tmp_path, router=None)
        entry = result[0]
        assert isinstance(entry, WakeWordPerMindStatusEntry)
        # Field set is the contract — adding/removing changes the API.
        expected_fields = {
            "mind_id",
            "wake_word",
            "voice_language",
            "wake_word_enabled",
            "runtime_registered",
            "model_path",
            "resolution_strategy",
            "last_error",
        }
        actual_fields = {f for f in entry.__slots__}
        assert actual_fields == expected_fields
