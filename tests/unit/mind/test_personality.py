"""Tests for sovyx.mind.personality — Personality Engine."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.mind.config import (
    MindConfig,
    OceanConfig,
    PersonalityConfig,
    SafetyConfig,
)
from sovyx.mind.personality import PersonalityEngine


def _config(**overrides: object) -> MindConfig:
    """Create MindConfig with overrides."""
    defaults: dict[str, object] = {"name": "Aria"}
    defaults.update(overrides)
    return MindConfig(**defaults)  # type: ignore[arg-type]


class TestGenerateSystemPrompt:
    """System prompt generation."""

    def test_contains_name(self) -> None:
        engine = PersonalityEngine(_config(name="Luna"))
        prompt = engine.generate_system_prompt()
        assert "Luna" in prompt

    def test_contains_tone(self) -> None:
        cfg = _config(personality=PersonalityConfig(tone="direct"))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "direct and concise" in prompt

    def test_warm_tone(self) -> None:
        cfg = _config(personality=PersonalityConfig(tone="warm"))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "warm and approachable" in prompt

    def test_playful_tone(self) -> None:
        cfg = _config(personality=PersonalityConfig(tone="playful"))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "playful" in prompt

    def test_contains_ocean_traits(self) -> None:
        engine = PersonalityEngine(_config())
        prompt = engine.generate_system_prompt()
        assert "Openness" in prompt
        assert "Conscientiousness" in prompt
        assert "Extraversion" in prompt
        assert "Agreeableness" in prompt
        assert "Neuroticism" in prompt

    def test_high_openness(self) -> None:
        cfg = _config(ocean=OceanConfig(openness=0.9))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "highly open" in prompt

    def test_low_openness(self) -> None:
        cfg = _config(ocean=OceanConfig(openness=0.1))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "practical" in prompt

    def test_contains_language(self) -> None:
        cfg = _config(language="pt-BR")
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "pt-BR" in prompt

    def test_safety_standard(self) -> None:
        engine = PersonalityEngine(_config())
        prompt = engine.generate_system_prompt()
        assert "Standard content filter" in prompt

    def test_safety_child_mode(self) -> None:
        cfg = _config(safety=SafetyConfig(child_safe_mode=True))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "CHILD SAFETY MODE" in prompt
        assert "children under 10" in prompt

    def test_safety_financial(self) -> None:
        cfg = _config(safety=SafetyConfig(financial_confirmation=True))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "financial" in prompt

    def test_safety_no_filter(self) -> None:
        cfg = _config(safety=SafetyConfig(content_filter="none", financial_confirmation=False))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        # "Safety:" section should not appear (filter=none, financial=off)
        # Anti-injection block is always present (contains "filters" in text)
        assert "Safety:" not in prompt


class TestDifferentConfigs:
    """Different configs → different prompts."""

    def test_different_names(self) -> None:
        p1 = PersonalityEngine(_config(name="Aria")).generate_system_prompt()
        p2 = PersonalityEngine(_config(name="Luna")).generate_system_prompt()
        assert p1 != p2

    def test_different_tones(self) -> None:
        p1 = PersonalityEngine(
            _config(personality=PersonalityConfig(tone="warm"))
        ).generate_system_prompt()
        p2 = PersonalityEngine(
            _config(personality=PersonalityConfig(tone="direct"))
        ).generate_system_prompt()
        assert p1 != p2

    def test_different_ocean(self) -> None:
        p1 = PersonalityEngine(_config(ocean=OceanConfig(openness=0.1))).generate_system_prompt()
        p2 = PersonalityEngine(_config(ocean=OceanConfig(openness=0.9))).generate_system_prompt()
        assert p1 != p2


class TestVerbosity:
    """Verbosity affects prompt."""

    def test_low_verbosity(self) -> None:
        cfg = _config(personality=PersonalityConfig(verbosity=0.1))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "brief" in prompt

    def test_high_verbosity(self) -> None:
        cfg = _config(personality=PersonalityConfig(verbosity=0.9))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "detailed" in prompt

    def test_medium_verbosity_no_directive(self) -> None:
        cfg = _config(personality=PersonalityConfig(verbosity=0.5))
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "brief" not in prompt
        assert "detailed" not in prompt


class TestEmotionalState:
    """Emotional state (v0.1: ignored)."""

    def test_emotional_state_ignored(self) -> None:
        engine = PersonalityEngine(_config())
        p1 = engine.generate_system_prompt()
        p2 = engine.generate_system_prompt(emotional_state={"valence": 0.9, "arousal": 0.1})
        assert p1 == p2


class TestGetPersonalitySummary:
    """Personality summary for debug."""

    def test_summary_contains_tone(self) -> None:
        engine = PersonalityEngine(_config())
        summary = engine.get_personality_summary()
        assert "tone=warm" in summary

    def test_summary_contains_ocean(self) -> None:
        engine = PersonalityEngine(_config())
        summary = engine.get_personality_summary()
        assert "OCEAN" in summary


class TestTraitDescriptors:
    """Trait descriptor helper functions."""

    def test_formality_levels(self) -> None:
        low = PersonalityEngine(
            _config(personality=PersonalityConfig(formality=0.1))
        ).generate_system_prompt()
        high = PersonalityEngine(
            _config(personality=PersonalityConfig(formality=0.9))
        ).generate_system_prompt()
        assert "casual" in low
        assert "formal" in high

    def test_humor_levels(self) -> None:
        low = PersonalityEngine(
            _config(personality=PersonalityConfig(humor=0.1))
        ).generate_system_prompt()
        high = PersonalityEngine(
            _config(personality=PersonalityConfig(humor=0.9))
        ).generate_system_prompt()
        assert "serious" in low
        assert "frequent" in high

    def test_empathy_levels(self) -> None:
        low = PersonalityEngine(
            _config(personality=PersonalityConfig(empathy=0.1))
        ).generate_system_prompt()
        high = PersonalityEngine(
            _config(personality=PersonalityConfig(empathy=0.9))
        ).generate_system_prompt()
        assert "solutions" in low
        assert "emotions" in high


class TestPropertyBased:
    """Property-based tests."""

    @given(
        tone=st.sampled_from(["warm", "neutral", "direct", "playful"]),
        formality=st.floats(min_value=0.0, max_value=1.0),
        openness=st.floats(min_value=0.0, max_value=1.0),
    )
    @settings(max_examples=30)
    def test_always_produces_valid_prompt(
        self, tone: str, formality: float, openness: float
    ) -> None:
        """Any valid config → non-empty prompt with name."""
        cfg = _config(
            personality=PersonalityConfig(
                tone=tone,
                formality=formality,  # type: ignore[arg-type]
            ),
            ocean=OceanConfig(openness=openness),
        )
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert len(prompt) > 50  # noqa: PLR2004
        assert "Aria" in prompt
