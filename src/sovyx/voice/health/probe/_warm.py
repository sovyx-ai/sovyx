"""Warm-mode probe analysis (RMS + VAD) and diagnosis.

ADR §4.3 warm-mode diagnosis: the warm probe runs the captured audio
through :class:`~sovyx.voice._frame_normalizer.FrameNormalizer` and
:class:`~sovyx.voice.vad.SileroVAD` so the probe can derive the full
:class:`~sovyx.voice.health.contract.Diagnosis` surface — in particular
:attr:`~sovyx.voice.health.contract.Diagnosis.APO_DEGRADED`, which
requires signal *content* evidence (healthy RMS + dead VAD).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice.health.contract import Combo, Diagnosis
from sovyx.voice.health.probe._classifier import (
    _RMS_DB_LOW_SIGNAL_CEILING,
    _RMS_DB_NO_SIGNAL_CEILING,
    _TARGET_PIPELINE_WINDOW,
    _VAD_APO_DEGRADED_CEILING,
    _VAD_HEALTHY_FLOOR,
    _compute_rms_db,
    _format_scale,
    _warmup_samples,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy.typing as npt

    from sovyx.voice.vad import SileroVAD


# ── Warm-mode analysis ────────────────────────────────────────────


def _analyse_rms(
    blocks: list[npt.NDArray[Any]],
    combo: Combo,
) -> float:
    """Compute dBFS over the post-warmup concatenation of ``blocks``."""
    import numpy as np

    if not blocks:
        return float("-inf")

    # Downmix multichannel blocks to mono for RMS — VAD does the same.
    mono_blocks: list[npt.NDArray[Any]] = []
    for b in blocks:
        if b.ndim == 2:
            mono_blocks.append(b.mean(axis=1))
        else:
            mono_blocks.append(b)

    flat = np.concatenate(mono_blocks)
    warmup = _warmup_samples(combo)
    if flat.size <= warmup:
        return float("-inf")
    tail = flat[warmup:]
    scale = _format_scale(combo.sample_format)
    return _compute_rms_db(tail, scale)


def _analyse_vad(
    blocks: list[npt.NDArray[Any]],
    *,
    combo: Combo,
    vad: SileroVAD,
    frame_normalizer_factory: Callable[[int, int, str], FrameNormalizer] | None,
) -> tuple[float, float]:
    """Run the post-warmup audio through the resampler + VAD.

    Returns ``(max_prob, mean_prob)``. Both are ``0.0`` when no full
    16 kHz / 512-sample window could be assembled from the captured
    audio (warmup-sized probe or empty stream).
    """
    import numpy as np

    if not blocks:
        return 0.0, 0.0

    factory = frame_normalizer_factory or _default_frame_normalizer_factory
    normalizer = factory(combo.sample_rate, combo.channels, combo.sample_format)

    probabilities: list[float] = []
    warmup_remaining = _warmup_samples(combo)

    for block in blocks:
        # Peel off warmup samples per-block so the VAD sees clean audio.
        if warmup_remaining > 0:
            if block.ndim == 1:
                if block.shape[0] <= warmup_remaining:
                    warmup_remaining -= block.shape[0]
                    continue
                block = block[warmup_remaining:]
            else:
                if block.shape[0] <= warmup_remaining:
                    warmup_remaining -= block.shape[0]
                    continue
                block = block[warmup_remaining:, :]
            warmup_remaining = 0

        windows = normalizer.push(block)
        for window in windows:
            if window.shape != (_TARGET_PIPELINE_WINDOW,):
                continue
            event = vad.process_frame(window)
            probabilities.append(float(event.probability))

    if not probabilities:
        return 0.0, 0.0

    arr = np.asarray(probabilities, dtype=np.float32)
    return float(arr.max()), float(arr.mean())


def _default_frame_normalizer_factory(
    source_rate: int,
    source_channels: int,
    source_format: str,
) -> FrameNormalizer:
    return FrameNormalizer(
        source_rate=source_rate,
        source_channels=source_channels,
        source_format=source_format,
    )


# ── Warm-mode diagnosis ───────────────────────────────────────────


def _diagnose_warm(
    *,
    rms_db: float,
    vad_max_prob: float,
    callbacks_fired: int,
) -> Diagnosis:
    """Warm-mode diagnosis table (ADR §4.3)."""
    if callbacks_fired == 0:
        return Diagnosis.NO_SIGNAL
    if rms_db < _RMS_DB_NO_SIGNAL_CEILING:
        return Diagnosis.NO_SIGNAL
    if rms_db < _RMS_DB_LOW_SIGNAL_CEILING:
        return Diagnosis.LOW_SIGNAL
    # rms_db ≥ -55 dB: decide on VAD.
    if vad_max_prob >= _VAD_HEALTHY_FLOOR:
        return Diagnosis.HEALTHY
    if vad_max_prob < _VAD_APO_DEGRADED_CEILING:
        return Diagnosis.APO_DEGRADED
    return Diagnosis.VAD_INSENSITIVE


__all__ = [
    "_analyse_rms",
    "_analyse_vad",
    "_default_frame_normalizer_factory",
    "_diagnose_warm",
]
