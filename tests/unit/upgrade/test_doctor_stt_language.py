"""Tests for :func:`sovyx.upgrade.doctor._check_stt_language_match`.

ENGINES-9 — the STT sibling of ``_check_piper_locale_match``. Pre-fix
the Moonshine no-Portuguese limitation surfaced only at pipeline start
(factory WARN + ``stt_language_coerced`` degraded banner); this probe
brings it to doctor/setup preflight time. Contract surfaces pinned:

1. **Supported language** (incl. region-tagged variants) → ``PASS``.
2. **Unsupported language** → ``WARN`` (LENIENT per
   ``feedback_staged_adoption``) whose message names the ACTUAL
   runtime behaviour (coercion to English transcription) +
   ``lenient_mode=True`` in details.
3. **None / empty input** → falls back to ``en`` to keep the probe
   runnable without an active mind context.
4. **SSoT alignment** — classification comes from
   ``normalize_stt_language`` + ``MOONSHINE_SUPPORTED_LANGUAGES``,
   the same symbols the factory wiring uses.
"""

from __future__ import annotations

from sovyx.upgrade.doctor import (
    DiagnosticStatus,
    _check_stt_language_match,
)
from sovyx.voice.stt import MOONSHINE_SUPPORTED_LANGUAGES, normalize_stt_language


class TestSttLanguageMatchPass:
    """Moonshine-supported languages return PASS."""

    def test_en_passes(self) -> None:
        result = _check_stt_language_match("en")
        assert result.status == DiagnosticStatus.PASS
        assert result.check == "stt_language_match"
        assert result.details is not None
        assert result.details["normalized_language"] == "en"

    def test_en_us_region_strips_to_supported_en(self) -> None:
        """Region stripping mirrors the factory's SSoT normaliser."""
        result = _check_stt_language_match("en-US")
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["normalized_language"] == "en"

    def test_every_moonshine_language_passes(self) -> None:
        for code in MOONSHINE_SUPPORTED_LANGUAGES:
            result = _check_stt_language_match(code)
            assert result.status == DiagnosticStatus.PASS, code


class TestSttLanguageMatchWarn:
    """Unsupported languages return WARN (LENIENT) naming the coercion."""

    def test_pt_br_warns_and_names_english_coercion(self) -> None:
        """The pt-BR operator's exact ENGINES-9 scenario."""
        result = _check_stt_language_match("pt-BR")
        assert result.status == DiagnosticStatus.WARN
        # Honest message: names the ACTUAL runtime behaviour (factory
        # coerces STT to English) — not a vague "unsupported".
        assert "transcribed in English" in result.message
        assert result.details is not None
        assert result.details["normalized_language"] == "pt"
        assert result.details["coerced_language"] == "en"
        assert result.details["lenient_mode"] is True

    def test_warn_fix_suggestion_matches_factory_remediation(self) -> None:
        """Same remediation the factory WARN carries (validate.py SSoT)."""
        result = _check_stt_language_match("de")
        assert result.status == DiagnosticStatus.WARN
        assert result.fix_suggestion is not None
        assert "multilingual STT engine" in result.fix_suggestion
        for code in sorted(MOONSHINE_SUPPORTED_LANGUAGES):
            assert code in result.fix_suggestion
        assert "voice will transcribe in English" in result.fix_suggestion

    def test_region_stripping_does_not_rescue_absent_base(self) -> None:
        """pt-BR → pt is still unsupported (LIVE-2 Phase 4 contract)."""
        assert normalize_stt_language("pt-BR") == "pt"
        result = _check_stt_language_match("pt-BR")
        assert result.status == DiagnosticStatus.WARN


class TestSttLanguageMatchFallback:
    """None / empty language falls back to en."""

    def test_none_falls_back_to_en(self) -> None:
        result = _check_stt_language_match(None)
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["language"] == "en"

    def test_empty_string_falls_back_to_en(self) -> None:
        result = _check_stt_language_match("")
        assert result.status == DiagnosticStatus.PASS

    def test_whitespace_only_falls_back_to_en(self) -> None:
        result = _check_stt_language_match("   ")
        assert result.status == DiagnosticStatus.PASS


class TestSttLanguageMatchCli:
    """`sovyx doctor stt_language_match` — exit semantics mirror the sibling."""

    def test_pass_exits_zero(self) -> None:
        from typer.testing import CliRunner

        from sovyx.cli.commands.doctor import doctor_app

        result = CliRunner().invoke(doctor_app, ["stt_language_match", "--language", "en-US"])
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_warn_exits_one_and_names_coercion(self) -> None:
        from typer.testing import CliRunner

        from sovyx.cli.commands.doctor import doctor_app

        result = CliRunner().invoke(doctor_app, ["stt_language_match", "--language", "pt-BR"])
        assert result.exit_code == 1
        assert "WARN" in result.output
        assert "English" in result.output

    def test_json_mode_emits_diagnostic_result(self) -> None:
        import json

        from typer.testing import CliRunner

        from sovyx.cli.commands.doctor import doctor_app

        result = CliRunner().invoke(
            doctor_app, ["stt_language_match", "--language", "ja", "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["check"] == "stt_language_match"
        assert payload["status"] == "pass"
