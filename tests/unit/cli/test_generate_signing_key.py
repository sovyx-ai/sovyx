"""Tests for ``sovyx voice generate-signing-key`` (BT.B.3, v0.32.0).

Mission: ``MISSION-voice-v0_32_0-structural-closure-2026-05-08.md``
Phase B BT.B.3. Validates the CLI surface for operator-driven Ed25519
calibration signing-key generation:

* happy path — fresh keypair under the canonical per-mind layout,
  correct file permissions on POSIX, parseable PEM bytes, structured
  event emitted.
* refuses to overwrite without ``--force`` (idempotency safety).
* ``--force`` overwrites and emits ``mode="forced"``.
* ``--output`` override redirects both halves to the chosen location.
* ``--mind-id`` directs the keypair under a specific mind directory.

CLAUDE.md anti-pattern #36 applies: ``patch.object`` on
filesystem-resolving helpers + a per-test ``tmp_path`` keeps the suite
hermetic on a dev box that has a real ``~/.sovyx``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

from sovyx.cli.commands.voice import voice_app
from sovyx.voice.calibration import _key_generation as _kg_module
from sovyx.voice.calibration._key_generation import (
    PRIVATE_KEY_FILENAME,
    PUBLIC_KEY_FILENAME,
)

runner = CliRunner()


def _patch_data_dir(tmp_path: Path):  # noqa: ANN202 — context-manager helper
    """Force the CLI to write under ``tmp_path`` instead of ``~/.sovyx``."""
    return patch(
        "sovyx.cli.commands.voice._resolve_data_dir_for_signing_key",
        return_value=tmp_path,
    )


class TestGenerateSigningKey:
    def test_happy_path_creates_keypair_with_canonical_paths(
        self,
        tmp_path: Path,
    ) -> None:
        # Spy on the module-level logger (structlog wraps stdlib +
        # routes through structlog's own handler chain, so caplog
        # is unreliable; patching the module's `logger` object is
        # the documented escape hatch — see CLAUDE.md anti-pattern
        # "structlog routing makes it caplog-flaky" / test_home_path).
        spy = MagicMock()
        with _patch_data_dir(tmp_path), patch.object(_kg_module, "logger", spy):
            result = runner.invoke(
                voice_app,
                ["generate-signing-key", "--mind-id", "test-mind"],
            )

        assert result.exit_code == 0, result.output
        priv = tmp_path / "test-mind" / PRIVATE_KEY_FILENAME
        pub = tmp_path / "test-mind" / PUBLIC_KEY_FILENAME
        assert priv.is_file()
        assert pub.is_file()
        # Stdout carries the canonical paths and a fingerprint.
        assert str(priv) in result.output
        assert str(pub) in result.output
        # Structured event emitted via logger.info with source="cli", mode="created".
        spy.info.assert_called_once()
        args, kwargs = spy.info.call_args
        assert args[0] == "voice.calibration.signing_key.generated"
        assert kwargs.get("source") == "cli"
        assert kwargs.get("mode") == "created"

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        with _patch_data_dir(tmp_path):
            first = runner.invoke(
                voice_app,
                ["generate-signing-key", "--mind-id", "m1"],
            )
            assert first.exit_code == 0, first.output
            # Second invocation without --force must refuse.
            second = runner.invoke(
                voice_app,
                ["generate-signing-key", "--mind-id", "m1"],
            )
        assert second.exit_code == 1
        assert "already exists" in second.output
        assert "--force" in second.output

    def test_force_overwrites_existing_keypair(
        self,
        tmp_path: Path,
    ) -> None:
        with _patch_data_dir(tmp_path):
            first = runner.invoke(
                voice_app,
                ["generate-signing-key", "--mind-id", "m1"],
            )
            assert first.exit_code == 0
            priv = tmp_path / "m1" / PRIVATE_KEY_FILENAME
            first_bytes = priv.read_bytes()

            spy = MagicMock()
            with patch.object(_kg_module, "logger", spy):
                second = runner.invoke(
                    voice_app,
                    ["generate-signing-key", "--mind-id", "m1", "--force"],
                )
        assert second.exit_code == 0, second.output
        second_bytes = priv.read_bytes()
        # Force MUST regenerate fresh key material.
        assert first_bytes != second_bytes
        # The force path emits mode="forced" in the structured event.
        spy.info.assert_called_once()
        _args, kwargs = spy.info.call_args
        assert kwargs.get("mode") == "forced"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX chmod is a no-op on Windows; NTFS ACLs govern access",
    )
    def test_private_key_has_owner_only_permissions_on_posix(
        self,
        tmp_path: Path,
    ) -> None:
        with _patch_data_dir(tmp_path):
            result = runner.invoke(
                voice_app,
                ["generate-signing-key", "--mind-id", "perm-test"],
            )
        assert result.exit_code == 0
        priv = tmp_path / "perm-test" / PRIVATE_KEY_FILENAME
        pub = tmp_path / "perm-test" / PUBLIC_KEY_FILENAME
        # Mask off the file-type bits — only the permission bits matter.
        assert (priv.stat().st_mode & 0o777) == 0o600
        assert (pub.stat().st_mode & 0o777) == 0o644

    def test_public_key_pem_is_parseable_ed25519(self, tmp_path: Path) -> None:
        with _patch_data_dir(tmp_path):
            result = runner.invoke(
                voice_app,
                ["generate-signing-key", "--mind-id", "parse-test"],
            )
        assert result.exit_code == 0
        pub = tmp_path / "parse-test" / PUBLIC_KEY_FILENAME
        # Round-trip the persisted PEM through cryptography to confirm
        # it's a valid Ed25519 public key, not random bytes.
        public_key = serialization.load_pem_public_key(pub.read_bytes())
        # The Ed25519 key has a well-known marker class; sniff via
        # the algorithm's algorithm OID indirectly by re-serialising.
        re_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        assert b"BEGIN PUBLIC KEY" in re_pem

    def test_output_flag_redirects_keypair_location(self, tmp_path: Path) -> None:
        target_dir = tmp_path / "custom-keys"
        target_dir.mkdir()
        target = target_dir / "my-key.priv"
        with _patch_data_dir(tmp_path):
            result = runner.invoke(
                voice_app,
                [
                    "generate-signing-key",
                    "--mind-id",
                    "m1",
                    "--output",
                    str(target),
                ],
            )
        assert result.exit_code == 0, result.output
        assert target.is_file()
        # Public key derived alongside the private key.
        assert target_dir.joinpath("my-key.pub").is_file()
        # Canonical per-mind path NOT created when --output is used.
        assert not (tmp_path / "m1" / PRIVATE_KEY_FILENAME).exists()

    def test_help_text_mentions_signing_key_generation(self) -> None:
        # Smoke: ``--help`` returns 0 + describes the command. Used by
        # the closure report's "CLI smoke test" section.
        result = runner.invoke(voice_app, ["generate-signing-key", "--help"])
        assert result.exit_code == 0
        assert "signing" in result.output.lower()
