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
    EmotionalBaselineConfig,
    LLMConfig,
    MindConfig,
    OceanConfig,
    PersonalityConfig,
    PluginConfigEntry,
    PluginsConfig,
    SafetyConfig,
    ScoringConfig,
    _check_json_schema_type,
    create_default_mind_config,
    load_mind_config,
    validate_plugin_config,
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

    def test_defaults_no_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        llm = LLMConfig()
        # No API keys → fields stay empty (resolved at runtime)
        assert llm.default_model == ""
        assert llm.fast_model == ""
        assert llm.temperature == 0.7

    def test_defaults_anthropic_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        llm = LLMConfig()
        assert llm.default_model == "claude-sonnet-4-20250514"
        assert llm.fast_model == "claude-3-5-haiku-20241022"
        assert llm.default_provider == "anthropic"

    def test_defaults_openai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        llm = LLMConfig()
        assert llm.default_model == "gpt-4o"
        assert llm.fast_model == "gpt-4o-mini"
        assert llm.default_provider == "openai"

    def test_defaults_google_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "test-google")
        llm = LLMConfig()
        assert llm.default_model == "gemini-2.5-pro-preview-03-25"
        assert llm.fast_model == "gemini-2.0-flash"
        assert llm.default_provider == "google"

    def test_explicit_model_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        llm = LLMConfig(default_model="gpt-4-turbo")
        assert llm.default_model == "gpt-4-turbo"

    def test_temperature_range(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(temperature=3.0)


class TestBrainConfig:
    """Brain configuration with range validation."""

    def test_defaults(self) -> None:
        b = BrainConfig()
        assert b.consolidation_interval_hours == 6
        assert b.forgetting_enabled is True
        assert b.decay_rate == 0.1
        assert b.max_concepts == 50000
        assert b.min_strength == 0.01

    def test_valid_ranges_accepted(self) -> None:
        b = BrainConfig(
            consolidation_interval_hours=1,
            max_concepts=100,
            decay_rate=0.0,
            min_strength=0.0,
        )
        assert b.consolidation_interval_hours == 1
        assert b.max_concepts == 100

    def test_upper_bounds_accepted(self) -> None:
        b = BrainConfig(
            consolidation_interval_hours=168,
            max_concepts=1_000_000,
            decay_rate=1.0,
            min_strength=1.0,
        )
        assert b.consolidation_interval_hours == 168
        assert b.max_concepts == 1_000_000

    def test_zero_interval_rejected(self) -> None:
        with pytest.raises(ValidationError, match="consolidation_interval_hours"):
            BrainConfig(consolidation_interval_hours=0)

    def test_negative_max_concepts_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_concepts"):
            BrainConfig(max_concepts=-1)

    def test_below_minimum_concepts_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_concepts"):
            BrainConfig(max_concepts=50)

    def test_decay_rate_overflow_rejected(self) -> None:
        with pytest.raises(ValidationError, match="decay_rate"):
            BrainConfig(decay_rate=1.5)

    def test_negative_decay_rate_rejected(self) -> None:
        with pytest.raises(ValidationError, match="decay_rate"):
            BrainConfig(decay_rate=-0.1)

    def test_interval_overflow_rejected(self) -> None:
        with pytest.raises(ValidationError, match="consolidation_interval_hours"):
            BrainConfig(consolidation_interval_hours=200)

    def test_emotional_baseline_defaults_neutral(self) -> None:
        """Default baseline preserves zero-anchor behaviour (no breaking change)."""
        b = BrainConfig()
        assert b.emotional_baseline.valence == 0.0
        assert b.emotional_baseline.arousal == 0.0
        assert b.emotional_baseline.dominance == 0.0
        assert b.emotional_baseline.homeostasis_rate == 0.05


class TestEmotionalBaselineConfig:
    """ADR-001 per-mind emotional baseline."""

    def test_explicit_override(self) -> None:
        b = EmotionalBaselineConfig(valence=0.3, arousal=-0.1, dominance=0.2)
        assert b.valence == 0.3
        assert b.arousal == -0.1
        assert b.dominance == 0.2

    @pytest.mark.parametrize("axis", ["valence", "arousal", "dominance"])
    def test_axis_below_minus_one_rejected(self, axis: str) -> None:
        with pytest.raises(ValidationError, match=axis):
            EmotionalBaselineConfig(**{axis: -1.1})

    @pytest.mark.parametrize("axis", ["valence", "arousal", "dominance"])
    def test_axis_above_plus_one_rejected(self, axis: str) -> None:
        with pytest.raises(ValidationError, match=axis):
            EmotionalBaselineConfig(**{axis: 1.1})

    def test_homeostasis_rate_bounds(self) -> None:
        EmotionalBaselineConfig(homeostasis_rate=0.0)
        EmotionalBaselineConfig(homeostasis_rate=1.0)
        with pytest.raises(ValidationError, match="homeostasis_rate"):
            EmotionalBaselineConfig(homeostasis_rate=1.1)
        with pytest.raises(ValidationError, match="homeostasis_rate"):
            EmotionalBaselineConfig(homeostasis_rate=-0.1)


class TestScoringConfig:
    """Scoring weight configuration (TASK-16)."""

    def test_defaults_valid(self) -> None:
        s = ScoringConfig()
        imp_sum = (
            s.importance_category
            + s.importance_llm
            + s.importance_emotional
            + s.importance_novelty
            + s.importance_explicit
        )
        conf_sum = (
            s.confidence_source
            + s.confidence_llm
            + s.confidence_explicitness
            + s.confidence_richness
        )
        assert abs(imp_sum - 1.0) < 0.001
        assert abs(conf_sum - 1.0) < 0.001

    def test_custom_weights_accepted(self) -> None:
        s = ScoringConfig(
            importance_category=0.20,
            importance_llm=0.30,
            importance_emotional=0.20,
            importance_novelty=0.10,
            importance_explicit=0.20,
        )
        assert s.importance_category == 0.20  # noqa: PLR2004

    def test_bad_importance_sum_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Importance weights must sum"):
            ScoringConfig(importance_category=0.50)  # Sum > 1.0

    def test_bad_confidence_sum_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Confidence weights must sum"):
            ScoringConfig(confidence_source=0.60)  # Sum > 1.0

    def test_brain_config_includes_scoring(self) -> None:
        b = BrainConfig()
        assert isinstance(b.scoring, ScoringConfig)
        assert b.scoring.importance_llm == 0.35  # noqa: PLR2004


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

    def test_voice_id_defaults_to_empty(self) -> None:
        """An unset ``voice_id`` signals "auto-pick from catalog" to the factory."""
        m = MindConfig(name="Aria")
        assert m.voice_id == ""

    def test_voice_id_explicit(self) -> None:
        """``voice_id`` is a free-form string — the factory validates against the catalog."""
        m = MindConfig(name="Aria", voice_id="pf_dora")
        assert m.voice_id == "pf_dora"


class TestPhase8VoiceIdentityFields:
    """Phase 8 / T8.1-T8.5 — per-mind voice identity surface.

    Pin the field defaults + derived-property contracts so:
    - Single-mind v0.30.0 deployments need no mind.yaml edits
      (effective_wake_word falls back to legacy 'Sovyx')
    - Operators opt-in via explicit field set
    - YAML round-trip is preserved (raw fields stay empty when not
      explicitly set; properties derive at read time)
    """

    # T8.1 — wake_word

    def test_wake_word_defaults_to_empty_string(self) -> None:
        """Default ``wake_word=""`` — preserves YAML round-trip."""
        m = MindConfig(name="Aria")
        assert m.wake_word == ""

    def test_effective_wake_word_falls_back_to_sovyx_when_empty(self) -> None:
        """Backward-compat: empty field → legacy global 'Sovyx'."""
        m = MindConfig(name="Aria")
        assert m.effective_wake_word == "Sovyx"

    def test_effective_wake_word_uses_explicit_field(self) -> None:
        """Operator opt-in: explicit field beats the fallback."""
        m = MindConfig(name="Lúcia", wake_word="Lúcia")
        assert m.effective_wake_word == "Lúcia"

    def test_wake_word_max_length_enforced(self) -> None:
        """Pydantic validation rejects wake words > 64 chars."""
        with pytest.raises(ValidationError):
            MindConfig(name="Aria", wake_word="X" * 65)

    # T8.2 — wake_word_variants

    def test_wake_word_variants_defaults_to_empty_list(self) -> None:
        """Default empty list — preserves YAML round-trip."""
        m = MindConfig(name="Aria")
        assert m.wake_word_variants == []

    def test_effective_variants_auto_derive_from_effective_wake_word(self) -> None:
        """Empty list → ``[<wake_lower>, hey <wake_lower>]``."""
        m = MindConfig(name="Aria")
        # effective_wake_word=Sovyx → variants=['sovyx', 'hey sovyx']
        assert m.effective_wake_word_variants == ["sovyx", "hey sovyx"]

    def test_effective_variants_strip_diacritics(self) -> None:
        """Latin-1 diacritics removed for STT robustness.

        STT engines commonly drop diacritics; matching them in the
        verifier requires the variants list to carry the ASCII form.
        """
        m = MindConfig(name="Lúcia", wake_word="Lúcia")
        assert "lucia" in m.effective_wake_word_variants
        assert "hey lucia" in m.effective_wake_word_variants

    def test_effective_variants_t816_full_matrix(self) -> None:
        """T8.16 — derived list covers (original × ASCII-fold) ×
        (bare × "hey") so STT engines that DO preserve diacritics
        also match. Lúcia → ["lúcia", "lucia", "hey lúcia",
        "hey lucia"]."""
        m = MindConfig(name="Lúcia", wake_word="Lúcia")
        variants = m.effective_wake_word_variants
        assert "lúcia" in variants  # original-form preserved
        assert "lucia" in variants  # ASCII-fold
        assert "hey lúcia" in variants
        assert "hey lucia" in variants
        assert len(variants) == 4  # noqa: PLR2004 — full matrix

    def test_effective_variants_ascii_only_dedupes_to_two(self) -> None:
        """Pure-ASCII names (Sovyx) collapse the matrix because
        original-lower == ascii-lower."""
        m = MindConfig(name="Aria")
        # effective_wake_word="Sovyx" → matrix ["sovyx", "sovyx",
        # "hey sovyx", "hey sovyx"] dedupes to 2.
        assert m.effective_wake_word_variants == ["sovyx", "hey sovyx"]

    def test_effective_variants_explicit_field_wins(self) -> None:
        """When operator sets the list, it's returned verbatim."""
        m = MindConfig(
            name="Lúcia",
            wake_word="Lúcia",
            wake_word_variants=["lúcia", "lucia", "lousha"],
        )
        assert m.effective_wake_word_variants == ["lúcia", "lucia", "lousha"]

    def test_effective_variants_returns_a_copy(self) -> None:
        """Mutating the returned list doesn't mutate the underlying field.

        Defensive immutability — caller-induced mutation of the
        returned list must not corrupt the MindConfig's state.
        """
        m = MindConfig(name="Aria")
        first = m.effective_wake_word_variants
        first.append("rogue")
        second = m.effective_wake_word_variants
        assert "rogue" not in second

    # T8.3 — voice_id (already existed; reaffirm)

    def test_voice_id_unchanged_by_phase8(self) -> None:
        """T8.3 reuses the existing voice_id field — no schema change."""
        m = MindConfig(name="Aria", voice_id="pf_dora")
        assert m.voice_id == "pf_dora"

    # T8.4 — voice_language + voice_accent

    def test_voice_language_defaults_to_empty(self) -> None:
        m = MindConfig(name="Aria")
        assert m.voice_language == ""

    def test_effective_voice_language_falls_back_to_top_level(self) -> None:
        """Empty voice_language → falls back to top-level language."""
        m = MindConfig(name="Aria", language="pt")
        assert m.effective_voice_language == "pt"

    def test_effective_voice_language_explicit_wins(self) -> None:
        m = MindConfig(name="Aria", language="pt", voice_language="en-US")
        assert m.effective_voice_language == "en-US"

    def test_voice_accent_defaults_to_empty(self) -> None:
        m = MindConfig(name="Aria")
        assert m.voice_accent == ""

    def test_voice_accent_max_length_enforced(self) -> None:
        with pytest.raises(ValidationError):
            MindConfig(name="Aria", voice_accent="X" * 33)

    # T8.5 — voice_cadence_wpm

    def test_voice_cadence_default_150(self) -> None:
        m = MindConfig(name="Aria")
        assert m.voice_cadence_wpm == 150

    def test_voice_cadence_lower_bound_50(self) -> None:
        """Bounds enforced — below 50 wpm sounds robotic."""
        with pytest.raises(ValidationError):
            MindConfig(name="Aria", voice_cadence_wpm=49)

    def test_voice_cadence_upper_bound_500(self) -> None:
        """Bounds enforced — above 500 wpm is unintelligible."""
        with pytest.raises(ValidationError):
            MindConfig(name="Aria", voice_cadence_wpm=501)

    def test_voice_cadence_at_bounds_accepted(self) -> None:
        """Exactly 50 + 500 are accepted (inclusive bounds)."""
        assert MindConfig(name="Aria", voice_cadence_wpm=50).voice_cadence_wpm == 50  # noqa: PLR2004
        assert MindConfig(name="Aria", voice_cadence_wpm=500).voice_cadence_wpm == 500  # noqa: PLR2004

    # Backward-compat — Phase 8 must not break existing minds

    def test_existing_minimal_config_still_works(self) -> None:
        """Pre-Phase-8 minds (no wake_word, no voice_language, etc.)
        load with defaults that preserve v0.30.0 behaviour."""
        m = MindConfig(name="Aria")
        # All Phase-8 fields default to safe values.
        assert m.wake_word == ""
        assert m.wake_word_variants == []
        assert m.voice_language == ""
        assert m.voice_accent == ""
        assert m.voice_cadence_wpm == 150
        # Effective accessors return v0.30.0-compatible values.
        assert m.effective_wake_word == "Sovyx"
        assert m.effective_voice_language == "en"  # falls back to language=en

    def test_yaml_round_trip_preserves_empty_sentinels(self) -> None:
        """Loading + re-serialising a minimal mind.yaml doesn't add
        the derived-default values to the file.

        Critical contract: the YAML round-trip must be idempotent.
        If the loader silently inserted ``wake_word: Sovyx`` into a
        previously-bare config, every restart would mutate the file
        and invalidate operator-managed git tracking. The empty-
        sentinel pattern + derived properties is the design that
        prevents this.
        """
        m = MindConfig(name="Aria")
        dumped = m.model_dump(exclude_unset=False)
        # Field is empty in the model (raw default), even though
        # effective_wake_word would return 'Sovyx'.
        assert dumped["wake_word"] == ""
        assert dumped["wake_word_variants"] == []


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


# ── PluginsConfig (TASK-434) ────────────────────────────────────────


class TestPluginConfigEntry:
    """Tests for PluginConfigEntry model."""

    def test_defaults(self) -> None:
        entry = PluginConfigEntry()
        assert entry.enabled is True
        assert entry.config == {}
        assert entry.permissions == []

    def test_custom_values(self) -> None:
        entry = PluginConfigEntry(
            enabled=False,
            config={"api_key": "abc123", "timeout": 30},
            permissions=["network:internet", "brain:read"],
        )
        assert entry.enabled is False
        assert entry.config["api_key"] == "abc123"
        assert len(entry.permissions) == 2


class TestPluginsConfig:
    """Tests for PluginsConfig model."""

    def test_defaults(self) -> None:
        p = PluginsConfig()
        assert p.enabled == []
        assert p.disabled == []
        assert p.plugins_config == {}
        assert p.tool_timeout_s == 30.0

    def test_effective_enabled_none_when_empty(self) -> None:
        """No enabled list → None (all plugins loaded)."""
        p = PluginsConfig()
        assert p.get_effective_enabled() is None

    def test_effective_enabled_whitelist(self) -> None:
        """Enabled list acts as whitelist."""
        p = PluginsConfig(enabled=["weather", "timer"])
        result = p.get_effective_enabled()
        assert result == {"weather", "timer"}

    def test_effective_enabled_minus_disabled(self) -> None:
        """Disabled overrides enabled."""
        p = PluginsConfig(enabled=["weather", "timer"], disabled=["timer"])
        result = p.get_effective_enabled()
        assert result == {"weather"}

    def test_effective_enabled_minus_per_plugin_disabled(self) -> None:
        """Per-plugin enabled=False overrides global enabled list."""
        p = PluginsConfig(
            enabled=["weather", "timer"],
            plugins_config={"timer": PluginConfigEntry(enabled=False)},
        )
        result = p.get_effective_enabled()
        assert result == {"weather"}

    def test_effective_disabled_combined(self) -> None:
        """Disabled combines global + per-plugin."""
        p = PluginsConfig(
            disabled={"weather"},
            plugins_config={"timer": PluginConfigEntry(enabled=False)},
        )
        result = p.get_effective_disabled()
        assert result == {"weather", "timer"}

    def test_get_plugin_config(self) -> None:
        p = PluginsConfig(
            plugins_config={
                "weather": PluginConfigEntry(config={"api_key": "test"}),
            },
        )
        assert p.get_plugin_config("weather") == {"api_key": "test"}

    def test_get_plugin_config_missing(self) -> None:
        p = PluginsConfig()
        assert p.get_plugin_config("unknown") == {}

    def test_get_all_plugin_configs(self) -> None:
        p = PluginsConfig(
            plugins_config={
                "weather": PluginConfigEntry(config={"key": "w"}),
                "timer": PluginConfigEntry(config={"key": "t"}),
                "empty": PluginConfigEntry(),
            },
        )
        result = p.get_all_plugin_configs()
        assert "weather" in result
        assert "timer" in result
        assert "empty" not in result  # Empty config excluded

    def test_get_granted_permissions(self) -> None:
        p = PluginsConfig(
            plugins_config={
                "weather": PluginConfigEntry(permissions=["network:internet"]),
            },
        )
        assert p.get_granted_permissions("weather") == {"network:internet"}

    def test_get_granted_permissions_empty(self) -> None:
        p = PluginsConfig()
        assert p.get_granted_permissions("unknown") == set()

    def test_get_all_granted_permissions(self) -> None:
        p = PluginsConfig(
            plugins_config={
                "weather": PluginConfigEntry(permissions=["network:internet"]),
                "timer": PluginConfigEntry(),  # No perms
            },
        )
        result = p.get_all_granted_permissions()
        assert "weather" in result
        assert "timer" not in result

    def test_tool_timeout_range(self) -> None:
        """Tool timeout must be in [1, 300]."""
        with pytest.raises(ValidationError):
            PluginsConfig(tool_timeout_s=0.5)
        with pytest.raises(ValidationError):
            PluginsConfig(tool_timeout_s=301)


class TestMindConfigPlugins:
    """Tests for plugins section in MindConfig."""

    def test_default_has_plugins(self) -> None:
        """MindConfig includes plugins section by default."""
        config = MindConfig(name="test")
        assert config.plugins is not None
        assert config.plugins.tool_timeout_s == 30.0

    def test_yaml_roundtrip(self, tmp_path: Path) -> None:
        """Plugins config survives YAML save/load."""
        yaml_content = """
name: test-mind
plugins:
  disabled:
    - dangerous-plugin
  plugins_config:
    weather:
      config:
        api_key: abc123
      permissions:
        - "network:internet"
    timer:
      enabled: false
"""
        path = tmp_path / "mind.yaml"
        path.write_text(yaml_content)
        config = load_mind_config(path)
        assert "dangerous-plugin" in config.plugins.disabled
        assert config.plugins.get_plugin_config("weather") == {"api_key": "abc123"}
        assert config.plugins.get_granted_permissions("weather") == {"network:internet"}
        assert not config.plugins.plugins_config["timer"].enabled

    def test_empty_plugins_section(self, tmp_path: Path) -> None:
        """Empty plugins section uses defaults."""
        yaml_content = "name: test\nplugins: {}\n"
        path = tmp_path / "mind.yaml"
        path.write_text(yaml_content)
        config = load_mind_config(path)
        assert config.plugins.enabled == []
        assert config.plugins.disabled == []


# ── validate_plugin_config (TASK-434) ───────────────────────────────


class TestValidatePluginConfig:
    """Tests for validate_plugin_config utility."""

    def test_valid_config(self) -> None:
        schema = {
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string"},
                "timeout": {"type": "integer"},
            },
        }
        errors = validate_plugin_config({"api_key": "abc", "timeout": 30}, schema)
        assert errors == []

    def test_missing_required(self) -> None:
        schema = {"required": ["api_key"]}
        errors = validate_plugin_config({}, schema)
        assert len(errors) == 1
        assert "api_key" in errors[0]

    def test_wrong_type(self) -> None:
        schema = {"properties": {"timeout": {"type": "integer"}}}
        errors = validate_plugin_config({"timeout": "not-int"}, schema)
        assert len(errors) == 1
        assert "timeout" in errors[0]

    def test_bool_not_integer(self) -> None:
        """JSON Schema: boolean is NOT integer."""
        schema = {"properties": {"count": {"type": "integer"}}}
        errors = validate_plugin_config({"count": True}, schema)
        assert len(errors) == 1

    def test_bool_not_number(self) -> None:
        schema = {"properties": {"rate": {"type": "number"}}}
        errors = validate_plugin_config({"rate": False}, schema)
        assert len(errors) == 1

    def test_number_accepts_int(self) -> None:
        schema = {"properties": {"rate": {"type": "number"}}}
        errors = validate_plugin_config({"rate": 42}, schema)
        assert errors == []

    def test_number_accepts_float(self) -> None:
        schema = {"properties": {"rate": {"type": "number"}}}
        errors = validate_plugin_config({"rate": 3.14}, schema)
        assert errors == []

    def test_unknown_type_accepted(self) -> None:
        """Unknown type string → no error."""
        schema = {"properties": {"x": {"type": "custom"}}}
        errors = validate_plugin_config({"x": "anything"}, schema)
        assert errors == []

    def test_empty_schema(self) -> None:
        errors = validate_plugin_config({"a": 1, "b": "c"}, {})
        assert errors == []

    def test_extra_fields_ignored(self) -> None:
        """Fields not in schema properties are not validated."""
        schema = {"properties": {"a": {"type": "string"}}}
        errors = validate_plugin_config({"a": "ok", "b": 123}, schema)
        assert errors == []

    def test_all_types(self) -> None:
        schema = {
            "properties": {
                "s": {"type": "string"},
                "i": {"type": "integer"},
                "n": {"type": "number"},
                "b": {"type": "boolean"},
                "a": {"type": "array"},
                "o": {"type": "object"},
            },
        }
        config = {"s": "hi", "i": 1, "n": 2.5, "b": True, "a": [1, 2], "o": {"k": "v"}}
        errors = validate_plugin_config(config, schema)
        assert errors == []

    def test_multiple_errors(self) -> None:
        schema = {
            "required": ["a", "b"],
            "properties": {"c": {"type": "integer"}},
        }
        errors = validate_plugin_config({"c": "nope"}, schema)
        assert len(errors) == 3  # 2 missing + 1 wrong type


class TestCheckJsonSchemaType:
    """Tests for _check_json_schema_type helper."""

    def test_string(self) -> None:
        assert _check_json_schema_type("hello", "string") is True
        assert _check_json_schema_type(123, "string") is False

    def test_integer(self) -> None:
        assert _check_json_schema_type(42, "integer") is True
        assert _check_json_schema_type(True, "integer") is False  # bool ≠ int

    def test_boolean(self) -> None:
        assert _check_json_schema_type(True, "boolean") is True
        assert _check_json_schema_type(1, "boolean") is False

    def test_array(self) -> None:
        assert _check_json_schema_type([1, 2], "array") is True
        assert _check_json_schema_type("nope", "array") is False

    def test_object(self) -> None:
        assert _check_json_schema_type({"k": "v"}, "object") is True
        assert _check_json_schema_type([1], "object") is False

    def test_unknown_type(self) -> None:
        assert _check_json_schema_type("anything", "custom") is True
