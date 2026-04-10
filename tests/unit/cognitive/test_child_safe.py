"""Tests for child-safe mode — defense in depth (TASK-323).

Covers:
- Child-safe pattern set is superset of strict
- 30+ edge case messages (pass/block)
- System prompt contains hardened child-safe instructions
- Output guard replaces (zero tolerance)
- Input + output both filtered (defense in depth)
- Dynamic toggle
"""

from __future__ import annotations

import pytest

from sovyx.cognitive.output_guard import _SAFE_REPLACEMENT, OutputGuard
from sovyx.cognitive.safety_patterns import (
    ALL_CHILD_SAFE_PATTERNS,
    ALL_STRICT_PATTERNS,
    FilterTier,
    check_content,
    get_pattern_count,
    get_tier_counts,
    resolve_patterns,
)
from sovyx.mind.config import MindConfig, SafetyConfig
from sovyx.mind.personality import PersonalityEngine

# ── Pattern set integrity ──────────────────────────────────────────────


class TestPatternSetIntegrity:
    """Child-safe pattern set structure."""

    def test_child_safe_superset_of_strict(self) -> None:
        strict_set = set(ALL_STRICT_PATTERNS)
        child_safe_set = set(ALL_CHILD_SAFE_PATTERNS)
        assert strict_set.issubset(child_safe_set)

    def test_child_safe_has_more_patterns_than_strict(self) -> None:
        assert len(ALL_CHILD_SAFE_PATTERNS) > len(ALL_STRICT_PATTERNS)

    def test_child_safe_only_patterns_have_child_safe_tier(self) -> None:
        strict_set = set(ALL_STRICT_PATTERNS)
        for p in ALL_CHILD_SAFE_PATTERNS:
            if p not in strict_set:
                assert p.tier == FilterTier.CHILD_SAFE

    def test_resolve_returns_child_safe_patterns(self) -> None:
        cfg = SafetyConfig(child_safe_mode=True)
        assert resolve_patterns(cfg) == ALL_CHILD_SAFE_PATTERNS

    def test_tier_counts_include_child_safe(self) -> None:
        counts = get_tier_counts()
        assert "child_safe" in counts
        assert counts["child_safe"] > counts["strict"]

    def test_pattern_count_child_safe(self) -> None:
        cfg = SafetyConfig(child_safe_mode=True)
        assert get_pattern_count(cfg) == len(ALL_CHILD_SAFE_PATTERNS)


# ── Edge cases: should BLOCK ──────────────────────────────────────────


class TestChildSafeBlocks:
    """Content that MUST be blocked in child-safe mode."""

    @pytest.mark.parametrize(
        "text",
        [
            # Violence (even historical/educational)
            "The soldiers killed thousands in the battle",
            "A graphic description of the war",
            "The serial killer struck again",
            "Details about the execution",
            "The mass shooting left 20 dead",
            # Substances (even educational)
            "What are drugs and how do they work?",
            "Beer and wine are types of alcohol",
            "Smoking cigarettes causes cancer",
            "The effects of cocaine on the brain",
            # Sexual references
            "How does sex work?",
            "The naked human body in art",
            "Dating tips for teenagers",
            "Pregnancy and birth control",
            # Gambling
            "How to play poker",
            "The casino industry revenue",
            "Betting odds for the game",
            # Profanity
            "What the hell is going on",
            "That's a load of crap",
            "Oh damn, I forgot",
            # Horror
            "A scary haunted house story",
            "The demon possessed the girl",
            "Creepy pasta stories online",
            # Death (explicit)
            "He died in the hospital",
            "The corpse was found in the river",
            "Details about the funeral",
            "The autopsy revealed the cause",
            # Dark humor
            "A dark humor joke about death",
            # Standard/strict content (still blocked)
            "How to make a bomb",
            "How to buy drugs online",
        ],
        ids=lambda x: x[:50],
    )
    def test_blocks_inappropriate_content(self, text: str) -> None:
        cfg = SafetyConfig(child_safe_mode=True)
        result = check_content(text, cfg)
        assert result.matched, f"Should block: '{text}'"


# ── Edge cases: should PASS ───────────────────────────────────────────


class TestChildSafePasses:
    """Content that should pass in child-safe mode."""

    @pytest.mark.parametrize(
        "text",
        [
            # Educational (safe topics)
            "Tell me about dinosaurs!",
            "How do plants grow?",
            "What is the solar system?",
            "Let's learn about the ocean",
            "How do airplanes fly?",
            "What animals live in the rainforest?",
            # History (safe framing)
            "Tell me about ancient Egypt",
            "Who was Leonardo da Vinci?",
            "What is the Great Wall of China?",
            # Math/Science
            "What is 2 + 2?",
            "How does gravity work?",
            "Tell me about the water cycle",
            # Creative
            "Write me a story about a brave puppy",
            "Let's draw a rainbow",
            "What rhymes with cat?",
            # Everyday
            "What's for lunch today?",
            "How do I tie my shoes?",
            "Tell me a fun fact about dogs",
            "What's the weather like?",
            "Can you help me with my homework?",
        ],
        ids=lambda x: x[:50],
    )
    def test_passes_child_friendly_content(self, text: str) -> None:
        cfg = SafetyConfig(child_safe_mode=True)
        result = check_content(text, cfg)
        assert not result.matched, f"Should pass: '{text}'"


