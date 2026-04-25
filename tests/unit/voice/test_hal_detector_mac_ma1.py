"""Tests for the MA1 macOS HAL plugin detector.

Mocks Path/iterdir + sys.platform so the suite stays cross-platform.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice._hal_detector_mac import (
    HalPluginCategory,
    HalPluginEntry,
    HalReport,
    detect_hal_plugins,
)

# ── Cross-platform branch ────────────────────────────────────────


class TestNonDarwinShortCircuit:
    def test_linux_returns_empty_with_note(self) -> None:
        with patch.object(sys, "platform", "linux"):
            report = detect_hal_plugins()
        assert report.plugins == ()
        assert any("non-darwin" in n for n in report.notes)

    def test_windows_returns_empty_with_note(self) -> None:
        with patch.object(sys, "platform", "win32"):
            report = detect_hal_plugins()
        assert report.plugins == ()


# ── Filesystem enumeration (mocked) ──────────────────────────────


def _mock_path_with_drivers(*driver_names: str, exists: bool = True) -> MagicMock:
    """Build a MagicMock that mimics a Path with iterdir() returning
    the given .driver bundles."""
    path = MagicMock()
    path.exists.return_value = exists

    if not exists:
        path.iterdir.side_effect = FileNotFoundError("missing")
        return path

    children = []
    for name in driver_names:
        child = MagicMock()
        child.suffix = ".driver"
        child.stem = name
        child.__str__.return_value = f"/Library/Audio/Plug-Ins/HAL/{name}.driver"
        children.append(child)
    # Also add a non-driver child to verify filtering works.
    other = MagicMock()
    other.suffix = ".bundle"
    other.stem = "OtherBundle"
    children.append(other)
    path.iterdir.return_value = iter(children)
    return path


class TestDirectoryScan:
    def test_no_directories_present_returns_empty(self) -> None:
        # Both system + user dirs missing → empty report with notes.
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                _mock_path_with_drivers(exists=False),
            ),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        assert report.plugins == ()
        assert len(report.notes) == 2  # noqa: PLR2004

    def test_blackhole_classified_as_virtual_audio(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                _mock_path_with_drivers("BlackHole2ch"),
            ),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        assert len(report.plugins) == 1
        plugin = report.plugins[0]
        assert plugin.bundle_name == "BlackHole2ch"
        assert plugin.category is HalPluginCategory.VIRTUAL_AUDIO
        assert plugin.friendly_label == "BlackHole (virtual audio bus)"
        assert report.virtual_audio_active is True

    def test_loopback_classified_as_virtual_audio(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                _mock_path_with_drivers("Loopback"),
            ),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        assert report.plugins[0].category is HalPluginCategory.VIRTUAL_AUDIO

    def test_audio_hijack_classified_as_enhancement(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                _mock_path_with_drivers("AudioHijack"),
            ),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        assert report.plugins[0].category is HalPluginCategory.AUDIO_ENHANCEMENT
        assert report.audio_enhancement_active is True

    def test_unknown_plugin_classified_as_unknown(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                _mock_path_with_drivers("RandomVendorThing"),
            ),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        plugin = report.plugins[0]
        assert plugin.category is HalPluginCategory.UNKNOWN
        # Friendly label is empty for UNKNOWN — caller renders raw bundle_name.
        assert plugin.friendly_label == ""
        assert plugin.bundle_name == "RandomVendorThing"

    def test_mixed_categories_in_one_directory(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                _mock_path_with_drivers(
                    "BlackHole16ch",
                    "Loopback",
                    "SoundSource",
                    "MysteryDriver",
                ),
            ),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        assert len(report.plugins) == 4  # noqa: PLR2004
        # by_category groups correctly.
        groups = report.by_category
        assert "virtual_audio" in groups
        assert "audio_enhancement" in groups
        assert "unknown" in groups
        assert len(groups["virtual_audio"]) == 2  # BlackHole + Loopback # noqa: PLR2004

    def test_results_sorted_alphabetically(self) -> None:
        # Stability across runs — diff-based regression depends on
        # this. Filesystem iterdir order is platform-dependent.
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                _mock_path_with_drivers("ZebraDriver", "AlphaDriver", "MikeDriver"),
            ),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        names = [p.bundle_name for p in report.plugins]
        assert names == ["AlphaDriver", "MikeDriver", "ZebraDriver"]

    def test_user_and_system_directories_combined(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                _mock_path_with_drivers("BlackHole2ch"),
            ),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers("Loopback"),
            ),
        ):
            report = detect_hal_plugins()
        assert len(report.plugins) == 2  # noqa: PLR2004
        assert {p.bundle_name for p in report.plugins} == {"BlackHole2ch", "Loopback"}

    def test_permission_denied_returns_empty_with_note(self) -> None:
        bad_path = MagicMock()
        bad_path.exists.return_value = True
        bad_path.iterdir.side_effect = PermissionError("denied")
        bad_path.__str__.return_value = "/Library/Audio/Plug-Ins/HAL"
        with (
            patch.object(sys, "platform", "darwin"),
            patch("sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR", bad_path),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        assert report.plugins == ()
        assert any("permission denied" in n for n in report.notes)

    def test_oserror_returns_empty_with_note(self) -> None:
        bad_path = MagicMock()
        bad_path.exists.return_value = True
        bad_path.iterdir.side_effect = OSError("bad io")
        bad_path.__str__.return_value = "/Library/Audio/Plug-Ins/HAL"
        with (
            patch.object(sys, "platform", "darwin"),
            patch("sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR", bad_path),
            patch(
                "sovyx.voice._hal_detector_mac._user_hal_dir",
                return_value=_mock_path_with_drivers(exists=False),
            ),
        ):
            report = detect_hal_plugins()
        assert report.plugins == ()
        assert any("read failed" in n for n in report.notes)


# ── Substring matching robustness ────────────────────────────────


class TestClassificationRobustness:
    def test_blackhole_variants_all_classify_correctly(self) -> None:
        for variant in ("BlackHole2ch", "BlackHole16ch", "BlackHole-64ch", "blackhole_test"):
            with (
                patch.object(sys, "platform", "darwin"),
                patch(
                    "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                    _mock_path_with_drivers(variant),
                ),
                patch(
                    "sovyx.voice._hal_detector_mac._user_hal_dir",
                    return_value=_mock_path_with_drivers(exists=False),
                ),
            ):
                report = detect_hal_plugins()
            assert report.plugins[0].category is HalPluginCategory.VIRTUAL_AUDIO, (
                f"variant {variant} should classify as VIRTUAL_AUDIO"
            )

    def test_case_insensitive_matching(self) -> None:
        for variant in ("BLACKHOLE", "BlackHole", "blackhole"):
            with (
                patch.object(sys, "platform", "darwin"),
                patch(
                    "sovyx.voice._hal_detector_mac._SYSTEM_HAL_DIR",
                    _mock_path_with_drivers(variant),
                ),
                patch(
                    "sovyx.voice._hal_detector_mac._user_hal_dir",
                    return_value=_mock_path_with_drivers(exists=False),
                ),
            ):
                report = detect_hal_plugins()
            assert report.plugins[0].category is HalPluginCategory.VIRTUAL_AUDIO


# ── Report contract ──────────────────────────────────────────────


class TestReportContract:
    def test_category_enum_values_stable(self) -> None:
        # Dashboards key on these strings.
        assert HalPluginCategory.VIRTUAL_AUDIO.value == "virtual_audio"
        assert HalPluginCategory.AUDIO_ENHANCEMENT.value == "audio_enhancement"
        assert HalPluginCategory.VENDOR.value == "vendor"
        assert HalPluginCategory.UNKNOWN.value == "unknown"

    def test_empty_report_safe_defaults(self) -> None:
        report = HalReport()
        assert report.plugins == ()
        assert report.notes == ()
        assert report.virtual_audio_active is False
        assert report.audio_enhancement_active is False
        assert report.by_category == {}

    def test_plugin_entry_has_all_fields(self) -> None:
        entry = HalPluginEntry(
            bundle_name="Test",
            path="/path/Test.driver",
            category=HalPluginCategory.VIRTUAL_AUDIO,
            friendly_label="Test (label)",
        )
        assert entry.bundle_name == "Test"
        assert entry.friendly_label == "Test (label)"


pytestmark = pytest.mark.timeout(10)
