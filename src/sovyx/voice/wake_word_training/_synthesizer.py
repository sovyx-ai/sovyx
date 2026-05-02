"""Kokoro-based positive sample synthesizer — Phase 8 / T8.13.

Generates the **positive sample set** for a custom wake-word
training job: dozens-to-hundreds of WAV files containing the
target wake word spoken by varied Kokoro voices, at varied speeds,
written to a job directory the trainer backend reads.

Why Kokoro:
* Already a Sovyx dep (``[voice]`` extras) — no new deps for
  synthesis.
* 54+ voices across multiple languages — the variety matters for
  generalisation: a wake-word ONNX trained against a single voice
  overfits to that voice's pitch / cadence / accent.
* Speed parameter (``[0.5, 2.0]``) gives free augmentation without
  separate audio-processing code.

What this module does NOT do:
* **No training.** That's the trainer backend's job (the
  ``TrainerBackend`` Protocol from ``_trainer_protocol.py``).
* **No negative samples.** Negative samples come from a bundled
  noise dataset + non-wake-word utterances; that's a separate
  module (deferred to the real-backend mini-mission).
* **No augmentation beyond Kokoro's variety + speed.** Background
  noise mixing, reverb, EQ are nice-to-haves the trainer backend
  can apply on top.

Architecture:
* ``KokoroSampleSynthesizer`` is async (Kokoro ``synthesize_with``
  is async; running synchronously would block the event loop).
* Uses dependency injection: tests pass a stub TTS that returns
  deterministic int16 audio without loading the real ONNX model.
* Resamples 24 kHz → 16 kHz (the wake-word target) via
  scipy.signal.resample if available, else a linear-interp
  fallback (no extra deps; same quality as the existing
  ``voice/audio.py`` resampler for the wizard test-record).

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.13. Operator debt:
``OPERATOR-DEBT-MASTER-2026-05-01.md`` D10 (training-pipeline
mini-mission).
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import numpy.typing as npt


logger = get_logger(__name__)


_TARGET_SAMPLE_RATE = 16000
"""Wake-word ONNX models all consume 16 kHz mono PCM. Synthesizer
output is resampled to this rate so the trainer backend can feed
the WAVs directly without further conversion."""


_KOKORO_NATIVE_RATE = 24000
"""Kokoro ONNX outputs at 24 kHz; we always resample down. Hardcoded
because the existing ``KokoroTTS.sample_rate`` property is a constant
property on the engine — no per-utterance variation."""


_DEFAULT_SPEED_RANGE: tuple[float, float] = (0.85, 1.15)
"""Speed augmentation range. ±15% gives meaningful pitch + duration
variation without straying into "comically fast / slow" territory.
Empirically chosen — operators with non-Latin names should stay
inside this range; speeds < 0.7 or > 1.4 produce robotic artefacts
that hurt training rather than help."""


_DEFAULT_VOICES: tuple[str, ...] = (
    # Top-of-list English Kokoro voices spanning F + M + neutral
    # with different speaking styles. Operators with non-English
    # wake words override via the ``voices`` constructor parameter.
    # See `kokoro-onnx` docs for the full 54-voice catalogue.
    "af_heart",
    "af_alloy",
    "af_aoede",
    "af_bella",
    "am_adam",
    "am_eric",
    "am_michael",
    "am_onyx",
)
"""Default voice IDs used when the operator doesn't override.
8 voices × 3-4 speeds × ~5 variant phrases = ~100-150 samples in
a default training job, before any augmentation."""


# ── TTS dependency injection ────────────────────────────────────────


@runtime_checkable
class _TTSEngine(Protocol):
    """Minimal Kokoro-shaped TTS interface for dependency injection.

    Production wires the real ``KokoroTTS`` from ``voice/tts_kokoro.py``;
    tests inject a stub that returns deterministic int16 audio
    without loading the ONNX model. Matches Kokoro's
    ``synthesize_with`` signature exactly so the production wiring is
    a one-line ``synthesizer = KokoroSampleSynthesizer(tts=kokoro_tts)``.
    """

    async def synthesize_with(
        self,
        text: str,
        *,
        voice: str,
        language: str,
        speed: float | None = None,
    ) -> object:
        """Return an audio chunk. Production returns
        ``KokoroTTS.AudioChunk``; the synthesizer reads ``.audio``
        (int16 ndarray) + ``.sample_rate`` (int) via duck-typing."""
        ...


# ── Public dataclasses ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """Spec for one positive-sample-generation job.

    Attributes:
        wake_word: The original wake word (with diacritics intact).
        variants: Phrases to synthesise. Typically the operator's
            ``MindConfig.effective_wake_word_variants`` extended
            via ``expand_wake_word_variants`` (T8.16). Each variant
            is rendered with multiple voices × speeds.
        language: BCP-47 tag passed to Kokoro for G2P.
        target_count: Total samples to produce. Synthesizer cycles
            through ``(variant × voice × speed)`` combinations until
            the count is reached. When the cartesian product is
            smaller than ``target_count``, samples are repeated with
            different voice picks (same variant, same speed picks
            different voice on the second pass).
        voices: Override the default voice catalogue. Empty tuple
            means use :data:`_DEFAULT_VOICES`.
        speed_range: ``(min, max)`` speed scaling factor. The
            synthesizer picks N evenly-spaced points in this range
            (where N = up to 4 to keep distribution clean).
    """

    wake_word: str
    variants: tuple[str, ...]
    language: str
    target_count: int
    voices: tuple[str, ...] = ()
    speed_range: tuple[float, float] = _DEFAULT_SPEED_RANGE


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Outcome of a synthesis job.

    Attributes:
        output_dir: Directory containing the generated WAV files.
        sample_paths: Paths to every WAV file written, in deterministic
            order (variant × voice × speed iteration order).
        cancelled: ``True`` when the cancel-check callback returned
            True before completion.
        completed_count: Number of WAVs successfully written. Always
            ``len(sample_paths)``; surfaced separately so the orchestrator
            can correlate with ``target_count`` in progress reports
            without a second len() call on the path list.
    """

    output_dir: Path
    sample_paths: tuple[Path, ...]
    cancelled: bool
    completed_count: int


