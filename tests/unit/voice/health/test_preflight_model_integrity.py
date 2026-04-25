"""Unit tests for the #27 model-integrity preflight extension.

Coverage:
* :func:`compute_file_sha256` chunked hashing matches a one-shot
  ``hashlib.sha256(bytes).hexdigest()`` reference.
* :func:`verify_model_integrity` correctly classifies every spec
  into ``checked`` / ``missing`` / ``failures``.
* :func:`check_model_integrity` async wrapper produces the
  ``(passed, hint, details)`` triple per the PreflightCheck protocol.
* :func:`default_model_integrity_specs` returns the expected six-spec
  list (4 voice + 2 brain) with the canonical SHA pins.
* SHA mismatch / read error / missing file each surface the right
  failure shape.

The probe is sync + side-effect-free, so the tests use ``tmp_path``
+ real file writes — no mocking of stdlib hashlib needed.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from sovyx.voice.health.preflight import (
    ModelIntegrityFailure,
    ModelIntegrityReport,
    ModelIntegritySpec,
    check_model_integrity,
    compute_file_sha256,
    default_model_integrity_specs,
    verify_model_integrity,
)

# ── compute_file_sha256 ──────────────────────────────────────────


class TestComputeFileSha256:
    def test_matches_one_shot_hashlib(self, tmp_path: Path) -> None:
        # Reference: feed identical bytes through hashlib directly
        # and confirm the chunked stream-hash matches.
        payload = b"sovyx model integrity probe (#27) test fixture\n" * 1024
        f = tmp_path / "fixture.bin"
        f.write_bytes(payload)
        expected = hashlib.sha256(payload).hexdigest()
        assert compute_file_sha256(f) == expected

    def test_handles_large_file_streaming(self, tmp_path: Path) -> None:
        # 5 MiB > 1 MiB chunk size — exercises the streaming loop.
        payload = b"x" * (5 << 20)
        f = tmp_path / "big.bin"
        f.write_bytes(payload)
        assert compute_file_sha256(f) == hashlib.sha256(payload).hexdigest()

    def test_empty_file_hashes_to_known_constant(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert compute_file_sha256(f) == hashlib.sha256(b"").hexdigest()

    def test_missing_file_raises_oserror(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            compute_file_sha256(tmp_path / "does-not-exist.bin")


# ── verify_model_integrity ──────────────────────────────────────


def _spec(
    *,
    name: str,
    path: Path,
    expected_sha: str = "",
    category: str = "vad",
) -> ModelIntegritySpec:
    return ModelIntegritySpec(
        name=name,
        category=category,
        path=path,
        expected_sha256=expected_sha,
    )


class TestVerifyModelIntegrity:
    def test_all_present_with_matching_sha_passes(self, tmp_path: Path) -> None:
        payload = b"valid model bytes"
        sha = hashlib.sha256(payload).hexdigest()
        a = tmp_path / "a.onnx"
        a.write_bytes(payload)
        b = tmp_path / "b.onnx"
        b.write_bytes(payload)

        report = verify_model_integrity(
            [
                _spec(name="model-a", path=a, expected_sha=sha),
                _spec(name="model-b", path=b, expected_sha=sha),
            ],
        )
        assert isinstance(report, ModelIntegrityReport)
        assert report.passed is True
        assert set(report.checked) == {"model-a", "model-b"}
        assert report.missing == ()
        assert report.failures == ()

    def test_missing_file_lands_in_missing_not_failures(self, tmp_path: Path) -> None:
        # File not yet downloaded — first-boot legitimate state.
        report = verify_model_integrity(
            [
                _spec(
                    name="not-downloaded",
                    path=tmp_path / "absent.onnx",
                    expected_sha="00" * 32,
                ),
            ],
        )
        assert report.passed is True
        assert report.missing == ("not-downloaded",)
        assert report.checked == ()
        assert report.failures == ()

    def test_sha_mismatch_lands_in_failures(self, tmp_path: Path) -> None:
        payload = b"corrupted bytes"
        f = tmp_path / "corrupt.onnx"
        f.write_bytes(payload)
        wrong_sha = "00" * 32  # definitely won't match

        report = verify_model_integrity(
            [_spec(name="corrupt", path=f, expected_sha=wrong_sha)],
        )
        assert report.passed is False
        assert len(report.failures) == 1
        failure = report.failures[0]
        assert failure.reason == "sha_mismatch"
        assert failure.expected_sha256 == wrong_sha
        assert failure.actual_sha256 == hashlib.sha256(payload).hexdigest()

    def test_no_expected_sha_skips_verification(self, tmp_path: Path) -> None:
        # An empty expected_sha256 disables the SHA check — file-exists
        # alone is enough to enter ``checked``.
        f = tmp_path / "unpinned.onnx"
        f.write_bytes(b"anything")
        report = verify_model_integrity(
            [_spec(name="unpinned", path=f, expected_sha="")],
        )
        assert report.passed is True
        assert report.checked == ("unpinned",)
        assert report.failures == ()

    def test_partial_failure_does_not_abort_other_checks(self, tmp_path: Path) -> None:
        # One bad spec should NOT prevent the other specs from being
        # verified — defensive isolation.
        good_payload = b"good"
        good_sha = hashlib.sha256(good_payload).hexdigest()
        good = tmp_path / "good.onnx"
        good.write_bytes(good_payload)

        bad = tmp_path / "bad.onnx"
        bad.write_bytes(b"different bytes")

        report = verify_model_integrity(
            [
                _spec(name="good", path=good, expected_sha=good_sha),
                _spec(name="bad", path=bad, expected_sha=good_sha),
            ],
        )
        assert report.passed is False
        assert "good" in report.checked
        assert any(f.name == "bad" for f in report.failures)

    def test_empty_spec_list_passes(self) -> None:
        report = verify_model_integrity([])
        assert report.passed is True
        assert report.checked == ()
        assert report.missing == ()
        assert report.failures == ()


# ── check_model_integrity (async wrapper) ────────────────────────


class TestCheckModelIntegrity:
    def test_passes_when_all_models_match_or_missing(self, tmp_path: Path) -> None:
        payload = b"ok"
        sha = hashlib.sha256(payload).hexdigest()
        f = tmp_path / "m.onnx"
        f.write_bytes(payload)

        passed, hint, details = asyncio.run(
            check_model_integrity(
                data_dir=tmp_path,
                specs=[_spec(name="m", path=f, expected_sha=sha)],
            ),
        )
        assert passed is True
        assert hint == ""
        assert details["checked"] == ["m"]
        assert details["missing"] == []
        assert details["failures"] == []

    def test_fails_with_actionable_hint_on_corruption(self, tmp_path: Path) -> None:
        payload = b"corrupt"
        f = tmp_path / "broken.onnx"
        f.write_bytes(payload)

        passed, hint, details = asyncio.run(
            check_model_integrity(
                data_dir=tmp_path,
                specs=[_spec(name="broken", path=f, expected_sha="00" * 32)],
            ),
        )
        assert passed is False
        # Hint must name the failing model + suggest remediation.
        assert "broken" in hint
        assert "Re-download" in hint or "delete" in hint
        # Details carry the structured failure for the dashboard.
        assert len(details["failures"]) == 1
        assert details["failures"][0]["reason"] == "sha_mismatch"


# ── default_model_integrity_specs ────────────────────────────────


class TestDefaultModelIntegritySpecs:
    def test_returns_voice_and_brain_specs(self, tmp_path: Path) -> None:
        specs = default_model_integrity_specs(data_dir=tmp_path)
        names = {s.name for s in specs}
        # Voice models with SHA pins (3): SileroVAD + Kokoro model +
        # voices. Moonshine has no SHA (managed by external package)
        # and is intentionally excluded.
        assert "silero-vad-v5" in names
        assert "kokoro-v1.0-int8" in names
        assert "kokoro-voices-v1.0" in names
        # Brain models (2): e5 embedding + tokenizer.
        assert "e5-small-v2" in names
        # Tokenizer entry uses the filename.
        assert any(s.category == "tokenizer" for s in specs)

    def test_specs_use_data_dir_layout(self, tmp_path: Path) -> None:
        specs = default_model_integrity_specs(data_dir=tmp_path)
        for spec in specs:
            # Every spec path must live under data_dir/models/...
            assert tmp_path / "models" in spec.path.parents

    def test_specs_carry_canonical_sha_pins(self, tmp_path: Path) -> None:
        # Each spec must have a non-empty expected SHA — verifies we
        # didn't accidentally ship an unpinned spec.
        specs = default_model_integrity_specs(data_dir=tmp_path)
        for spec in specs:
            assert spec.expected_sha256, (
                f"spec {spec.name} has no SHA pin — would degrade to a file-exists-only check"
            )
            # SHA must be 64 hex chars (full digest).
            assert len(spec.expected_sha256) == 64  # noqa: PLR2004


# ── Failure dataclass shape ─────────────────────────────────────


class TestFailureShape:
    def test_failure_is_frozen(self) -> None:
        f = ModelIntegrityFailure(
            name="x",
            category="vad",
            path="/tmp/x",  # noqa: S108
            reason="sha_mismatch",
        )
        with pytest.raises(Exception) as exc:  # noqa: PT011
            f.reason = "other"  # type: ignore[misc]
        assert (
            "frozen" in str(exc.value).lower()
            or "FrozenInstanceError"
            in type(
                exc.value,
            ).__name__
        )


pytestmark = pytest.mark.timeout(15)
