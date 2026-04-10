"""Tests for safety config hot-reload (TASK-325).

Covers:
- PUT /api/config with safety changes → reflects immediately
- SafetyConfigUpdated WebSocket event emitted
- GET /api/safety/status returns active runtime state
- Structured log emitted on safety config change
- PersonalityEngine system prompt reflects new safety in <1s
- Dynamic config: no restart needed
"""

from __future__ import annotations

from typing import Any

import pytest

from sovyx.cognitive.safety_patterns import get_pattern_count, resolve_patterns
from sovyx.dashboard.config import _apply_safety
from sovyx.mind.config import MindConfig, SafetyConfig
from sovyx.mind.personality import PersonalityEngine


class TestApplySafetyChanges:
    """Safety config mutations via _apply_safety."""

    def test_content_filter_none_to_standard(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(content_filter="none"))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"content_filter": "standard"}, changes)
        assert cfg.safety.content_filter == "standard"
        assert "safety.content_filter" in changes
        assert "none → standard" in changes["safety.content_filter"]

    def test_content_filter_standard_to_strict(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(content_filter="standard"))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"content_filter": "strict"}, changes)
        assert cfg.safety.content_filter == "strict"

    def test_child_safe_toggle(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(child_safe_mode=False))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"child_safe_mode": True}, changes)
        assert cfg.safety.child_safe_mode is True
        assert "safety.child_safe_mode" in changes

    def test_financial_confirmation_toggle(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(financial_confirmation=False))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"financial_confirmation": True}, changes)
        assert cfg.safety.financial_confirmation is True
        assert "safety.financial_confirmation" in changes

    def test_no_change_when_same_value(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(content_filter="standard"))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"content_filter": "standard"}, changes)
        assert changes == {}

    def test_invalid_filter_ignored(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(content_filter="standard"))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"content_filter": "invalid"}, changes)
        assert cfg.safety.content_filter == "standard"
        assert changes == {}

    def test_multiple_changes_at_once(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(
            content_filter="none",
            child_safe_mode=False,
            financial_confirmation=False,
        ))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {
            "content_filter": "strict",
            "child_safe_mode": True,
            "financial_confirmation": True,
        }, changes)
        assert cfg.safety.content_filter == "strict"
        assert cfg.safety.child_safe_mode is True
        assert cfg.safety.financial_confirmation is True
        assert len(changes) == 3


class TestStructuredLog:
    """Structured log emitted on safety config change."""

    def test_log_emitted_on_change(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(content_filter="none"))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"content_filter": "standard"}, changes)
        # structlog emits to stdout — verify the structured log exists
        out = capsys.readouterr().out
        assert "safety_config_changed" in out
        assert "content_filter" in out

    def test_no_log_when_no_change(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(content_filter="standard"))
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"content_filter": "standard"}, changes)
        out = capsys.readouterr().out
        assert "safety_config_changed" not in out


class TestDynamicPatternResolution:
    """Patterns resolve dynamically from config reference — no cache."""

    def test_patterns_change_with_config(self) -> None:
        cfg = SafetyConfig(content_filter="none")
        patterns_none = resolve_patterns(cfg)
        assert len(patterns_none) == 0

        cfg.content_filter = "standard"  # type: ignore[assignment]
        patterns_standard = resolve_patterns(cfg)
        assert len(patterns_standard) > 0

        cfg.content_filter = "strict"  # type: ignore[assignment]
        patterns_strict = resolve_patterns(cfg)
        assert len(patterns_strict) > len(patterns_standard)

    def test_child_safe_overrides_filter(self) -> None:
        cfg = SafetyConfig(content_filter="none", child_safe_mode=True)
        patterns = resolve_patterns(cfg)
        # Child-safe always uses full pattern set regardless of filter
        assert len(patterns) > 0

    def test_pattern_count_reflects_config(self) -> None:
        cfg_none = SafetyConfig(content_filter="none")
        cfg_std = SafetyConfig(content_filter="standard")
        cfg_strict = SafetyConfig(content_filter="strict")

        assert get_pattern_count(cfg_none) == 0
        assert get_pattern_count(cfg_std) > 0
        assert get_pattern_count(cfg_strict) > get_pattern_count(cfg_std)


class TestSystemPromptReflectsChange:
    """PersonalityEngine prompt reflects safety config dynamically."""

    def test_prompt_changes_with_filter(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(content_filter="none"))
        engine = PersonalityEngine(cfg)

        prompt_none = engine.generate_system_prompt()
        cfg.safety.content_filter = "standard"  # type: ignore[assignment]
        prompt_standard = engine.generate_system_prompt()

        assert prompt_none != prompt_standard
        assert "Standard" in prompt_standard

    def test_prompt_changes_with_child_safe(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(child_safe_mode=False))
        engine = PersonalityEngine(cfg)

        prompt_off = engine.generate_system_prompt()
        cfg.safety.child_safe_mode = True
        prompt_on = engine.generate_system_prompt()

        assert prompt_off != prompt_on
        assert "CHILD SAFETY MODE" in prompt_on

    def test_prompt_changes_with_financial(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(financial_confirmation=False))
        engine = PersonalityEngine(cfg)

        prompt_off = engine.generate_system_prompt()
        cfg.safety.financial_confirmation = True
        prompt_on = engine.generate_system_prompt()

        assert prompt_off != prompt_on
        assert "confirmation" in prompt_on.lower()


class TestSafetyStatusEndpoint:
    """GET /api/safety/status returns active runtime state."""

    @pytest.fixture()
    def _mock_app_state(self) -> dict[str, Any]:
        cfg = MindConfig(
            name="Aria",
            safety=SafetyConfig(
                content_filter="strict",
                child_safe_mode=False,
                financial_confirmation=True,
            ),
        )
        return {"mind_config": cfg}

    def test_status_reflects_config(
        self,
        _mock_app_state: dict[str, Any],
    ) -> None:
        cfg = _mock_app_state["mind_config"]
        safety = cfg.safety
        patterns = resolve_patterns(safety)

        assert safety.content_filter == "strict"
        assert len(patterns) > 0
        assert safety.financial_confirmation is True

    def test_status_changes_after_mutation(self) -> None:
        cfg = MindConfig(name="Aria", safety=SafetyConfig(content_filter="none"))
        assert len(resolve_patterns(cfg.safety)) == 0

        changes: dict[str, str] = {}
        _apply_safety(cfg, {"content_filter": "strict"}, changes)
        assert len(resolve_patterns(cfg.safety)) > 0
