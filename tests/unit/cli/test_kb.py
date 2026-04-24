"""Tests for ``sovyx kb`` — the mixer-profile contribution CLI.

Covers every subcommand — ``list``, ``inspect``, ``validate``,
``fixtures`` — across happy, ambiguous, and error paths. The tests
inject the shipped-profiles directory via ``patch.object`` so they
don't depend on (or mutate) the bundled ``_mixer_kb/profiles/``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sovyx.cli.commands import kb as kb_mod
from sovyx.cli.commands.kb import _missing_fixtures, kb_app

runner = CliRunner()


# Minimal schema-valid YAML — mirrors the test fixture at
# tests/unit/voice/health/test_mixer_kb.py:48, rewritten per-test to
# avoid cross-file coupling.
_GOOD_YAML = dedent("""
    schema_version: 1
    profile_id: vaio_vjfe69_sn6180
    profile_version: 1
    description: Sony VAIO FE-series with Conexant SN6180.

    codec_id_glob: "14F1:5045"
    driver_family: hda
    system_vendor_glob: "Sony*"
    system_product_glob: "VJFE69*"
    kernel_major_minor_glob: "6.*"
    audio_stack: pipewire
    match_threshold: 0.6

    factory_regime: attenuation
    factory_signature:
      capture_master:
        expected_fraction_range: [0.3, 0.6]
      internal_mic_boost:
        expected_raw_range: [0, 0]

    recommended_preset:
      controls:
        - role: capture_master
          value: {fraction: 1.0}
        - role: internal_mic_boost
          value: {raw: 0}
      auto_mute_mode: disabled
      runtime_pm_target: "on"

    validation:
      rms_dbfs_range: [-30, -15]
      peak_dbfs_max: -2
      snr_db_vocal_band_min: 15
      silero_prob_min: 0.5
      wake_word_stage2_prob_min: 0.4

    verified_on:
      - system_product: "VJFE69F11X-B0221H"
        codec_id: "14F1:5045"
        kernel: "6.14.0-37"
        distro: "linuxmint-22.2"
        verified_at: "2026-04-23"
        verified_by: "sovyx-core-pilot"

    contributed_by: sovyx-core