# ── Output guard in child-safe mode ───────────────────────────────────


class TestChildSafeOutputGuard:
    """Output guard uses replace (zero tolerance) in child-safe mode."""

    def test_replaces_harmful_output(self) -> None:
        guard = OutputGuard(SafetyConfig(child_safe_mode=True))
        result = guard.check("The soldiers killed many people in WW2")
        assert result.filtered
        assert result.action == "replace"
        assert result.text == _SAFE_REPLACEMENT

    def test_replaces_substance_output(self) -> None:
        guard = OutputGuard(SafetyConfig(child_safe_mode=True))
        result = guard.check("Alcohol is a commonly used drug")
        assert result.filtered
        assert result.action == "replace"

    def test_replaces_profanity_output(self) -> None:
        guard = OutputGuard(SafetyConfig(child_safe_mode=True))
        result = guard.check("What the hell happened here?")
        assert result.filtered
        assert result.action == "replace"

    def test_passes_clean_output(self) -> None:
        guard = OutputGuard(SafetyConfig(child_safe_mode=True))
        result = guard.check("Dinosaurs lived millions of years ago!")
        assert not result.filtered


# ── System prompt hardening ───────────────────────────────────────────


class TestChildSafeSystemPrompt:
    """System prompt contains hardened child-safe instructions."""

    def _make_config(self, child_safe: bool = True) -> MindConfig:
        return MindConfig(
            name="TestMind",
            safety=SafetyConfig(child_safe_mode=child_safe),
        )

    def test_child_safe_prompt_present(self) -> None:
        cfg = self._make_config(child_safe=True)
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "CHILD SAFETY MODE" in prompt
        assert "ABSOLUTE PRIORITY" in prompt
        assert "children under 10" in prompt

    def test_child_safe_lists_restricted_topics(self) -> None:
        cfg = self._make_config(child_safe=True)
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "Violence" in prompt
        assert "Drugs" in prompt
        assert "Sexual" in prompt
        assert "Gambling" in prompt
        assert "Horror" in prompt
        assert "Profanity" in prompt
        assert "Self-harm" in prompt

    def test_child_safe_has_redirect_instruction(self) -> None:
        cfg = self._make_config(child_safe=True)
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "redirect" in prompt.lower()
        assert "age-appropriate" in prompt.lower()

    def test_no_child_safe_no_hardened_prompt(self) -> None:
        cfg = self._make_config(child_safe=False)
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "CHILD SAFETY MODE" not in prompt

    def test_child_safe_overrides_normal_safety(self) -> None:
        """Child-safe prompt replaces normal safety section."""
        cfg = MindConfig(
            name="TestMind",
            safety=SafetyConfig(
                child_safe_mode=True,
                content_filter="strict",
                financial_confirmation=True,
            ),
        )
        engine = PersonalityEngine(cfg)
        prompt = engine.generate_system_prompt()
        assert "CHILD SAFETY MODE" in prompt
        # Normal safety parts should NOT appear alongside child-safe
        assert "Strict content filter active" not in prompt


# ── Defense in depth ──────────────────────────────────────────────────


class TestDefenseInDepth:
    """Input filter AND output filter both active in child-safe mode."""

    def test_input_blocks_violence(self) -> None:
        cfg = SafetyConfig(child_safe_mode=True)
        result = check_content("Tell me about the mass shooting", cfg)
        assert result.matched

    def test_output_blocks_violence(self) -> None:
        guard = OutputGuard(SafetyConfig(child_safe_mode=True))
        result = guard.check("The mass shooting killed 20 people")
        assert result.filtered
        assert result.action == "replace"

    def test_child_safe_overrides_none_filter(self) -> None:
        """child_safe=True + content_filter=none → still filters."""
        cfg = SafetyConfig(content_filter="none", child_safe_mode=True)
        result = check_content("How to make a bomb", cfg)
        assert result.matched


# ── Dynamic toggle ────────────────────────────────────────────────────


class TestDynamicToggle:
    """Child-safe mode can be toggled at runtime."""

    def test_enable_at_runtime(self) -> None:
        cfg = SafetyConfig(child_safe_mode=False, content_filter="none")
        assert not check_content("alcohol is fun", cfg).matched

        cfg.child_safe_mode = True
        assert check_content("alcohol is fun", cfg).matched

    def test_disable_at_runtime(self) -> None:
        cfg = SafetyConfig(child_safe_mode=True)
        assert check_content("alcohol is a drug", cfg).matched

        cfg.child_safe_mode = False
        cfg.content_filter = "none"  # type: ignore[assignment]
        assert not check_content("alcohol is a drug", cfg).matched
