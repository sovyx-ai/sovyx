"""Tests for ``KokoroSampleSynthesizer`` — Phase 8 / T8.13.

Tests use a deterministic stub TTS engine — no real Kokoro ONNX
model is loaded. Stub returns synthetic int16 audio at the
configured sample rate (24 kHz Kokoro native or any other to test
resampling) so the synthesizer's plan-build + WAV-write +
resume-skip logic is exercised in isolation.

Coverage:

* Validation: target_count > 0, variants non-empty, speed_range
  bounded.
* Plan building: cartesian variant × voice × speed; cycle
  wrap-around when product < target_count.
* Synthesis happy path: WAV files written with deterministic
  filenames; sample_paths returned in order.
* Resume primitive: existing files skipped without re-synthesis.
* Cancellation: cancel_check returning True stops the loop;
  result reflects partial completion.
* Progress callback: invoked once per sample with index/total/message.
* Resampling: 24 kHz Kokoro native → 16 kHz target.
* Filename sanitisation: diacritics + non-alphanumerics replaced.
* TTS error handling: per-sample failures logged + skipped, don't
  crash the job.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from sovyx.voice.wake_word_training._synthesizer import (
    KokoroSampleSynthesizer,
    SynthesisRequest,
)

# ── Stub TTS ────────────────────────────────────────────────────────


class _StubAudioChunk:
    """Duck-types Kokoro's AudioChunk — has ``audio`` + ``sample_rate``."""

    def __init__(self, audio: np.ndarray, sample_rate: int) -> None:
        self.audio = audio
        self.sample_rate = sample_rate


class _StubTTS:
    """Deterministic TTS for tests — returns int16 sine at the
    requested speed × duration."""

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        duration_s: float = 0.5,
    ) -> None:
        self._sample_rate = sample_rate
        self._duration_s = duration_s
        self.calls: list[tuple[str, str, str, float]] = []

    async def synthesize_with(
        self,
        text: str,
        *,
        voice: str,
        language: str,
        speed: float | None = None,
    ) -> _StubAudioChunk:
        spd = speed if speed is not None else 1.0
        self.calls.append((text, voice, language, spd))
        n = int(self._duration_s * self._sample_rate)
        # Sine at 200 Hz, scaled to int16 range.
        t = np.arange(n) / self._sample_rate
        signal = (8000 * np.sin(2 * np.pi * 200 * t)).astype(np.int16)
        return _StubAudioChunk(signal, self._sample_rate)


class _ErroringTTS:
    """TTS that raises on the Nth call (1-indexed). Tests per-sample
    error tolerance."""

    def __init__(self, fail_on: int) -> None:
        self._fail_on = fail_on
        self._call_count = 0

    async def synthesize_with(
        self,
        text: str,  # noqa: ARG002
        *,
        voice: str,  # noqa: ARG002
        language: str,  # noqa: ARG002
        speed: float | None = None,  # noqa: ARG002
    ) -> _StubAudioChunk:
        self._call_count += 1
        if self._call_count == self._fail_on:
            msg = "fake TTS error"
            raise RuntimeError(msg)
        return _StubAudioChunk(
            np.zeros(8000, dtype=np.int16),
            sample_rate=16000,
        )


# ── Validation ──────────────────────────────────────────────────────


