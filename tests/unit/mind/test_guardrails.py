"""Tests for custom guardrails — SPE-002 (TASK-326).

Covers:
- Default guardrails (honesty, privacy, safety) present
- Custom guardrails injected into system prompt
- Severity levels (critical/warning) rendered correctly
- CRUD via _apply_safety
- Builtins non-deletable
- YAML persistence (guardrails in config output)
"""

from __future__ import annotations

from sovyx.dashboard.config import _apply_safety, get_config
from sovyx.mind.config import (
    DEFAULT_GUARDRAILS,
    Guardrail,
    MindConfig,
    SafetyConfig,
)
from sovyx.mind.personality import PersonalityEngine


def _config(**overrides: object) -> MindConfig:
    defaults: dict[str, object] = {"name": "Aria"}
    defaults.update(overrides)
    return MindConfig(**defaults)  # type: ignore[arg-type]


class TestDefaultGuardrails:
    """3 default guardrails always present."""

    def test_three_defaults(self) -> None:
        assert len(DEFAULT_GUARDRAILS) == 3

    def test_default_ids(self) -> None:
        ids = {g.id for g in DEFAULT_GUARDRAILS}
        assert ids == {"honesty", "privacy", "safety"}

    def test_all_builtin(self) -> None:
        for g in DEFAULT_GUARDRAILS:
            assert g.builtin is True

    def test_all_critical(self) -> None:
        for g in DEFAULT_GUARDRAILS:
            assert g.severity == "critical"

    def test_safety_config_has_defaults(self) -> None:
        cfg = SafetyConfig()
        assert len(cfg.guardrails) == 3
        ids = {g.id for g in cfg.guardrails}
        assert "honesty" in ids


class TestSystemPromptIntegration:
    """Guardrails appear in system prompt."""

    def test_defaults_in_prompt(self) -> None:
        cfg = _config()
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "ABSOLUTE RULES" in prompt
        assert "[CRITICAL]" in prompt
        assert "truthful" in prompt.lower()
        assert "personal data" in prompt.lower()

    def test_custom_guardrail_in_prompt(self) -> None:
        cfg = _config(safety=SafetyConfig(
            guardrails=list(DEFAULT_GUARDRAILS) + [
                Guardrail(
                    id="no-medical",
                    rule="Never provide medical diagnoses.",
                    severity="warning",
                ),
            ],
        ))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "medical diagnoses" in prompt.lower()
        assert "[WARNING]" in prompt

    def test_empty_guardrails_no_section(self) -> None:
        cfg = _config(safety=SafetyConfig(guardrails=[]))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "ABSOLUTE RULES" not in prompt


class TestGuardrailCRUD:
    """CRUD via _apply_safety."""

    def test_add_custom_guardrail(self) -> None:
        cfg = _config()
        changes: dict[str, str] = {}
        _apply_safety(cfg, {
            "guardrails": [
                {"id": "no-legal", "rule": "Never provide legal advice.", "severity": "warning"},
            ],
        }, changes)
        assert len(cfg.safety.guardrails) == 4  # 3 builtins + 1 custom
        ids = {g.id for g in cfg.safety.guardrails}
        assert "no-legal" in ids

    def test_builtins_preserved(self) -> None:
        cfg = _config()
        changes: dict[str, str] = {}
        _apply_safety(cfg, {
            "guardrails": [
                {"id": "custom1", "rule": "Be concise."},
            ],
        }, changes)
        builtin_ids = {g.id for g in cfg.safety.guardrails if g.builtin}
        assert builtin_ids == {"honesty", "privacy", "safety"}

    def test_cannot_override_builtin(self) -> None:
        cfg = _config()
        changes: dict[str, str] = {}
        _apply_safety(cfg, {
            "guardrails": [
                {"id": "honesty", "rule": "Override attempt"},
            ],
        }, changes)
        honesty = next(g for g in cfg.safety.guardrails if g.id == "honesty")
        assert honesty.builtin is True
        assert "Override" not in honesty.rule

    def test_replace_custom_guardrails(self) -> None:
        cfg = _config(safety=SafetyConfig(
            guardrails=list(DEFAULT_GUARDRAILS) + [
                Guardrail(id="old-custom", rule="Old rule"),
            ],
        ))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {
            "guardrails": [
                {"id": "new-custom", "rule": "New rule"},
            ],
        }, changes)
        ids = {g.id for g in cfg.safety.guardrails}
        assert "new-custom" in ids
        assert "old-custom" not in ids  # Replaced

    def test_empty_guardrails_keeps_builtins(self) -> None:
        cfg = _config()
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"guardrails": []}, changes)
        assert len(cfg.safety.guardrails) == 3  # Only builtins remain

    def test_changes_dict_updated(self) -> None:
        cfg = _config()
        changes: dict[str, str] = {}
        _apply_safety(cfg, {
            "guardrails": [
                {"id": "c1", "rule": "Rule 1"},
                {"id": "c2", "rule": "Rule 2"},
            ],
        }, changes)
        assert "safety.guardrails" in changes
        assert "2 custom" in changes["safety.guardrails"]


class TestConfigOutput:
    """Guardrails appear in get_config output."""

    def test_guardrails_in_config(self) -> None:
        cfg = _config()
        output = get_config(cfg)
        guardrails = output["safety"]["guardrails"]
        assert len(guardrails) == 3
        assert all("id" in g for g in guardrails)
        assert all("rule" in g for g in guardrails)
        assert all("severity" in g for g in guardrails)
        assert all("builtin" in g for g in guardrails)

    def test_custom_guardrail_in_config(self) -> None:
        cfg = _config(safety=SafetyConfig(
            guardrails=list(DEFAULT_GUARDRAILS) + [
                Guardrail(id="custom", rule="My rule", severity="warning"),
            ],
        ))
        output = get_config(cfg)
        guardrails = output["safety"]["guardrails"]
        assert len(guardrails) == 4
        custom = [g for g in guardrails if g["id"] == "custom"]
        assert len(custom) == 1
        assert custom[0]["severity"] == "warning"
        assert custom[0]["builtin"] is False


class TestGuardrailModel:
    """Guardrail model validation."""

    def test_default_severity(self) -> None:
        g = Guardrail(id="test", rule="Test rule")
        assert g.severity == "critical"

    def test_default_not_builtin(self) -> None:
        g = Guardrail(id="test", rule="Test rule")
        assert g.builtin is False

    def test_warning_severity(self) -> None:
        g = Guardrail(id="test", rule="Test", severity="warning")
        assert g.severity == "warning"