""").strip()


def _write_profile(
    dir_path: Path,
    profile_id: str,
    *,
    body: str | None = None,
) -> Path:
    """Write a profile YAML under ``dir_path``.

    Defaults to ``_GOOD_YAML`` rewritten so ``profile_id`` matches the
    filename stem (the loader enforces that invariant).
    """
    yaml_body = body if body is not None else _GOOD_YAML
    if body is None:
        yaml_body = yaml_body.replace(
            "profile_id: vaio_vjfe69_sn6180",
            f"profile_id: {profile_id}",
        )
    path = dir_path / f"{profile_id}.yaml"
    path.write_text(yaml_body, encoding="utf-8")
    return path


@pytest.fixture
def shipped_dir(tmp_path: Path) -> Path:
    """Fresh shipped-pool directory isolated per test."""
    d = tmp_path / "shipped"
    d.mkdir()
    return d


@pytest.fixture
def user_dir(tmp_path: Path) -> Path:
    """Fresh user-pool directory isolated per test."""
    d = tmp_path / "user"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# sovyx kb list
# ---------------------------------------------------------------------------


class TestKbList:
    def test_empty_prints_hint(
        self,
        shipped_dir: Path,
        user_dir: Path,
    ) -> None:
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                ["list", "--user-dir", str(user_dir)],
            )
        assert result.exit_code == 0
        assert "No mixer-KB profiles loaded" in result.stdout

    def test_lists_shipped_profile(
        self,
        shipped_dir: Path,
        user_dir: Path,
    ) -> None:
        _write_profile(shipped_dir, "vaio_a")
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                ["list", "--user-dir", str(user_dir)],
            )
        assert result.exit_code == 0
        assert "vaio_a" in result.stdout
        assert "1 shipped" in result.stdout
        assert "0 user" in result.stdout

    def test_lists_user_profile(
        self,
        shipped_dir: Path,
        user_dir: Path,
    ) -> None:
        # Shorter profile id so rich's table doesn't truncate it.
        _write_profile(user_dir, "xps9320")
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                ["list", "--user-dir", str(user_dir)],
            )
        assert result.exit_code == 0
        assert "xps9320" in result.stdout
        assert "1 user" in result.stdout

    def test_shipped_only_hides_user_pool(
        self,
        shipped_dir: Path,
        user_dir: Path,
    ) -> None:
        _write_profile(shipped_dir, "ship_one")
        _write_profile(user_dir, "user_one")
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                ["list", "--user-dir", str(user_dir), "--shipped-only"],
            )
        assert result.exit_code == 0
        assert "ship_one" in result.stdout
        assert "user_one" not in result.stdout


# ---------------------------------------------------------------------------
# sovyx kb inspect
# ---------------------------------------------------------------------------


class TestKbInspect:
    def test_inspect_shipped_profile(
        self,
        shipped_dir: Path,
        user_dir: Path,
    ) -> None:
        _write_profile(shipped_dir, "vaio_target")
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                ["inspect", "vaio_target", "--user-dir", str(user_dir)],
            )
        assert result.exit_code == 0
        assert "vaio_target" in result.stdout
        assert "shipped pool" in result.stdout
        assert "driver_family" in result.stdout

    def test_inspect_user_profile(
        self,
        shipped_dir: Path,
        user_dir: Path,
    ) -> None:
        _write_profile(user_dir, "community_device")
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                ["inspect", "community_device", "--user-dir", str(user_dir)],
            )
        assert result.exit_code == 0
        assert "community_device" in result.stdout
        assert "user pool" in result.stdout

    def test_inspect_missing_profile_fails(
        self,
        shipped_dir: Path,
        user_dir: Path,
    ) -> None:
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                ["inspect", "does_not_exist", "--user-dir", str(user_dir)],
            )
        assert result.exit_code == 1
        assert "No profile with id" in result.stdout


# ---------------------------------------------------------------------------
# sovyx kb validate
# ---------------------------------------------------------------------------


class TestKbValidate:
    def test_good_yaml_passes(self, tmp_path: Path) -> None:
        target = tmp_path / "vaio_valid.yaml"
        target.write_text(
            _GOOD_YAML.replace(
                "profile_id: vaio_vjfe69_sn6180",
                "profile_id: vaio_valid",
            ),
            encoding="utf-8",
        )
        result = runner.invoke(kb_app, ["validate", str(target)])
        assert result.exit_code == 0
        assert "OK" in result.stdout
        assert "vaio_valid" in result.stdout

    def test_missing_file_exits_usage_error(self, tmp_path: Path) -> None:
        target = tmp_path / "nope.yaml"
        result = runner.invoke(kb_app, ["validate", str(target)])
        assert result.exit_code == 2
        assert "File not found" in result.stdout

    def test_malformed_yaml_exits_validation_failed(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.yaml"
        target.write_text("this: is: not: yaml::\n", encoding="utf-8")
        result = runner.invoke(kb_app, ["validate", str(target)])
        assert result.exit_code == 1
        assert "malformed" in result.stdout.lower()

    def test_schema_violation_surfaces_field(self, tmp_path: Path) -> None:
        # Delete a required field — expect pydantic to flag it.
        broken = _GOOD_YAML.replace('codec_id_glob: "14F1:5045"\n', "")
        target = tmp_path / "vaio_broken.yaml"
        target.write_text(broken, encoding="utf-8")
        result = runner.invoke(kb_app, ["validate", str(target)])
        assert result.exit_code == 1
        assert "Schema validation failed" in result.stdout
        assert "codec_id_glob" in result.stdout

    def test_filename_id_mismatch_rejected(self, tmp_path: Path) -> None:
        # Filename and profile_id must agree; loader raises ValueError.
        target = tmp_path / "wrong_name.yaml"
        target.write_text(
            _GOOD_YAML.replace(
                "profile_id: vaio_vjfe69_sn6180",
                "profile_id: some_other_id",
            ),
            encoding="utf-8",
        )
        result = runner.invoke(kb_app, ["validate", str(target)])
        assert result.exit_code == 1
        # Expect the loader's filename-stem message to surface.
        assert "disagrees" in result.stdout or "filename" in result.stdout.lower()

    def test_directory_path_exits_usage_error(self, tmp_path: Path) -> None:
        # Typer's built-in path validation ("dir_okay=False") intercepts
        # directories and exits with 2 before our custom handler runs;
        # either path is acceptable — we assert on the exit code only.
        result = runner.invoke(kb_app, ["validate", str(tmp_path)])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# sovyx kb fixtures
# ---------------------------------------------------------------------------


class TestKbFixtures:
    def _make_fixtures(self, root: Path, profile_id: str) -> None:
        (root / f"{profile_id}_before.txt").write_text("before", encoding="utf-8")
        (root / f"{profile_id}_after.txt").write_text("after", encoding="utf-8")
        (root / f"{profile_id}_capture.wav").write_bytes(b"\x00" * 16)

    def test_missing_fixtures_root_exits_usage_error(
        self,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            kb_app,
            [
                "fixtures",
                "any_id",
                "--fixtures-root",
                str(tmp_path / "missing"),
            ],
        )
        assert result.exit_code == 2

    def test_profile_not_in_shipped_pool_exits_usage_error(
        self,
        shipped_dir: Path,
        tmp_path: Path,
    ) -> None:
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                [
                    "fixtures",
                    "not_shipped",
                    "--fixtures-root",
                    str(fixtures),
                ],
            )
        assert result.exit_code == 2
        assert "not found" in result.stdout

    def test_single_profile_all_fixtures_present(
        self,
        shipped_dir: Path,
        tmp_path: Path,
    ) -> None:
        _write_profile(shipped_dir, "vaio_ok")
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        self._make_fixtures(fixtures, "vaio_ok")
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                [
                    "fixtures",
                    "vaio_ok",
                    "--fixtures-root",
                    str(fixtures),
                ],
            )
        assert result.exit_code == 0
        assert "OK" in result.stdout

    def test_single_profile_missing_fixture_exits_validation_failed(
        self,
        shipped_dir: Path,
        tmp_path: Path,
    ) -> None:
        _write_profile(shipped_dir, "vaio_missing")
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        # Only write _before — the rest are missing.
        (fixtures / "vaio_missing_before.txt").write_text("x", encoding="utf-8")
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                [
                    "fixtures",
                    "vaio_missing",
                    "--fixtures-root",
                    str(fixtures),
                ],
            )
        assert result.exit_code == 1
        assert "MISSING" in result.stdout
        assert "_after.txt" in result.stdout
        assert "_capture.wav" in result.stdout

    def test_all_profiles_iteration(
        self,
        shipped_dir: Path,
        tmp_path: Path,
    ) -> None:
        _write_profile(shipped_dir, "vaio_a")
        _write_profile(shipped_dir, "vaio_b")
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        self._make_fixtures(fixtures, "vaio_a")
        self._make_fixtures(fixtures, "vaio_b")
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                [
                    "fixtures",
                    "all",
                    "--fixtures-root",
                    str(fixtures),
                ],
            )
        assert result.exit_code == 0
        assert "vaio_a" in result.stdout
        assert "vaio_b" in result.stdout
        assert "2 profiles fixture-complete" in result.stdout

    def test_all_with_empty_shipped_pool_succeeds(
        self,
        shipped_dir: Path,
        tmp_path: Path,
    ) -> None:
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        with patch.object(kb_mod, "_SHIPPED_PROFILES_DIR", shipped_dir):
            result = runner.invoke(
                kb_app,
                [
                    "fixtures",
                    "all",
                    "--fixtures-root",
                    str(fixtures),
                ],
            )
        assert result.exit_code == 0
        assert "No shipped profiles" in result.stdout


# ---------------------------------------------------------------------------
# Pure helper — `_missing_fixtures` is reused by the CI shim test.
# ---------------------------------------------------------------------------


class TestMissingFixturesHelper:
    def test_returns_empty_list_when_all_present(self, tmp_path: Path) -> None:
        (tmp_path / "p1_before.txt").write_text("x", encoding="utf-8")
        (tmp_path / "p1_after.txt").write_text("x", encoding="utf-8")
        (tmp_path / "p1_capture.wav").write_bytes(b"\x00")
        assert _missing_fixtures("p1", tmp_path) == []

    def test_returns_suffixes_in_order(self, tmp_path: Path) -> None:
        # Only _capture.wav present.
        (tmp_path / "p1_capture.wav").write_bytes(b"\x00")
        missing = _missing_fixtures("p1", tmp_path)
        assert missing == ["p1_before.txt", "p1_after.txt"]

    def test_reports_all_three_missing(self, tmp_path: Path) -> None:
        missing = _missing_fixtures("p1", tmp_path)
        assert missing == ["p1_before.txt", "p1_after.txt", "p1_capture.wav"]