# ── Synthesizer ─────────────────────────────────────────────────────


class KokoroSampleSynthesizer:
    """Generate positive WAV samples for a wake-word training job.

    Args:
        tts: A :class:`_TTSEngine`-shaped TTS engine. Production
            wires :class:`sovyx.voice.tts_kokoro.KokoroTTS`; tests
            inject a deterministic stub.

    Thread safety:
        The synthesizer holds no mutable state — concurrent
        ``synthesize`` calls are safe IF the underlying TTS engine
        is. Kokoro's ONNX session is single-threaded; tests with
        stub TTS can parallelise freely.
    """

    def __init__(self, tts: _TTSEngine) -> None:
        self._tts = tts

    async def synthesize(
        self,
        request: SynthesisRequest,
        output_dir: Path,
        *,
        on_progress: Callable[[int, int, str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> SynthesisResult:
        """Generate WAV samples up to ``request.target_count``.

        The output directory is created if missing. WAV filenames
        follow the deterministic pattern
        ``"{idx:04d}_{variant}_{voice}_speed{speed:.2f}.wav"`` so a
        partial run can be resumed by skipping existing filenames
        (orchestrator's job, not the synthesizer's).

        Args:
            request: Spec for what to generate.
            output_dir: Where to write WAVs. Created if missing.
                Existing files are NOT overwritten — the synthesizer
                skips a slot when the target filename already exists.
                This is the resume primitive: re-running a synthesis
                on the same dir + spec writes only the missing files.
            on_progress: Optional callback invoked once per generated
                sample with ``(index, total, message)``. ``index`` is
                1-indexed; ``total`` equals ``request.target_count``.
                ``message`` is a human-readable status line (variant +
                voice + speed).
            cancel_check: Optional callback polled before each
                synthesis call. When it returns ``True``, the
                synthesizer stops cleanly and returns
                :class:`SynthesisResult` with ``cancelled=True`` and
                ``completed_count`` reflecting what was already
                written.

        Returns:
            :class:`SynthesisResult`.

        Raises:
            ValueError: ``request.target_count`` is non-positive,
                ``request.variants`` is empty, or
                ``request.speed_range`` is malformed.
        """
        self._validate(request)
        output_dir.mkdir(parents=True, exist_ok=True)

        voices = request.voices or _DEFAULT_VOICES
        speeds = self._build_speed_grid(request.speed_range)
        plan = self._build_plan(request, voices, speeds)

        sample_paths: list[Path] = []
        cancelled = False

        for idx, (variant, voice, speed) in enumerate(plan, start=1):
            if cancel_check is not None and cancel_check():
                cancelled = True
                break

            filename = self._format_filename(idx, variant, voice, speed)
            target_path = output_dir / filename

            # Resume primitive: skip existing files. Lets the
            # orchestrator re-invoke the synthesizer on the same
            # output_dir to fill in gaps without redoing successful
            # samples. The "completed_count" reflects what's there
            # at the end (existing + newly written).
            if target_path.exists():
                sample_paths.append(target_path)
                if on_progress is not None:
                    on_progress(idx, request.target_count, f"resumed {filename}")
                continue

            try:
                samples_int16 = await self._synthesize_one(
                    variant,
                    voice=voice,
                    language=request.language,
                    speed=speed,
                )
            except Exception:  # noqa: BLE001 — log + skip + continue
                logger.warning(
                    "voice.training.synthesizer.sample_failed",
                    variant=variant,
                    voice=voice,
                    speed=speed,
                )
                continue

            self._write_wav(target_path, samples_int16, _TARGET_SAMPLE_RATE)
            sample_paths.append(target_path)
            if on_progress is not None:
                on_progress(idx, request.target_count, filename)

        return SynthesisResult(
            output_dir=output_dir,
            sample_paths=tuple(sample_paths),
            cancelled=cancelled,
            completed_count=len(sample_paths),
        )

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _validate(request: SynthesisRequest) -> None:
        if request.target_count <= 0:
            msg = f"SynthesisRequest.target_count must be positive; got {request.target_count}"
            raise ValueError(msg)
        if not request.variants:
            msg = "SynthesisRequest.variants must be non-empty"
            raise ValueError(msg)
        low, high = request.speed_range
        if not (0.5 <= low <= high <= 2.0):
            msg = (
                f"SynthesisRequest.speed_range must satisfy "
                f"0.5 <= min <= max <= 2.0; got {request.speed_range}"
            )
            raise ValueError(msg)

    @staticmethod
    def _build_speed_grid(speed_range: tuple[float, float]) -> tuple[float, ...]:
        """Pick up to 4 evenly-spaced speed values inside ``speed_range``.

        4 is empirically the sweet spot: enough variation for the
        trainer to generalise + few enough that the
        ``variants × voices × speeds`` cartesian doesn't explode
        beyond a typical training corpus.
        """
        low, high = speed_range
        if low == high:
            return (low,)
        # 4 evenly-spaced points; np.linspace is overkill for 4 values
        # so we compute manually + round to avoid float-noise
        # filename suffixes.
        points = (low, low + (high - low) / 3, low + 2 * (high - low) / 3, high)
        return tuple(round(p, 2) for p in points)

    @staticmethod
    def _build_plan(
        request: SynthesisRequest,
        voices: tuple[str, ...],
        speeds: tuple[float, ...],
    ) -> list[tuple[str, str, float]]:
        """Build the iteration plan (variant × voice × speed).

        Cycles through the cartesian product until ``target_count``
        is reached. Order: voice innermost, then speed, then variant
        — so consecutive samples have different voices (better
        progress UX than 8 consecutive "af_heart" then 8 "af_alloy").
        """
        plan: list[tuple[str, str, float]] = []
        target = request.target_count
        idx = 0
        # Build the canonical cycle: variant×speed×voice; iterate
        # until we hit target. When the product is smaller than
        # target, the loop wraps (operators training with very few
        # variants get larger sample counts via wrap-around).
        cycle: list[tuple[str, str, float]] = []
        for variant in request.variants:
            for speed in speeds:
                for voice in voices:
                    cycle.append((variant, voice, speed))
        if not cycle:
            return plan
        while len(plan) < target:
            plan.append(cycle[idx % len(cycle)])
            idx += 1
        return plan

    @staticmethod
    def _format_filename(
        idx: int,
        variant: str,
        voice: str,
        speed: float,
    ) -> str:
        # Sanitise variant for filesystem use: keep only ASCII
        # alphanumerics; everything else (diacritics, spaces, punctuation)
        # becomes underscore. Diacritics typically already stripped at
        # the variant-composition layer (T8.2 / T8.16), but be defensive
        # — Python's ``str.isalnum`` accepts Unicode letters (``ú``,
        # ``ñ``) which produce filenames that mojibake on cross-filesystem
        # transfers (NTFS export → ext4 import) or surface as encoding
        # warnings in archive tools.
        safe_variant = "".join(
            c if (c.isascii() and c.isalnum()) else "_" for c in variant.lower()
        )[:32]
        return f"{idx:04d}_{safe_variant}_{voice}_speed{speed:.2f}.wav"

    async def _synthesize_one(
        self,
        text: str,
        *,
        voice: str,
        language: str,
        speed: float,
    ) -> npt.NDArray[np.int16]:
        """Run one synthesis call, resample, return int16 mono."""
        chunk = await self._tts.synthesize_with(
            text,
            voice=voice,
            language=language,
            speed=speed,
        )
        # Duck-type extraction — Kokoro's AudioChunk has .audio + .sample_rate.
        audio_int16 = getattr(chunk, "audio", None)
        sample_rate = getattr(chunk, "sample_rate", _KOKORO_NATIVE_RATE)
        if audio_int16 is None:
            return np.zeros(0, dtype=np.int16)

        samples = np.asarray(audio_int16, dtype=np.int16).flatten()
        if int(sample_rate) != _TARGET_SAMPLE_RATE:
            samples = self._resample_int16(
                samples,
                src_rate=int(sample_rate),
                dst_rate=_TARGET_SAMPLE_RATE,
            )
        return samples

    @staticmethod
    def _resample_int16(
        samples: npt.NDArray[np.int16],
        *,
        src_rate: int,
        dst_rate: int,
    ) -> npt.NDArray[np.int16]:
        """Resample int16 mono audio. Linear-interp; matches existing
        ``voice/audio.py`` simple resampler used for the wizard
        test-record path. scipy.signal.resample would be higher
        quality but pulls in an extra dep at training-time."""
        if src_rate == dst_rate or samples.size == 0:
            return samples
        target_len = int(len(samples) * dst_rate / src_rate)
        if target_len <= 1:
            return np.zeros(target_len, dtype=np.int16)
        indices = np.linspace(0, len(samples) - 1, target_len)
        # Convert to float64 for interpolation; cast back at the end.
        as_float = samples.astype(np.float64)
        resampled = np.interp(indices, np.arange(len(samples)), as_float)
        return resampled.astype(np.int16)

    @staticmethod
    def _write_wav(path: Path, samples: npt.NDArray[np.int16], sample_rate: int) -> None:
        """Write int16 mono PCM WAV using stdlib ``wave``.

        stdlib wave avoids the scipy.io.wavfile dependency and is
        sufficient for the trainer's needs (the OpenWakeWord pipeline
        opens WAVs via soundfile / librosa which accept stdlib WAVs).
        """
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(sample_rate)
            wf.writeframes(samples.tobytes())


__all__ = [
    "KokoroSampleSynthesizer",
    "SynthesisRequest",
    "SynthesisResult",
]
