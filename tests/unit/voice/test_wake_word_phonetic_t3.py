"""T3 mission tests — phonetic matcher auto-detect + kill-switch.

Mission: ``MISSION-pre-wake-word-ui-hardening-2026-05-03.md`` §T3 (D3).

Verifies:

* When ``phonetic_fallback_enabled=True`` (default) AND espeak-ng is
  available on PATH, the resolver gets a working PhoneticMatcher.
* When ``phonetic_fallback_enabled=True`` AND espeak-ng is absent, the
  matcher's ``is_available=False`` degrades gracefully to EXACT-only
  (bit-exact match v0.28.2 hardcoded-None contract on Windows hosts
  without espeak-ng).
* When ``phonetic_fallback_enabled=False``, the matcher is NOT
  constructed at all — operator's explicit kill-switch wins.
* The per-mind ``voice_language`` is threaded through to the matcher
  (different minds can have different espeak-ng languages).

Symmetry contract (T3 explicit decision): ``resolve_wake_word_model_for_mind``
(dashboard hot-apply path) and ``build_wake_word_router_for_enabled_minds``
(boot path) MUST behave identically for the same inputs. Asymmetry
between the two surfaces is operator-visible drift.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from sovyx.engine.errors import VoiceError
from sovyx.voice._phonetic_matcher import PhoneticMatcher
from sovyx.voice.factory._wake_word_wire_up import (
    build_wake_word_router_for_enabled_minds,
    resolve_wake_word_model_for_mind,
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


# ── Kill-switch contract ─────────────────────────────────────────────


class TestKillSwitchOff:
    """``phonetic_fallback_enabled=False`` → matcher NOT constructed."""

    def test_resolve_for_mind_skips_matcher_when_disabled(self, tmp_path: Path) -> None:
        """Operator explicitly disables phonetic fallback. EXACT match
        still works; non-EXACT raises VoiceError."""
        _write_pretrained_model(tmp_path, "aria")

        with patch("sovyx.voice.factory._wake_word_wire_up.PhoneticMatcher") as mock_matcher:
            path = resolve_wake_word_model_for_mind(
                data_dir=tmp_path,
                wake_word="Aria",
                phonetic_fallback_enabled=False,
            )
            # Matcher constructor MUST NOT have been called.
            mock_matcher.assert_not_called()
            assert path.name == "aria.onnx"

    def test_build_for_minds_skips_matcher_when_disabled(self, tmp_path: Path) -> None:
        """Symmetric counterpart for the boot-time builder."""
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_pretrained_model(tmp_path, "aria")

        with (
            _patch_onnxruntime(),
            patch("sovyx.voice.factory._wake_word_wire_up.PhoneticMatcher") as mock_matcher,
        ):
            router = build_wake_word_router_for_enabled_minds(
                data_dir=tmp_path,
                phonetic_fallback_enabled=False,
            )
            mock_matcher.assert_not_called()
        assert router is not None
        assert "aria" in router.registered_minds


# ── Auto-detect contract ─────────────────────────────────────────────


class TestAutoDetect:
    """Default ``phonetic_fallback_enabled=True`` constructs the
    matcher; espeak-ng auto-detect inside ``PhoneticMatcher.__init__``
    handles the espeak-ng-absent case without raising."""

    def test_resolve_for_mind_constructs_matcher_by_default(self, tmp_path: Path) -> None:
        _write_pretrained_model(tmp_path, "aria")

        with patch("sovyx.voice.factory._wake_word_wire_up.PhoneticMatcher") as mock_matcher:
            mock_matcher.return_value = MagicMock(spec=PhoneticMatcher)
            mock_matcher.return_value.is_available = True
            resolve_wake_word_model_for_mind(
                data_dir=tmp_path,
                wake_word="Aria",
                voice_language="pt-BR",
            )
            # Matcher constructed once with the per-mind language.
            mock_matcher.assert_called_once()
            kwargs = mock_matcher.call_args.kwargs
            assert kwargs["language"] == "pt-BR"
            assert kwargs["enabled"] is None  # auto-detect semantics

    def test_build_for_minds_threads_voice_language_per_mind(self, tmp_path: Path) -> None:
        """Each mind's ``voice_language`` field is threaded into its
        own PhoneticMatcher — espeak-ng phonemes are language-specific."""
        _write_mind_yaml(
            tmp_path,
            "aria",
            wake_word="Aria",
            wake_word_enabled=True,
            voice_language="en-US",
        )
        _write_mind_yaml(
            tmp_path,
            "lucia",
            wake_word="Lucia",
            wake_word_enabled=True,
            voice_language="pt-BR",
        )
        _write_pretrained_model(tmp_path, "aria")
        _write_pretrained_model(tmp_path, "lucia")

        with (
            _patch_onnxruntime(),
            patch("sovyx.voice.factory._wake_word_wire_up.PhoneticMatcher") as mock_matcher,
        ):
            mock_matcher.return_value = MagicMock(spec=PhoneticMatcher)
            mock_matcher.return_value.is_available = True
            build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        # Two minds → two matcher constructions, each with its own language.
        assert mock_matcher.call_count == 2
        languages = {call.kwargs["language"] for call in mock_matcher.call_args_list}
        assert languages == {"en-US", "pt-BR"}


# ── Espeak-ng absent (auto-detect graceful degrade) ──────────────────


class TestEspeakNgAbsent:
    """When ``shutil.which('espeak-ng')`` returns None,
    ``PhoneticMatcher.__init__(enabled=None)`` sets ``is_available=False``
    without raising. The resolver then degrades to EXACT-only — bit-
    exact match v0.28.2 hardcoded-None contract."""

    def test_resolve_with_no_espeak_ng_returns_exact_match(self, tmp_path: Path) -> None:
        _write_pretrained_model(tmp_path, "aria")

        with patch("sovyx.voice._phonetic_matcher.shutil.which", return_value=None):
            # Real PhoneticMatcher path — auto-detects absent espeak-ng.
            path = resolve_wake_word_model_for_mind(
                data_dir=tmp_path,
                wake_word="Aria",  # exact filename match
            )
        assert path.name == "aria.onnx"

    def test_resolve_with_no_espeak_ng_raises_for_diacritic(self, tmp_path: Path) -> None:
        """Without espeak-ng, "Lúcia" doesn't phonetic-match "lucia.onnx" —
        falls through to EXACT-only and raises NONE."""
        _write_pretrained_model(tmp_path, "lucia")

        with (
            patch("sovyx.voice._phonetic_matcher.shutil.which", return_value=None),
            pytest.raises(VoiceError, match="train-wake-word"),
        ):
            # Note: WakeWordModelResolver normalizes via ASCII-fold,
            # so "Lúcia" actually matches "lucia.onnx" via EXACT after
            # normalisation. This test uses a wake word that does NOT
            # ASCII-fold to a registry name.
            resolve_wake_word_model_for_mind(
                data_dir=tmp_path,
                wake_word="Joaquín",  # no joaquin.onnx exists
            )