class TestValidation:
    @pytest.mark.asyncio
    async def test_zero_target_count_rejected(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        with pytest.raises(ValueError, match="target_count must be positive"):
            await synth.synthesize(
                SynthesisRequest(
                    wake_word="x",
                    variants=("x",),
                    language="en",
                    target_count=0,
                ),
                tmp_path,
            )

    @pytest.mark.asyncio
    async def test_negative_target_count_rejected(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        with pytest.raises(ValueError, match="target_count must be positive"):
            await synth.synthesize(
                SynthesisRequest(
                    wake_word="x",
                    variants=("x",),
                    language="en",
                    target_count=-1,
                ),
                tmp_path,
            )

    @pytest.mark.asyncio
    async def test_empty_variants_rejected(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        with pytest.raises(ValueError, match="variants must be non-empty"):
            await synth.synthesize(
                SynthesisRequest(
                    wake_word="x",
                    variants=(),
                    language="en",
                    target_count=1,
                ),
                tmp_path,
            )

    @pytest.mark.parametrize(
        "speed_range",
        [
            (0.4, 1.0),  # below 0.5 floor
            (1.0, 2.5),  # above 2.0 ceiling
            (1.2, 0.8),  # min > max
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_speed_range_rejected(
        self,
        tmp_path: Path,
        speed_range: tuple[float, float],
    ) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        with pytest.raises(ValueError, match="speed_range must satisfy"):
            await synth.synthesize(
                SynthesisRequest(
                    wake_word="x",
                    variants=("x",),
                    language="en",
                    target_count=1,
                    speed_range=speed_range,
                ),
                tmp_path,
            )


# ── Happy path ──────────────────────────────────────────────────────


class TestSynthesisHappyPath:
    @pytest.mark.asyncio
    async def test_writes_target_count_wavs(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="Lúcia",
                variants=("lucia", "hey lucia"),
                language="pt-BR",
                target_count=10,
                voices=("voice_a", "voice_b"),
                speed_range=(1.0, 1.0),  # collapses to one speed
            ),
            tmp_path,
        )
        assert result.completed_count == 10  # noqa: PLR2004
        assert len(result.sample_paths) == 10  # noqa: PLR2004
        assert not result.cancelled
        # All paths exist + are valid WAV files.
        for p in result.sample_paths:
            assert p.exists()
            with wave.open(str(p), "rb") as wf:
                assert wf.getnchannels() == 1
                assert wf.getsampwidth() == 2  # noqa: PLR2004 — int16
                assert wf.getframerate() == 16000  # noqa: PLR2004 — target rate

    @pytest.mark.asyncio
    async def test_filenames_are_deterministic(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hello",),
                language="en",
                target_count=2,
                voices=("voice_a",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        # Pattern: NNNN_variant_voice_speedX.XX.wav
        assert result.sample_paths[0].name.startswith("0001_hello_voice_a_speed")
        assert result.sample_paths[1].name.startswith("0002_hello_voice_a_speed")

    @pytest.mark.asyncio
    async def test_filename_sanitises_diacritics(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="Lúcia",
                variants=("Lúcia!",),  # diacritic + special char
                language="pt-BR",
                target_count=1,
                voices=("voice_a",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        # Diacritics + ! become "_" via the sanitiser; "lúcia!" → "l_cia_".
        # Lowercased, alphanumerics preserved.
        assert "lúcia" not in result.sample_paths[0].name  # original gone
        assert result.sample_paths[0].name.startswith("0001_")

    @pytest.mark.asyncio
    async def test_voices_cycle_for_progress_diversity(
        self,
        tmp_path: Path,
    ) -> None:
        """Voice rotation INNER-most so progress UI shows variety
        across consecutive samples."""
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=4,
                voices=("alpha", "beta"),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        # voices=2 × speeds=1 × variants=1 = 2 cycle entries.
        # Iteration order: alpha → beta → alpha → beta.
        names = [p.name for p in result.sample_paths]
        assert "alpha" in names[0]
        assert "beta" in names[1]
        assert "alpha" in names[2]
        assert "beta" in names[3]

    @pytest.mark.asyncio
    async def test_speed_grid_4_points(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=4,
                voices=("v",),
                speed_range=(0.85, 1.15),
            ),
            tmp_path,
        )
        # 4 evenly-spaced speeds: 0.85, 0.95, 1.05, 1.15.
        speeds_in_filenames = sorted(
            {n.name.split("speed")[1].split(".wav")[0] for n in result.sample_paths},
        )
        assert speeds_in_filenames == ["0.85", "0.95", "1.05", "1.15"]


# ── Resume primitive ────────────────────────────────────────────────


class TestResume:
    @pytest.mark.asyncio
    async def test_existing_files_skipped(self, tmp_path: Path) -> None:
        # First run: produce 4 samples.
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        first = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=4,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        first_count = len(first.sample_paths)
        assert first_count == 4  # noqa: PLR2004

        # Second run: same spec → all files exist, TTS NOT called again.
        tts2 = _StubTTS()
        synth2 = KokoroSampleSynthesizer(tts=tts2)
        second = await synth2.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=4,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        # All paths returned (existing files); TTS uncalled.
        assert len(second.sample_paths) == 4  # noqa: PLR2004
        assert tts2.calls == []  # zero TTS calls — fully resumed

    @pytest.mark.asyncio
    async def test_partial_resume_fills_gaps(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        # First run: produce 2 samples by setting cancel after 2.
        cancel_count = [0]

        def cancel_after_2() -> bool:
            cancel_count[0] += 1
            return cancel_count[0] > 2  # noqa: PLR2004

        first = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=4,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
            cancel_check=cancel_after_2,
        )
        assert first.cancelled
        assert first.completed_count == 2  # noqa: PLR2004

        # Second run: same spec without cancel → fills the gap.
        synth2 = KokoroSampleSynthesizer(tts=_StubTTS())
        second = await synth2.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=4,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        assert second.completed_count == 4  # noqa: PLR2004
        assert not second.cancelled


# ── Cancellation ────────────────────────────────────────────────────


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_check_stops_loop(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=10,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
            cancel_check=lambda: True,  # cancel immediately
        )
        assert result.cancelled
        assert result.completed_count == 0

    @pytest.mark.asyncio
    async def test_cancel_after_n_samples(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        n = [0]

        def cancel_after_3() -> bool:
            n[0] += 1
            return n[0] > 3  # noqa: PLR2004

        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=10,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
            cancel_check=cancel_after_3,
        )
        assert result.cancelled
        assert result.completed_count == 3  # noqa: PLR2004


# ── Progress callback ───────────────────────────────────────────────


class TestProgress:
    @pytest.mark.asyncio
    async def test_progress_called_per_sample(self, tmp_path: Path) -> None:
        synth = KokoroSampleSynthesizer(tts=_StubTTS())
        events: list[tuple[int, int, str]] = []

        def on_progress(idx: int, total: int, msg: str) -> None:
            events.append((idx, total, msg))

        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=3,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
            on_progress=on_progress,
        )
        assert result.completed_count == 3  # noqa: PLR2004
        # 3 events, one per sample, in order.
        assert len(events) == 3  # noqa: PLR2004
        assert events[0][0] == 1
        assert events[2][0] == 3  # noqa: PLR2004
        assert all(e[1] == 3 for e in events)  # total stable across calls  # noqa: PLR2004


# ── Resampling ──────────────────────────────────────────────────────


class TestResampling:
    @pytest.mark.asyncio
    async def test_24k_native_resampled_to_16k(self, tmp_path: Path) -> None:
        # Stub returns 24 kHz; synthesizer must resample to 16 kHz.
        synth = KokoroSampleSynthesizer(
            tts=_StubTTS(sample_rate=24000, duration_s=0.5),
        )
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=1,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        with wave.open(str(result.sample_paths[0]), "rb") as wf:
            assert wf.getframerate() == 16000  # noqa: PLR2004
            # 0.5 s × 16 kHz = 8000 frames (within ±1 from rounding).
            assert abs(wf.getnframes() - 8000) <= 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_16k_native_passthrough(self, tmp_path: Path) -> None:
        # Stub returns 16 kHz; no resampling needed.
        synth = KokoroSampleSynthesizer(
            tts=_StubTTS(sample_rate=16000, duration_s=0.5),
        )
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=1,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        with wave.open(str(result.sample_paths[0]), "rb") as wf:
            assert wf.getframerate() == 16000  # noqa: PLR2004
            assert wf.getnframes() == 8000  # noqa: PLR2004


# ── TTS error handling ─────────────────────────────────────────────


class TestTTSErrors:
    @pytest.mark.asyncio
    async def test_per_sample_error_skipped_continue(self, tmp_path: Path) -> None:
        """One TTS call raises; synthesizer skips that slot + continues."""
        synth = KokoroSampleSynthesizer(tts=_ErroringTTS(fail_on=2))
        result = await synth.synthesize(
            SynthesisRequest(
                wake_word="x",
                variants=("hi",),
                language="en",
                target_count=4,
                voices=("v",),
                speed_range=(1.0, 1.0),
            ),
            tmp_path,
        )
        # 4 attempts, 1 failed, 3 written.
        assert result.completed_count == 3  # noqa: PLR2004
        assert not result.cancelled
