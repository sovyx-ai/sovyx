"""Tests for sovyx.dashboard.config — mind config read/update.

Covers: get_config, apply_config, all mutable sections, validation,
persistence, edge cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from sovyx.dashboard.config import apply_config, get_config
from sovyx.mind.config import (
    MindConfig,
    OceanConfig,
    PersonalityConfig,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_config(**overrides: object) -> MindConfig:
    """Create a MindConfig with sensible test defaults."""
    return MindConfig(name="TestMind", **overrides)  # type: ignore[arg-type]


class TestGetConfig:
    """GET /api/config — read mind configuration."""

    def test_returns_all_sections(self) -> None:
        cfg = _make_config()
        result = get_config(cfg)

        assert result["name"] == "TestMind"
        assert result["id"] == "testmind"
        assert "personality" in result
        assert "ocean" in result
        assert "safety" in result
        assert "brain" in result
        assert "llm" in result

    def test_personality_fields(self) -> None:
        cfg = _make_config()
        p = get_config(cfg)["personality"]

        assert p["tone"] == "warm"
        assert isinstance(p["formality"], float)
        assert isinstance(p["humor"], float)
        assert isinstance(p["assertiveness"], float)
        assert isinstance(p["curiosity"], float)
        assert isinstance(p["empathy"], float)
        assert isinstance(p["verbosity"], float)

    def test_ocean_fields(self) -> None:
        cfg = _make_config()
        o = get_config(cfg)["ocean"]

        assert isinstance(o["openness"], float)
        assert isinstance(o["conscientiousness"], float)
        assert isinstance(o["extraversion"], float)
        assert isinstance(o["agreeableness"], float)
        assert isinstance(o["neuroticism"], float)

    def test_safety_fields(self) -> None:
        cfg = _make_config()
        s = get_config(cfg)["safety"]

        assert isinstance(s["child_safe_mode"], bool)
        assert isinstance(s["financial_confirmation"], bool)
        assert s["content_filter"] in ("none", "standard", "strict")

    def test_brain_fields(self) -> None:
        cfg = _make_config()
        b = get_config(cfg)["brain"]

        assert isinstance(b["consolidation_interval_hours"], int)
        assert isinstance(b["dream_time"], str)
        assert isinstance(b["max_concepts"], int)
        assert isinstance(b["forgetting_enabled"], bool)
        assert isinstance(b["decay_rate"], float)

    def test_llm_fields(self) -> None:
        cfg = _make_config()
        llm = get_config(cfg)["llm"]

        assert isinstance(llm["default_provider"], str)
        assert isinstance(llm["default_model"], str)
        assert isinstance(llm["temperature"], float)
        assert isinstance(llm["streaming"], bool)
        assert isinstance(llm["budget_daily_usd"], float)

    def test_no_sensitive_fields(self) -> None:
        """API keys and token env vars must NOT appear."""
        cfg = _make_config()
        result = get_config(cfg)

        flat = str(result)
        assert "token_env" not in flat
        assert "api_key_env" not in flat
        assert "SOVYX_TELEGRAM" not in flat

    def test_custom_personality(self) -> None:
        cfg = _make_config(
            personality=PersonalityConfig(tone="direct", humor=0.1, empathy=0.9),
        )
        p = get_config(cfg)["personality"]

        assert p["tone"] == "direct"
        assert p["humor"] == 0.1
        assert p["empathy"] == 0.9

    def test_custom_ocean(self) -> None:
        cfg = _make_config(
            ocean=OceanConfig(openness=0.9, neuroticism=0.1),
        )
        o = get_config(cfg)["ocean"]

        assert o["openness"] == 0.9
        assert o["neuroticism"] == 0.1


class TestApplyConfigPersonality:
    """PUT personality updates."""

    def test_update_tone(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"personality": {"tone": "direct"}})

        assert "personality.tone" in changes
        assert cfg.personality.tone == "direct"

    def test_invalid_tone_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"personality": {"tone": "angry"}})

        assert changes == {}
        assert cfg.personality.tone == "warm"

    def test_update_float_trait(self) -> None:
        cfg = _make_config()
        old = cfg.personality.humor
        changes = apply_config(cfg, {"personality": {"humor": 0.9}})

        assert "personality.humor" in changes
        assert cfg.personality.humor == 0.9
        assert str(old) in changes["personality.humor"]

    def test_float_out_of_range_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"personality": {"humor": 1.5}})
        assert changes == {}

    def test_float_negative_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"personality": {"humor": -0.1}})
        assert changes == {}

    def test_float_non_numeric_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"personality": {"humor": "not_a_number"}})
        assert changes == {}

    def test_same_value_no_change(self) -> None:
        cfg = _make_config()
        old_humor = cfg.personality.humor
        changes = apply_config(cfg, {"personality": {"humor": old_humor}})
        assert changes == {}

    def test_multiple_traits(self) -> None:
        cfg = _make_config()
        changes = apply_config(
            cfg,
            {
                "personality": {"humor": 0.9, "empathy": 0.3, "tone": "playful"},
            },
        )
        assert len(changes) == 3
        assert cfg.personality.humor == 0.9
        assert cfg.personality.empathy == 0.3
        assert cfg.personality.tone == "playful"


class TestApplyConfigOcean:
    """PUT OCEAN updates."""

    def test_update_single_trait(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"ocean": {"openness": 0.95}})

        assert "ocean.openness" in changes
        assert cfg.ocean.openness == 0.95

    def test_update_all_traits(self) -> None:
        cfg = _make_config()
        changes = apply_config(
            cfg,
            {
                "ocean": {
                    "openness": 0.1,
                    "conscientiousness": 0.2,
                    "extraversion": 0.3,
                    "agreeableness": 0.4,
                    "neuroticism": 0.5,
                },
            },
        )
        assert len(changes) == 5
        assert cfg.ocean.openness == 0.1

    def test_out_of_range_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"ocean": {"openness": 2.0}})
        assert changes == {}

    def test_non_numeric_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"ocean": {"openness": "high"}})
        assert changes == {}


class TestApplyConfigSafety:
    """PUT safety updates."""

    def test_toggle_child_safe(self) -> None:
        cfg = _make_config()
        assert cfg.safety.child_safe_mode is False
        changes = apply_config(cfg, {"safety": {"child_safe_mode": True}})

        assert "safety.child_safe_mode" in changes
        assert cfg.safety.child_safe_mode is True

    def test_toggle_financial_confirmation(self) -> None:
        cfg = _make_config()
        assert cfg.safety.financial_confirmation is True
        changes = apply_config(cfg, {"safety": {"financial_confirmation": False}})

        assert "safety.financial_confirmation" in changes
        assert cfg.safety.financial_confirmation is False

    def test_update_content_filter(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"safety": {"content_filter": "strict"}})

        assert "safety.content_filter" in changes
        assert cfg.safety.content_filter == "strict"

    def test_invalid_content_filter(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"safety": {"content_filter": "ultra"}})
        assert changes == {}

    def test_same_value_no_change(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"safety": {"child_safe_mode": False}})
        assert changes == {}


class TestApplyConfigTopLevel:
    """PUT top-level fields (name, language, timezone)."""

    def test_update_name(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"name": "NewName"})

        assert "name" in changes
        assert cfg.name == "NewName"

    def test_empty_name_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"name": ""})
        assert changes == {}

    def test_whitespace_name_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"name": "   "})
        assert changes == {}

    def test_update_language(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"language": "pt-BR"})

        assert "language" in changes
        assert cfg.language == "pt-BR"

    def test_update_timezone(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"timezone": "America/Sao_Paulo"})

        assert "timezone" in changes
        assert cfg.timezone == "America/Sao_Paulo"

    def test_same_name_no_change(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"name": "TestMind"})
        assert changes == {}


class TestApplyConfigImmutable:
    """Immutable fields must be ignored."""

    def test_immutable_template_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"template": "evil"})
        assert changes == {}
        assert cfg.template == "assistant"

    def test_immutable_brain_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"brain": {"decay_rate": 0.99}})
        assert changes == {}

    def test_immutable_llm_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"llm": {"temperature": 2.0}})
        assert changes == {}

    def test_unknown_section_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"hacking": {"root": True}})
        assert changes == {}


class TestApplyConfigMixed:
    """Mixed updates (multiple sections at once)."""

    def test_mixed_sections(self) -> None:
        cfg = _make_config()
        changes = apply_config(
            cfg,
            {
                "personality": {"tone": "playful"},
                "ocean": {"openness": 0.99},
                "name": "NewMind",
                "safety": {"content_filter": "strict"},
            },
        )
        assert len(changes) == 4
        assert cfg.personality.tone == "playful"
        assert cfg.ocean.openness == 0.99
        assert cfg.name == "NewMind"
        assert cfg.safety.content_filter == "strict"

    def test_mixed_valid_and_invalid(self) -> None:
        cfg = _make_config()
        changes = apply_config(
            cfg,
            {
                "personality": {"tone": "direct"},
                "brain": {"decay_rate": 0.99},  # immutable
                "unknown": "value",  # unknown
            },
        )
        assert len(changes) == 1
        assert "personality.tone" in changes


class TestPersistence:
    """YAML persistence."""

    def test_persist_on_change(self, tmp_path: Path) -> None:
        cfg = _make_config()
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text(yaml.dump({"name": "TestMind", "personality": {"tone": "warm"}}))

        apply_config(cfg, {"personality": {"tone": "direct"}}, mind_yaml_path=yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert data["personality"]["tone"] == "direct"

    def test_no_persist_without_path(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"personality": {"tone": "direct"}})
        assert len(changes) == 1  # changes applied but no crash

    def test_no_persist_without_changes(self, tmp_path: Path) -> None:
        cfg = _make_config()
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text("name: TestMind\n")

        apply_config(cfg, {"personality": {"tone": "warm"}}, mind_yaml_path=yaml_path)

        # File unchanged (tone already warm)
        data = yaml.safe_load(yaml_path.read_text())
        assert "personality" not in data  # wasn't written

    def test_persist_creates_new_file(self, tmp_path: Path) -> None:
        cfg = _make_config()
        yaml_path = tmp_path / "new_mind.yaml"

        apply_config(cfg, {"personality": {"humor": 0.9}}, mind_yaml_path=yaml_path)

        assert yaml_path.exists()
        data = yaml.safe_load(yaml_path.read_text())
        assert data["personality"]["humor"] == 0.9

    def test_persist_preserves_existing_keys(self, tmp_path: Path) -> None:
        cfg = _make_config()
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text(
            yaml.dump(
                {
                    "name": "TestMind",
                    "template": "assistant",
                    "llm": {"temperature": 0.7},
                }
            )
        )

        apply_config(cfg, {"personality": {"tone": "direct"}}, mind_yaml_path=yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert data["personality"]["tone"] == "direct"
        assert data["llm"]["temperature"] == 0.7  # preserved

    def test_persist_failure_does_not_crash(self, tmp_path: Path) -> None:
        cfg = _make_config()
        yaml_path = tmp_path / "not_a_file"
        yaml_path.mkdir()

        # Should not raise
        changes = apply_config(
            cfg,
            {"personality": {"tone": "direct"}},
            mind_yaml_path=yaml_path,
        )
        assert "personality.tone" in changes

    def test_ocean_persisted(self, tmp_path: Path) -> None:
        cfg = _make_config()
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text("name: TestMind\n")

        apply_config(cfg, {"ocean": {"openness": 0.1}}, mind_yaml_path=yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert data["ocean"]["openness"] == 0.1

    def test_safety_persisted(self, tmp_path: Path) -> None:
        cfg = _make_config()
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text("name: TestMind\n")

        apply_config(
            cfg,
            {"safety": {"child_safe_mode": True}},
            mind_yaml_path=yaml_path,
        )

        data = yaml.safe_load(yaml_path.read_text())
        assert data["safety"]["child_safe_mode"] is True


class TestApplyConfigNonDict:
    """Edge case: non-dict value for dict sections."""

    def test_personality_non_dict_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"personality": "not_a_dict"})
        assert changes == {}

    def test_ocean_non_dict_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"ocean": 42})
        assert changes == {}

    def test_safety_non_dict_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"safety": [1, 2, 3]})
        assert changes == {}

    def test_name_non_str_ignored(self) -> None:
        cfg = _make_config()
        changes = apply_config(cfg, {"name": 42})
        assert changes == {}
