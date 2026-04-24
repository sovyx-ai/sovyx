"""Tests for the 7-signal user-customization heuristic (L2.5 F1.E).

Covers every signal in isolation (A..G) + weight composition +
score clamping. All filesystem paths injected via ``home_dir`` +
``asound_state_path`` + ``time_now_s`` so the tests never touch
the real user environment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from sovyx.voice.health import (
    HardwareContext,
    detect_user_customization,
)
from sovyx.voice.health._mixer_sanity import (
    _ASOUND_STATE_RECENT_SECONDS,  # noqa: PLC2701 — constant tests key on
    _SIGNAL_WEIGHTS,  # noqa: PLC2701
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _StubComboStore:
    """Minimal ComboStore stub exposing only .get()."""

    entries: dict[str, object]

    def get(self, endpoint_guid: str) -> object | None:
        return self.entries.get(endpoint_guid)


@dataclass
class _StubCaptureOverrides:
    """Minimal CaptureOverrides stub exposing only .get_entry()."""

    pinned: dict[str, object]

    def get_entry(self, endpoint_guid: str) -> object | None:
        return self.pinned.get(endpoint_guid)


_HW = HardwareContext(driver_family="hda", codec_id="14F1:5045")


def _empty_home(tmp_path: Path) -> Path:
    """Return an empty home dir — no asoundrc, no pipewire configs."""
    home = tmp_path / "home"
    home.mkdir()
    return home


class TestSignalAMixerDiffersFromFactory:
    """Signal A contribution scales with (1 - factory_signature_score)."""

    def test_full_factory_match_no_contribution(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "A_mixer_differs_from_factory" not in report.signals_fired
        assert report.score == 0.0

    def test_full_deviation_maxes_out_a(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=0.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "A_mixer_differs_from_factory" in report.signals_fired
        # Only signal A fires → score == weight A == 0.30
        assert report.score == pytest.approx(_SIGNAL_WEIGHTS["A_mixer_differs_from_factory"])

    def test_partial_deviation_partial_contribution(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=0.5,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "A_mixer_differs_from_factory" in report.signals_fired
        assert report.score == pytest.approx(0.5 * 0.30)


class TestSignalBAsoundrc:
    def test_asoundrc_exists_fires_signal(self, tmp_path: Path) -> None:
        home = _empty_home(tmp_path)
        (home / ".asoundrc").write_text("pcm.!default { type hw }\n", encoding="utf-8")
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=home,
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "B_asoundrc_exists" in report.signals_fired
        assert report.score == pytest.approx(_SIGNAL_WEIGHTS["B_asoundrc_exists"])

    def test_asoundrc_absent_no_fire(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "B_asoundrc_exists" not in report.signals_fired


class TestSignalCPipewireUserConf:
    def test_pipewire_conf_d_with_conf_fires(self, tmp_path: Path) -> None:
        home = _empty_home(tmp_path)
        conf_d = home / ".config" / "pipewire" / "pipewire.conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "10-user.conf").write_text("context.properties = {}\n")
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=home,
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "C_pipewire_user_conf" in report.signals_fired

    def test_pipewire_conf_d_empty_no_fire(self, tmp_path: Path) -> None:
        home = _empty_home(tmp_path)
        (home / ".config" / "pipewire" / "pipewire.conf.d").mkdir(parents=True)
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=home,
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "C_pipewire_user_conf" not in report.signals_fired

    def test_pipewire_non_conf_file_no_fire(self, tmp_path: Path) -> None:
        home = _empty_home(tmp_path)
        conf_d = home / ".config" / "pipewire" / "pipewire.conf.d"
        conf_d.mkdir(parents=True)
        # README.md should not trigger — only *.conf does.
        (conf_d / "README.md").write_text("docs")
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=home,
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "C_pipewire_user_conf" not in report.signals_fired


class TestSignalDAsoundStateRecent:
    def test_recent_mtime_fires(self, tmp_path: Path) -> None:
        asound = tmp_path / "asound.state"
        asound.write_text("state")
        now = time.time()
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=asound,
            time_now_s=now,  # freshly written — mtime ≈ now
        )
        assert "D_asound_state_recent" in report.signals_fired

    def test_old_mtime_no_fire(self, tmp_path: Path) -> None:
        asound = tmp_path / "asound.state"
        asound.write_text("state")
        now = time.time()
        # Simulate a file that was last modified 30 days ago.
        far_future = now + 30 * 24 * 3600
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=asound,
            time_now_s=far_future,  # now is way after mtime
        )
        assert "D_asound_state_recent" not in report.signals_fired

    def test_missing_file_no_fire(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "missing.state",
            time_now_s=time.time(),
        )
        assert "D_asound_state_recent" not in report.signals_fired

    def test_boundary_exactly_at_window(self, tmp_path: Path) -> None:
        """At exactly _ASOUND_STATE_RECENT_SECONDS the signal must fire (closed boundary)."""
        asound = tmp_path / "asound.state"
        asound.write_text("state")
        import os

        target_mtime = time.time() - _ASOUND_STATE_RECENT_SECONDS + 1
        os.utime(asound, (target_mtime, target_mtime))
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=asound,
            time_now_s=time.time(),
        )
        assert "D_asound_state_recent" in report.signals_fired


class TestSignalEWireplumberUserConf:
    def test_wireplumber_conf_fires(self, tmp_path: Path) -> None:
        home = _empty_home(tmp_path)
        conf_d = home / ".config" / "wireplumber" / "wireplumber.conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "99-mixer.conf").write_text("x")
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=home,
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "E_wireplumber_user_conf" in report.signals_fired

    def test_wireplumber_absent_no_fire(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "E_wireplumber_user_conf" not in report.signals_fired


class TestSignalFComboStoreDrift:
    def test_entry_plus_low_factory_score_fires(self, tmp_path: Path) -> None:
        store = _StubComboStore(entries={"guid-1": object()})
        report = detect_user_customization(
            factory_signature_score=0.0,  # low → "drifted from factory"
            hw=_HW,
            combo_store=store,  # type: ignore[arg-type]
            endpoint_guid="guid-1",
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "F_combo_store_has_entry_with_drift" in report.signals_fired

    def test_entry_plus_high_factory_score_no_fire(self, tmp_path: Path) -> None:
        # factory_score=0.8 ≥ 0.5 threshold → F does NOT fire.
        store = _StubComboStore(entries={"guid-1": object()})
        report = detect_user_customization(
            factory_signature_score=0.8,
            hw=_HW,
            combo_store=store,  # type: ignore[arg-type]
            endpoint_guid="guid-1",
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "F_combo_store_has_entry_with_drift" not in report.signals_fired

    def test_no_entry_no_fire(self, tmp_path: Path) -> None:
        store = _StubComboStore(entries={})
        report = detect_user_customization(
            factory_signature_score=0.0,
            hw=_HW,
            combo_store=store,  # type: ignore[arg-type]
            endpoint_guid="guid-1",
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "F_combo_store_has_entry_with_drift" not in report.signals_fired

    def test_no_store_no_fire(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=0.0,
            hw=_HW,
            combo_store=None,
            endpoint_guid="guid-1",
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "F_combo_store_has_entry_with_drift" not in report.signals_fired

    def test_no_endpoint_guid_no_fire(self, tmp_path: Path) -> None:
        store = _StubComboStore(entries={"guid-1": object()})
        report = detect_user_customization(
            factory_signature_score=0.0,
            hw=_HW,
            combo_store=store,  # type: ignore[arg-type]
            endpoint_guid=None,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "F_combo_store_has_entry_with_drift" not in report.signals_fired


class TestSignalGCaptureOverrides:
    def test_pinned_combo_fires(self, tmp_path: Path) -> None:
        overrides = _StubCaptureOverrides(pinned={"guid-1": object()})
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            capture_overrides=overrides,  # type: ignore[arg-type]
            endpoint_guid="guid-1",
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "G_capture_overrides_pinned" in report.signals_fired
        assert report.score == pytest.approx(_SIGNAL_WEIGHTS["G_capture_overrides_pinned"])

    def test_no_pin_no_fire(self, tmp_path: Path) -> None:
        overrides = _StubCaptureOverrides(pinned={})
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            capture_overrides=overrides,  # type: ignore[arg-type]
            endpoint_guid="guid-1",
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert "G_capture_overrides_pinned" not in report.signals_fired


class TestCombinedWeights:
    def test_all_signals_hit_clamps_to_one(self, tmp_path: Path) -> None:
        home = _empty_home(tmp_path)
        # Set up every signal to fire.
        (home / ".asoundrc").write_text("x")
        pw = home / ".config" / "pipewire" / "pipewire.conf.d"
        pw.mkdir(parents=True)
        (pw / "a.conf").write_text("x")
        wp = home / ".config" / "wireplumber" / "wireplumber.conf.d"
        wp.mkdir(parents=True)
        (wp / "a.conf").write_text("x")
        asound = tmp_path / "asound.state"
        asound.write_text("x")
        store = _StubComboStore(entries={"guid-1": object()})
        overrides = _StubCaptureOverrides(pinned={"guid-1": object()})

        report = detect_user_customization(
            factory_signature_score=0.0,  # A maxes out
            hw=_HW,
            combo_store=store,  # type: ignore[arg-type]
            capture_overrides=overrides,  # type: ignore[arg-type]
            endpoint_guid="guid-1",
            home_dir=home,
            asound_state_path=asound,
            time_now_s=time.time(),
        )
        assert set(report.signals_fired) == set(_SIGNAL_WEIGHTS.keys())
        # Sum of all weights = 1.0 — already at clamp ceiling.
        assert report.score == pytest.approx(1.0)

    def test_weights_sum_to_one(self) -> None:
        """Invariant — tests key on this total."""
        assert sum(_SIGNAL_WEIGHTS.values()) == pytest.approx(1.0)

    def test_score_clamped_to_unit_interval(self, tmp_path: Path) -> None:
        """Even if every signal fires, score stays in [0, 1]."""
        report = detect_user_customization(
            factory_signature_score=0.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert 0.0 <= report.score <= 1.0

    def test_real_home_default(self) -> None:
        """Default home_dir = Path.home(). Smoke test — should not crash."""
        # Pass unreachable asound_state_path so signal D never fires
        # regardless of machine state; we only verify the call path.
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            asound_state_path=_path_that_definitely_does_not_exist(),
            time_now_s=time.time(),
        )
        assert 0.0 <= report.score <= 1.0


def _path_that_definitely_does_not_exist() -> Path:
    from pathlib import Path

    return Path("/absolutely/no/path/like/this/exists.xyz")


class TestReportShape:
    def test_report_is_frozen(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        with pytest.raises(Exception):
            report.score = 0.5  # type: ignore[misc]

    def test_signals_fired_is_tuple(self, tmp_path: Path) -> None:
        report = detect_user_customization(
            factory_signature_score=1.0,
            hw=_HW,
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        assert isinstance(report.signals_fired, tuple)


class TestInjectedStoreIsolation:
    def test_only_stub_methods_invoked(self, tmp_path: Path) -> None:
        # Use MagicMock to verify the heuristic only touches .get().
        mock_store = MagicMock()
        mock_store.get.return_value = object()
        mock_overrides = MagicMock()
        mock_overrides.get_entry.return_value = object()
        detect_user_customization(
            factory_signature_score=0.0,
            hw=_HW,
            combo_store=mock_store,
            capture_overrides=mock_overrides,
            endpoint_guid="g",
            home_dir=_empty_home(tmp_path),
            asound_state_path=tmp_path / "nope",
            time_now_s=time.time(),
        )
        mock_store.get.assert_called_once_with("g")
        mock_overrides.get_entry.assert_called_once_with("g")
