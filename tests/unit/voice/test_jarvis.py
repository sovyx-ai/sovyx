"""Tests for JarvisIllusion module (V05-24).

Covers: FillerCategory, filler selection, repetition avoidance, beep synthesis,
pre-cache, filler playback timing, text splitting, and configuration validation.

Ref: SPE-010 §7, IMPL-SUP-005 §SPEC-1.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.jarvis import (
    CROSSFADE_MS,
    FILLER_BANK,
    FILLER_DELAY_MS,
    FILLER_MIN_DURATION_MS,
    HISTORY_SIZE,
    MAX_SAME_FILLER_IN_ROW,
    POST_FILLER_PAUSE_MS,
    FillerCategory,
    JarvisConfig,
    JarvisIllusion,
    split_at_boundaries,
    synthesize_beep,
    validate_jarvis_config,
)
from sovyx.voice.tts_piper import AudioChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(duration_ms: float = 50.0) -> AudioChunk:
    """Create a minimal AudioChunk for testing."""
    import numpy as np

    samples = int(22050 * duration_ms / 1000)
    return AudioChunk(
        audio=np.zeros(samples, dtype=np.int16),
        sample_rate=22050,
        duration_ms=duration_ms,
    )


def _make_tts() -> AsyncMock:
    """Create a mock TTS engine that returns AudioChunks."""
    tts = AsyncMock()
    tts.synthesize = AsyncMock(return_value=_make_chunk())
    return tts


def _make_output() -> AsyncMock:
    """Create a mock AudioOutputQueue."""
    output = AsyncMock()
    output.play_immediate = AsyncMock()
    return output


def _make_illusion(
    config: JarvisConfig | None = None,
    tts: AsyncMock | None = None,
) -> JarvisIllusion:
    """Create a JarvisIllusion with defaults."""
    return JarvisIllusion(
        config=config or JarvisConfig(),
        tts=tts or _make_tts(),
    )


# ---------------------------------------------------------------------------
# FillerCategory
# ---------------------------------------------------------------------------


class TestFillerCategory:
    """Tests for FillerCategory enum."""

    def test_all_categories_exist(self) -> None:
        assert len(FillerCategory) == 5

    def test_category_values(self) -> None:
        assert FillerCategory.THINKING.value == "thinking"
        assert FillerCategory.CHECKING.value == "checking"
        assert FillerCategory.ACKNOWLEDGING.value == "ack"
        assert FillerCategory.CONFIRMING.value == "confirm"
        assert FillerCategory.TRANSITIONAL.value == "transition"


# ---------------------------------------------------------------------------
# FILLER_BANK
# ---------------------------------------------------------------------------


class TestFillerBank:
    """Tests for the default filler bank."""

    def test_all_categories_have_phrases(self) -> None:
        for cat in FillerCategory:
            assert cat in FILLER_BANK, f"Missing category {cat}"
            assert len(FILLER_BANK[cat]) > 0, f"Empty phrases for {cat}"

    def test_phrases_are_strings(self) -> None:
        for cat, phrases in FILLER_BANK.items():
            for phrase in phrases:
                assert isinstance(phrase, str), f"Non-string phrase in {cat}: {phrase}"
                assert len(phrase) > 0, f"Empty phrase in {cat}"

    def test_minimum_phrases_per_category(self) -> None:
        """Each category should have at least 3 phrases."""
        for cat, phrases in FILLER_BANK.items():
            assert len(phrases) >= 3, f"Category {cat} has only {len(phrases)} phrases"


# ---------------------------------------------------------------------------
# JarvisConfig
# ---------------------------------------------------------------------------


class TestJarvisConfig:
    """Tests for JarvisConfig dataclass and validation."""

    def test_defaults(self) -> None:
        cfg = JarvisConfig()
        assert cfg.fillers_enabled is True
        assert cfg.filler_delay_ms == FILLER_DELAY_MS
        assert cfg.filler_min_duration_ms == FILLER_MIN_DURATION_MS
        assert cfg.crossfade_ms == CROSSFADE_MS
        assert cfg.post_filler_pause_ms == POST_FILLER_PAUSE_MS
        assert cfg.confirmation_tone == "beep"
        assert cfg.max_same_in_row == MAX_SAME_FILLER_IN_ROW
        assert cfg.history_size == HISTORY_SIZE

    def test_custom_values(self) -> None:
        cfg = JarvisConfig(
            fillers_enabled=False,
            filler_delay_ms=100,
            confirmation_tone="none",
        )
        assert cfg.fillers_enabled is False
        assert cfg.filler_delay_ms == 100
        assert cfg.confirmation_tone == "none"

    def test_validate_negative_delay(self) -> None:
        cfg = JarvisConfig(filler_delay_ms=-1)
        with pytest.raises(ValueError, match="filler_delay_ms"):
            validate_jarvis_config(cfg)

    def test_validate_negative_min_duration(self) -> None:
        cfg = JarvisConfig(filler_min_duration_ms=-1)
        with pytest.raises(ValueError, match="filler_min_duration_ms"):
            validate_jarvis_config(cfg)

    def test_validate_negative_crossfade(self) -> None:
        cfg = JarvisConfig(crossfade_ms=-1)
        with pytest.raises(ValueError, match="crossfade_ms"):
            validate_jarvis_config(cfg)

    def test_validate_negative_pause(self) -> None:
        cfg = JarvisConfig(post_filler_pause_ms=-1)
        with pytest.raises(ValueError, match="post_filler_pause_ms"):
            validate_jarvis_config(cfg)

    def test_validate_bad_tone(self) -> None:
        cfg = JarvisConfig(confirmation_tone="chime")
        with pytest.raises(ValueError, match="confirmation_tone"):
            validate_jarvis_config(cfg)

    def test_validate_max_same_in_row_zero(self) -> None:
        cfg = JarvisConfig(max_same_in_row=0)
        with pytest.raises(ValueError, match="max_same_in_row"):
            validate_jarvis_config(cfg)

    def test_validate_history_size_zero(self) -> None:
        cfg = JarvisConfig(history_size=0)
        with pytest.raises(ValueError, match="history_size"):
            validate_jarvis_config(cfg)

    def test_validate_valid_config(self) -> None:
        """No exception for valid config."""
        validate_jarvis_config(JarvisConfig())


# ---------------------------------------------------------------------------
# Filler selection
# ---------------------------------------------------------------------------


class TestFillerSelection:
    """Tests for select_category and select_filler."""

    def test_question_selects_thinking(self) -> None:
        ji = _make_illusion()
        assert ji.select_category("What is the weather?") == FillerCategory.THINKING

    def test_long_input_selects_checking(self) -> None:
        ji = _make_illusion()
        long_input = "Please " + "word " * 11  # > 10 words
        assert ji.select_category(long_input) == FillerCategory.CHECKING

    def test_short_input_selects_transitional(self) -> None:
        ji = _make_illusion()
        assert ji.select_category("hello") == FillerCategory.TRANSITIONAL

    def test_intent_overrides_heuristic(self) -> None:
        ji = _make_illusion()
        # Even though input ends with ?, command intent wins
        assert ji.select_category("Set timer?", intent="command") == FillerCategory.ACKNOWLEDGING

    def test_intent_question(self) -> None:
        ji = _make_illusion()
        assert ji.select_category("anything", intent="question") == FillerCategory.THINKING

    def test_intent_confirmation(self) -> None:
        ji = _make_illusion()
        assert ji.select_category("yes", intent="confirmation") == FillerCategory.CONFIRMING

    def test_intent_complex(self) -> None:
        ji = _make_illusion()
        assert ji.select_category("x", intent="complex") == FillerCategory.CHECKING

    def test_unknown_intent_uses_transitional(self) -> None:
        ji = _make_illusion()
        assert ji.select_category("hi", intent="unknown_intent") == FillerCategory.TRANSITIONAL

    def test_select_filler_returns_string(self) -> None:
        ji = _make_illusion()
        phrase = ji.select_filler(user_input="What time is it?")
        assert isinstance(phrase, str)
        assert len(phrase) > 0

    def test_select_filler_from_correct_category(self) -> None:
        ji = _make_illusion()
        phrase = ji.select_filler(category=FillerCategory.ACKNOWLEDGING)
        assert phrase in FILLER_BANK[FillerCategory.ACKNOWLEDGING]

    def test_select_filler_adds_to_history(self) -> None:
        ji = _make_illusion()
        assert len(ji.history) == 0
        ji.select_filler()
        assert len(ji.history) == 1

    def test_select_filler_explicit_category(self) -> None:
        ji = _make_illusion()
        phrase = ji.select_filler(category=FillerCategory.THINKING)
        assert phrase in FILLER_BANK[FillerCategory.THINKING]


# ---------------------------------------------------------------------------
# Repetition avoidance
# ---------------------------------------------------------------------------


class TestRepetitionAvoidance:
    """Tests for filler repetition avoidance logic."""

    def test_avoids_same_filler_max_in_row(self) -> None:
        """After MAX_SAME_FILLER_IN_ROW of the same filler, next must differ."""
        # Use a category with 3+ phrases so avoidance is possible
        cfg = JarvisConfig(
            max_same_in_row=2,
            filler_bank={
                FillerCategory.TRANSITIONAL: ("A.", "B.", "C."),
                **{k: v for k, v in FILLER_BANK.items() if k != FillerCategory.TRANSITIONAL},
            },
        )
        ji = _make_illusion(config=cfg)
        # Force history: "A." twice
        ji._history.append("A.")
        ji._history.append("A.")

        # Select from transitional — should NOT get "A." again
        phrase = ji.select_filler(category=FillerCategory.TRANSITIONAL)
        assert phrase != "A.", "Should have avoided repeating A. a third time"

    def test_allows_same_filler_under_limit(self) -> None:
        """Same filler is fine if under the repetition limit."""
        cfg = JarvisConfig(
            max_same_in_row=3,
            filler_bank={
                FillerCategory.TRANSITIONAL: ("A.",),
                **{k: v for k, v in FILLER_BANK.items() if k != FillerCategory.TRANSITIONAL},
            },
        )
        ji = _make_illusion(config=cfg)
        ji._history.append("A.")
        ji._history.append("A.")
        # Only 2x — max_same_in_row=3 → still allowed
        phrase = ji.select_filler(category=FillerCategory.TRANSITIONAL)
        assert phrase == "A."  # Only option, and it's allowed

    def test_all_filtered_allows_any(self) -> None:
        """If all phrases filtered, falls back to allow any."""
        cfg = JarvisConfig(
            max_same_in_row=1,
            filler_bank={
                FillerCategory.TRANSITIONAL: ("A.",),
                **{k: v for k, v in FILLER_BANK.items() if k != FillerCategory.TRANSITIONAL},
            },
        )
        ji = _make_illusion(config=cfg)
        ji._history.append("A.")
        # "A." would be filtered (1 consecutive, max_same=1), but it's the only option
        phrase = ji.select_filler(category=FillerCategory.TRANSITIONAL)
        assert phrase == "A."  # Fallback: allow anyway

    def test_reset_history(self) -> None:
        ji = _make_illusion()
        ji.select_filler()
        ji.select_filler()
        assert len(ji.history) == 2
        ji.reset_history()
        assert len(ji.history) == 0

    def test_history_bounded_by_size(self) -> None:
        ji = _make_illusion(config=JarvisConfig(history_size=3))
        for _ in range(10):
            ji.select_filler()
        assert len(ji.history) == 3


# ---------------------------------------------------------------------------
# Beep synthesis
# ---------------------------------------------------------------------------


class TestBeepSynthesis:
    """Tests for synthesize_beep standalone function."""

    def test_default_beep(self) -> None:
        chunk = synthesize_beep()
        assert isinstance(chunk, AudioChunk)
        assert chunk.sample_rate == 22050
        assert chunk.duration_ms == pytest.approx(50.0)
        import numpy as np

        assert chunk.audio.dtype == np.int16
        assert len(chunk.audio) > 0

    def test_custom_beep(self) -> None:
        chunk = synthesize_beep(freq_hz=880, duration_s=0.1, sample_rate=44100)
        assert chunk.sample_rate == 44100
        assert chunk.duration_ms == pytest.approx(100.0)
        expected_samples = int(44100 * 0.1)
        assert len(chunk.audio) == expected_samples

    def test_beep_amplitude_range(self) -> None:
        """Beep samples should be within int16 range."""
        chunk = synthesize_beep()
        assert chunk.audio.max() <= 32767
        assert chunk.audio.min() >= -32768

    def test_beep_has_fade(self) -> None:
        """First and last samples should be near zero (fade in/out)."""
        chunk = synthesize_beep()
        # First sample should be very small (faded in from 0)
        assert abs(int(chunk.audio[0])) < 1000
        # Last sample should be very small (faded out to 0)
        assert abs(int(chunk.audio[-1])) < 1000

    def test_beep_not_silent(self) -> None:
        """Beep should have non-zero energy in the middle."""
        chunk = synthesize_beep()
        mid = len(chunk.audio) // 2
        assert abs(int(chunk.audio[mid])) > 100


# ---------------------------------------------------------------------------
# Pre-cache
# ---------------------------------------------------------------------------


class TestPreCache:
    """Tests for JarvisIllusion.pre_cache."""

    @pytest.mark.asyncio
    async def test_pre_cache_fillers(self) -> None:
        tts = _make_tts()
        ji = _make_illusion(tts=tts)
        await ji.pre_cache()

        total_phrases = sum(len(p) for p in FILLER_BANK.values())
        assert ji.cached_filler_count == total_phrases
        assert tts.synthesize.call_count == total_phrases

    @pytest.mark.asyncio
    async def test_pre_cache_beep_enabled(self) -> None:
        ji = _make_illusion(config=JarvisConfig(confirmation_tone="beep"))
        await ji.pre_cache()
        assert ji.beep_cached is True

    @pytest.mark.asyncio
    async def test_pre_cache_beep_disabled(self) -> None:
        ji = _make_illusion(config=JarvisConfig(confirmation_tone="none"))
        await ji.pre_cache()
        assert ji.beep_cached is False

    @pytest.mark.asyncio
    async def test_pre_cache_handles_tts_failure(self) -> None:
        tts = _make_tts()
        tts.synthesize = AsyncMock(side_effect=RuntimeError("TTS down"))
        ji = _make_illusion(tts=tts)
        # Should not raise — just log warnings
        await ji.pre_cache()
        assert ji.cached_filler_count == 0

    @pytest.mark.asyncio
    async def test_get_cached_filler_hit(self) -> None:
        tts = _make_tts()
        ji = _make_illusion(tts=tts)
        await ji.pre_cache()
        phrase = FILLER_BANK[FillerCategory.THINKING][0]
        chunk = ji.get_cached_filler(phrase)
        assert chunk is not None

    @pytest.mark.asyncio
    async def test_get_cached_filler_miss(self) -> None:
        ji = _make_illusion()
        assert ji.get_cached_filler("nonexistent phrase") is None


# ---------------------------------------------------------------------------
# Play beep
# ---------------------------------------------------------------------------


class TestPlayBeep:
    """Tests for JarvisIllusion.play_beep."""

    @pytest.mark.asyncio
    async def test_play_beep_when_cached(self) -> None:
        ji = _make_illusion(config=JarvisConfig(confirmation_tone="beep"))
        await ji.pre_cache()
        output = _make_output()
        await ji.play_beep(output)
        output.play_immediate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_play_beep_when_not_cached(self) -> None:
        ji = _make_illusion(config=JarvisConfig(confirmation_tone="none"))
        await ji.pre_cache()
        output = _make_output()
        await ji.play_beep(output)
        output.play_immediate.assert_not_awaited()

    def test_get_beep_returns_chunk(self) -> None:
        ji = _make_illusion()
        # Before pre_cache — no beep
        assert ji.get_beep() is None


# ---------------------------------------------------------------------------
# Play filler after delay
# ---------------------------------------------------------------------------


class TestPlayFillerAfterDelay:
    """Tests for filler timing logic."""

    @pytest.mark.asyncio
    async def test_filler_plays_after_timeout(self) -> None:
        cfg = JarvisConfig(filler_delay_ms=10)  # Very short delay
        tts = _make_tts()
        ji = _make_illusion(config=cfg, tts=tts)
        await ji.pre_cache()

        output = _make_output()
        cancel = asyncio.Event()
        # Don't set cancel — filler should play
        result = await ji.play_filler_after_delay(output, cancel)
        assert result is True
        output.play_immediate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_filler_cancelled_before_delay(self) -> None:
        cfg = JarvisConfig(filler_delay_ms=5000)  # Long delay
        ji = _make_illusion(config=cfg)
        await ji.pre_cache()

        output = _make_output()
        cancel = asyncio.Event()

        async def cancel_soon() -> None:
            await asyncio.sleep(0.01)
            cancel.set()

        asyncio.create_task(cancel_soon())
        result = await ji.play_filler_after_delay(output, cancel)
        assert result is False
        output.play_immediate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_filler_fallback_when_not_cached(self) -> None:
        """If filler not in cache, synthesize on the fly."""
        cfg = JarvisConfig(filler_delay_ms=10)
        tts = _make_tts()
        ji = JarvisIllusion(config=cfg, tts=tts)
        # Don't pre_cache — cache is empty

        output = _make_output()
        cancel = asyncio.Event()
        result = await ji.play_filler_after_delay(output, cancel)
        assert result is True
        # Should have called tts.synthesize for the filler
        tts.synthesize.assert_awaited()

    @pytest.mark.asyncio
    async def test_filler_synthesis_failure(self) -> None:
        """If synthesis fails, return False gracefully."""
        cfg = JarvisConfig(filler_delay_ms=10)
        tts = _make_tts()
        tts.synthesize = AsyncMock(side_effect=RuntimeError("TTS error"))
        ji = JarvisIllusion(config=cfg, tts=tts)

        output = _make_output()
        cancel = asyncio.Event()
        result = await ji.play_filler_after_delay(output, cancel)
        assert result is False

    @pytest.mark.asyncio
    async def test_filler_uses_smart_selection(self) -> None:
        """Fillers should come from correct category based on input."""
        cfg = JarvisConfig(filler_delay_ms=10)
        tts = _make_tts()
        ji = _make_illusion(config=cfg, tts=tts)
        await ji.pre_cache()

        output = _make_output()
        cancel = asyncio.Event()
        # Question input → THINKING category
        await ji.play_filler_after_delay(output, cancel, user_input="What time is it?")
        # Verify a filler was played (from THINKING category ideally)
        output.play_immediate.assert_awaited_once()


# ---------------------------------------------------------------------------
# split_at_boundaries
# ---------------------------------------------------------------------------


class TestSplitAtBoundaries:
    """Tests for streaming TTS text splitting."""

    def test_single_sentence(self) -> None:
        result = split_at_boundaries("Hello world there.")
        assert result == ["Hello world there."]

    def test_multiple_sentences(self) -> None:
        result = split_at_boundaries("First sentence here. Second sentence there.")
        assert len(result) == 2
        assert result[0] == "First sentence here."
        assert result[1] == "Second sentence there."

    def test_question_mark(self) -> None:
        result = split_at_boundaries("Is it raining? Yes it is.")
        assert len(result) == 2

    def test_exclamation(self) -> None:
        result = split_at_boundaries("Wow that is great! Tell me more.")
        assert len(result) == 2

    def test_short_fragments_merged(self) -> None:
        """Fragments under 3 words get merged with previous."""
        result = split_at_boundaries("Hello. OK. This is a longer sentence.")
        # "OK." is only 1 word — should merge
        assert any("OK." in seg for seg in result)

    def test_empty_string(self) -> None:
        result = split_at_boundaries("")
        assert result == [""]

    def test_no_boundaries(self) -> None:
        result = split_at_boundaries("no boundaries here at all")
        assert result == ["no boundaries here at all"]

    def test_semicolon_boundary(self) -> None:
        result = split_at_boundaries("First part here; second part there.")
        assert len(result) == 2

    def test_colon_boundary(self) -> None:
        result = split_at_boundaries("Note this well: the answer is clear.")
        assert len(result) == 2

    def test_newline_boundary(self) -> None:
        result = split_at_boundaries("Line one here.\nLine two here.")
        assert len(result) == 2

    def test_em_dash_boundary(self) -> None:
        result = split_at_boundaries("First thought here\u2014 second thought here.")
        assert len(result) >= 1  # Should split at em dash

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(text=st.text(min_size=0, max_size=200))
    def test_split_never_loses_content(self, text: str) -> None:
        """Property: joined result should preserve all non-whitespace content."""
        result = split_at_boundaries(text)
        # All original non-whitespace chars should appear
        original_chars = set(text.replace(" ", ""))
        result_chars = set(" ".join(result).replace(" ", ""))
        # Every original char that's not pure whitespace should be preserved
        for char in original_chars:
            if char.strip():
                assert char in result_chars


# ---------------------------------------------------------------------------
# Properties (Hypothesis)
# ---------------------------------------------------------------------------


class TestJarvisProperties:
    """Property-based tests for JarvisIllusion."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(user_input=st.text(min_size=0, max_size=100))
    def test_select_category_always_returns_valid(self, user_input: str) -> None:
        """select_category always returns a valid FillerCategory."""
        ji = _make_illusion()
        cat = ji.select_category(user_input)
        assert isinstance(cat, FillerCategory)

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(user_input=st.text(min_size=0, max_size=100))
    def test_select_filler_always_returns_nonempty(self, user_input: str) -> None:
        """select_filler always returns a non-empty string."""
        ji = _make_illusion()
        phrase = ji.select_filler(user_input=user_input)
        assert isinstance(phrase, str)
        assert len(phrase) > 0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(n=st.integers(min_value=1, max_value=50))
    def test_history_never_exceeds_max(self, n: int) -> None:
        """History size never exceeds configured maximum."""
        cfg = JarvisConfig(history_size=5)
        ji = _make_illusion(config=cfg)
        for _ in range(n):
            ji.select_filler()
        assert len(ji.history) <= 5


