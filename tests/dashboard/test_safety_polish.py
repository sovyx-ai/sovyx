"""Tests for dashboard safety polish i18n + config (TASK-332).

Covers:
- All new i18n keys exist
- Config output includes new safety fields
- Guardrails in config output
"""

from __future__ import annotations

import json
from pathlib import Path

from sovyx.dashboard.config import get_config
from sovyx.mind.config import DEFAULT_GUARDRAILS, Guardrail, MindConfig, SafetyConfig


def _config(**overrides: object) -> MindConfig:
    defaults: dict[str, object] = {"name": "Aria"}
    defaults.update(overrides)
    return MindConfig(**defaults)  # type: ignore[arg-type]


LOCALES_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "locales" / "en"


class TestI18NKeys:
    """All new safety i18n keys exist in settings.json."""

    def test_new_keys_present(self) -> None:
        settings_path = LOCALES_DIR / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        safety = data["safety"]

        required_keys = [
            "title",
            "description",
            "contentFilter",
            "contentFilterDesc",
            "childSafeMode",
            "childSafeModeDesc",
            "childSafeWarning",
            "childSafeEnforced",
            "financialConfirmation",
            "financialConfirmationDesc",
            "piiProtection",
            "piiProtectionDesc",
            "guardrails",
            "guardrailsDesc",
            "guardrailBuiltin",
            "guardrailCustom",
            "guardrailAdd",
            "guardrailPlaceholder",
            "guardrailSeverity",
            "guardrailCritical",
            "guardrailWarning",
            "patternCount",
        ]
        for key in required_keys:
            assert key in safety, f"Missing i18n key: safety.{key}"

    def test_filter_tooltips(self) -> None:
        settings_path = LOCALES_DIR / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        tooltips = data["safety"]["filterTooltips"]
        assert "none" in tooltips
        assert "standard" in tooltips
        assert "strict" in tooltips


class TestConfigOutput:
    """Config output includes all safety fields."""

    def test_pii_protection_in_config(self) -> None:
        cfg = _config()
        output = get_config(cfg)
        assert "pii_protection" in output["safety"]

    def test_guardrails_in_config(self) -> None:
        cfg = _config()
        output = get_config(cfg)
        assert "guardrails" in output["safety"]
        assert len(output["safety"]["guardrails"]) == 3

    def test_guardrail_fields(self) -> None:
        cfg = _config(
            safety=SafetyConfig(
                guardrails=list(DEFAULT_GUARDRAILS)
                + [
                    Guardrail(id="custom", rule="My rule", severity="warning"),
                ],
            )
        )
        output = get_config(cfg)
        guardrails = output["safety"]["guardrails"]
        custom = [g for g in guardrails if g["id"] == "custom"]
        assert len(custom) == 1
        assert custom[0]["severity"] == "warning"
        assert custom[0]["builtin"] is False
        assert custom[0]["rule"] == "My rule"
