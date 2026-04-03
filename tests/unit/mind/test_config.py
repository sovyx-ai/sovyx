"""Tests for sovyx.mind.config — Mind definition and YAML loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.engine.errors import MindConfigError
from sovyx.engine.types import MindId
from sovyx.mind.config import (
    BrainConfig,
    ChannelsConfig,
    LLMConfig,
    MindConfig,
    OceanConfig,
    PersonalityConfig,
    SafetyConfig,
    create_default_mind_config,
    load_mind_config,
)


class TestPersonalityConfig:
    """PersonalityConfig validation."""

    def test_defaults(self) -> None:
        p = PersonalityConfig()
        assert p.tone == "warm"
        assert p.formality == 0.5
        assert p.empathy == 0.8

    def test_invalid_tone(self) -> None:
        with pytest.raises(ValidationError):
            PersonalityConfig(tone="aggressive")  # type: ignore[arg-type]

    def test_clamp_values(self) -> None:
        with pytest.raises(ValidationError):
            PersonalityConfig(humor=1.5)

    def test_negative_value(self) -> None:
        with pytest.raises(ValidationError):
            PersonalityConfig(curiosity=-0.1)


class TestOceanConfig:
    """OCEAN personality model."""

    def test_defaults(self) -> None:
        o = OceanConfig()
        assert o.openness == 0.7
        assert o.neuroticism == 0.3

    def test_boundary_values(self) -> None:
        o = OceanConfig(openness=0.0, neuroticism=1.0)
        assert o.openness == 0.0
        assert o.neuroticism == 1.0


class TestLLMConfig:
    """LLM configuration."""

    def test_defaults(self) -> None:
        llm = LLMConfig()
        assert llm.default_model == "claude-sonnet-4-20250514"
        assert llm.fast_model == "claude-3-5-haiku-20241022"
        assert llm.temperature == 0.7

    def test_temperature_range(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(temperature=3.0)


class TestBrainConfig:
    """Brain configuration."""

    def test_defaults(self) -> None:
        b = BrainConfig()
        assert b.consolidation_interval_hours == 6
        assert b.forgetting_enabled is True
        assert b.decay_rate == 0.1


class TestSafetyConfig:
    """Safety configuration."""

    def test_defaults(self) -> None:
        s = SafetyConfig()
        assert s.content_filter == "standard"
        assert s.financial_confirmation is True

    def test_invalid_filter(self) -> None:
        with pytest.raises(ValidationError):
            SafetyConfig(content_filter="custom")  # type: ignore[arg-type]


class TestMindConfig:
    """MindConfig composite model."""

    def test_minimal(self) -> None:
        m = MindConfig(name="Aria")
        assert m.name == "Aria"
        assert m.id == MindId("aria")

    def test_auto_id_from_name(self) -> None:
        m = MindConfig(name="My Cool Bot")
        assert m.id == MindId("my-cool-bot")

    def test_explicit_id(self) -> None:
        m = MindConfig(name="Aria", id=MindId("custom-id"))
        assert m.id == MindId("custom-id")

    def test_defaults_populated(self) -> None:
        m = MindConfig(name="Test")
        assert isinstance(m.personality, PersonalityConfig)
        assert isinstance(m.ocean, OceanConfig)
        assert isinstance(m.llm, LLMConfig)
        assert isinstance(m.brain, BrainConfig)
        assert isinstance(m.channels, ChannelsConfig)
        assert isinstance(m.safety, SafetyConfig)

    def test_name_required(self) -> None:
        with pytest.raises(ValidationError):
            MindConfig()  # type: ignore[call-arg]


class TestLoadMindConfig:
    """load_mind_config() — YAML loading."""

    def test_load_valid(self, tmp_path: Path) -> None:
        data = {"name": "Aria", "language": "pt-BR"}
        path = tmp_path / "mind.yaml"
        path.write_text(yaml.dump(data), encoding="utf-8")

        config = load_mind_config(path)
        assert config.name == "Aria"
        assert config.language == "pt-BR"
        assert config.id == MindId("aria")

    def test_load_full_config(self, tmp_path: Path) -> None:
        data = {
            "name": "Aria",
            "personality": {"tone": "playful", "humor": 0.9},
            "ocean": {"openness": 0.9},
            "llm": {"temperature": 0.5},
            "safety": {"child_safe_mode": True},
        }
        path = tmp_path / "mind.yaml"
        path.write_text(yaml.dump(data), encoding="utf-8")

        config = load_mind_config(path)
        assert config.personality.tone == "playful"
        assert config.personality.humor == 0.9
        assert config.ocean.openness == 0.9
        assert config.safety.child_safe_mode is True

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(MindConfigError, match="not found"):
            load_mind_config(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "mind.yaml"
        path.write_text("{{invalid yaml::", encoding="utf-8")
        with pytest.raises(MindConfigError, match="Invalid YAML"):
            load_mind_config(path)

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "mind.yaml"
        path.write_text("- just\n- a\n- list", encoding="utf-8")
        with pytest.raises(MindConfigError, match="mapping"):
            load_mind_config(path)

    def test_validation_error(self, tmp_path: Path) -> None:
        data = {"name": "Aria", "personality": {"humor": 5.0}}
        path = tmp_path / "mind.yaml"
        path.write_text(yaml.dump(data), encoding="utf-8")
        with pytest.raises(MindConfigError, match="validation"):
            load_mind_config(path)


class TestCreateDefaultMindConfig:
    """create_default_mind_config() — YAML generation."""

    def test_creates_file(self, tmp_path: Path) -> None:
        path = create_default_mind_config("Aria", tmp_path)
        assert path.exists()
        assert path.name == "mind.yaml"

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Create → Load → Verify."""
        create_default_mind_config("Aria", tmp_path)
        config = load_mind_config(tmp_path / "mind.yaml")
        assert config.name == "Aria"
        assert config.id == MindId("aria")

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested"
        path = create_default_mind_config("Test", nested)
        assert path.exists()


class TestPropertyBased:
    """Property-based tests."""

    @given(
        name=st.text(min_size=1, max_size=50).filter(lambda s: s.strip()),
    )
    @settings(max_examples=30)
    def test_any_name_produces_valid_config(self, name: str) -> None:
        """Any non-empty name → valid MindConfig."""
        config = MindConfig(name=name)
        assert config.name == name
        assert len(str(config.id)) > 0

    @given(
        val=st.floats(min_value=0.0, max_value=1.0),
    )
    @settings(max_examples=20)
    def test_ocean_values_in_range(self, val: float) -> None:
        """Any float [0, 1] → valid OCEAN."""
        o = OceanConfig(
            openness=val,
            conscientiousness=val,
            extraversion=val,
            agreeableness=val,
            neuroticism=val,
        )
        assert o.openness == val