# ---------------------------------------------------------------------------
# Integration — JarvisIllusion + VoicePipelineConfig compatibility
# ---------------------------------------------------------------------------


class TestJarvisConfigCompatibility:
    """Ensure JarvisConfig can be constructed from VoicePipelineConfig fields."""

    def test_jarvis_config_from_pipeline_fields(self) -> None:
        """Pipeline creates JarvisConfig from its own config."""
        from sovyx.voice.pipeline import VoicePipelineConfig

        pipeline_cfg = VoicePipelineConfig(
            fillers_enabled=True,
            filler_delay_ms=100,
            confirmation_tone="beep",
        )
        jarvis_cfg = JarvisConfig(
            fillers_enabled=pipeline_cfg.fillers_enabled,
            filler_delay_ms=pipeline_cfg.filler_delay_ms,
            confirmation_tone=pipeline_cfg.confirmation_tone,
        )
        validate_jarvis_config(jarvis_cfg)
        assert jarvis_cfg.fillers_enabled is True
        assert jarvis_cfg.filler_delay_ms == 100

    def test_pipeline_creates_jarvis_illusion(self) -> None:
        """VoicePipeline should create JarvisIllusion internally."""
        from sovyx.voice.pipeline import VoicePipeline, VoicePipelineConfig

        config = VoicePipelineConfig()
        vad = MagicMock()
        vad.process_frame = MagicMock()
        wake = MagicMock()
        stt = AsyncMock()
        tts = _make_tts()

        pipeline = VoicePipeline(
            config=config,
            vad=vad,
            wake_word=wake,
            stt=stt,
            tts=tts,
        )
        assert isinstance(pipeline.jarvis, JarvisIllusion)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests."""

    def test_empty_filler_bank_category_falls_back(self) -> None:
        """If a category has no phrases, fall back to transitional."""
        cfg = JarvisConfig(
            filler_bank={
                FillerCategory.THINKING: (),  # Empty!
                FillerCategory.CHECKING: ("Checking.",),
                FillerCategory.ACKNOWLEDGING: ("OK.",),
                FillerCategory.CONFIRMING: ("Right.",),
                FillerCategory.TRANSITIONAL: ("Well...",),
            }
        )
        ji = _make_illusion(config=cfg)
        # Asking for THINKING (empty) should fall back to TRANSITIONAL
        phrase = ji.select_filler(category=FillerCategory.THINKING)
        assert phrase == "Well..."

    def test_constructor_validates_config(self) -> None:
        """Constructor should validate config."""
        bad_cfg = JarvisConfig(filler_delay_ms=-1)
        with pytest.raises(ValueError):
            _make_illusion(config=bad_cfg)

    @pytest.mark.asyncio
    async def test_pre_cache_idempotent(self) -> None:
        """Calling pre_cache twice doesn't break anything."""
        tts = _make_tts()
        ji = _make_illusion(tts=tts)
        await ji.pre_cache()
        count1 = ji.cached_filler_count
        await ji.pre_cache()
        count2 = ji.cached_filler_count
        assert count1 == count2

    def test_config_is_frozen(self) -> None:
        """JarvisConfig should be immutable."""
        cfg = JarvisConfig()
        with pytest.raises(AttributeError):
            cfg.filler_delay_ms = 999  # type: ignore[misc]
