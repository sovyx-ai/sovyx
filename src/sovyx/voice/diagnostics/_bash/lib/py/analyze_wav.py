#!/usr/bin/env python3
"""analyze_wav — métricas acústicas + classificação espectral de um WAV.

Gera o JSON descrito em §3 do plano (schema de ``analysis.json``).

Uso:
    analyze_wav.py --wav PATH --state STATE [--source SRC] [--capture-id ID]
                   [--monotonic-ns N] [--utc-iso-ns T] [--out OUT_JSON]

Depende de stdlib + numpy. numpy é obrigatório para a banda espectral;
sem ele, ``spectral_available=false`` e o classifier reporta
``classification="spectrum_unavailable"`` com código.

AUDIT v3 (post-session-2)
-------------------------
Três bugs de correção numérica foram corrigidos:

1. **``rolloff_40db_hz`` vs ``rolloff_99_hz`` schema mismatch.** A
   versão anterior retornava ``rolloff_40db_hz: 0.0`` no fallback
   (audio < 32 samples) mas ``rolloff_99_hz`` no success path. O
   classifier lia a chave errada → classificação falsa para capturas
   curtas. **Schema unificado: sempre ``rolloff_85_hz`` (0.85
   cumulative energy, standard MIR).** O threshold antigo 0.99 era
   igual ao "máximo" → não distinguia muita coisa; 0.85 é o padrão.

2. **ZCR divisor usando request em vez de chunk real.** Dividir
   crossings pelo ``window_s`` solicitado (0.5 s) quando o último
   chunk tem comprimento menor (ex. 0.2 s sobra do final) reporta
   ZCR sub-estimado para a última janela. Fix: divide por
   ``len(chunk) / rate`` — duração REAL do chunk.

3. **Silence floor inconsistente.** ``_rms_db`` retornava ``-120``
   para silêncio, mas ``_rms_windows`` retornava ``log10(1e-12) ≈
   -240`` (off-by-120dB!). Analista comparando janelas com total
   veria mismatch catastrófico. Fix: ambos usam ``_RMS_FLOOR_DB``
   compartilhado.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

# AUDIT v3 — constantes compartilhadas (antes dispersas).
_RMS_FLOOR_DB = -120.0  # piso de silêncio — usado por _rms_db E _rms_windows
_SILENCE_RMS_THRESHOLD_DB = -55.0  # acima disto, considera-se ter signal
_CLIPPING_FULL_SCALE = 0.9999  # |x| >= este → sample clipped
_ROLLOFF_CUM_FRACTION = 0.85  # MIR standard (librosa default)
_ROLLOFF_KEY = "rolloff_85_hz"  # nome da chave no JSON — ESTÁVEL


def _read_wav(path: Path) -> tuple[list[float], int, int, int]:
    """Retorna (samples_mono_float_-1..+1, rate, channels, bits)."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)

    if width == 2:
        fmt = f"<{n * ch}h"
        samples = list(struct.unpack(fmt, raw))
        scale = 32768.0
    elif width == 4:
        fmt = f"<{n * ch}i"
        samples = list(struct.unpack(fmt, raw))
        scale = 2147483648.0
    elif width == 1:
        # unsigned 8-bit WAV
        fmt = f"<{n * ch}B"
        samples = [x - 128 for x in struct.unpack(fmt, raw)]
        scale = 128.0
    else:
        raise ValueError(f"Unsupported WAV width: {width}")

    # Downmix to mono by averaging channels.
    if ch > 1:
        mono: list[float] = []
        for i in range(0, len(samples), ch):
            frame = samples[i : i + ch]
            mono.append(sum(frame) / (ch * scale))
        return mono, rate, ch, width * 8
    return [s / scale for s in samples], rate, ch, width * 8


def _rms_db(samples: list[float]) -> float:
    if not samples:
        return _RMS_FLOOR_DB
    s = sum(x * x for x in samples)
    mean = s / len(samples)
    if mean <= 1e-12:
        return _RMS_FLOOR_DB
    return 20.0 * math.log10(math.sqrt(mean))


def _peak_db(samples: list[float]) -> float:
    if not samples:
        return _RMS_FLOOR_DB
    peak = max(abs(x) for x in samples)
    if peak <= 1e-12:
        return _RMS_FLOOR_DB
    return 20.0 * math.log10(peak)


def _rms_windows(samples: list[float], rate: int, window_s: float) -> list[float]:
    """Per-window RMS in dBFS. Uses _RMS_FLOOR_DB for silent windows
    (was -240 via log10(1e-12) — 120dB discrepancy vs _rms_db)."""
    win = int(rate * window_s)
    if win <= 0:
        return []
    out: list[float] = []
    for start in range(0, len(samples), win):
        chunk = samples[start : start + win]
        if not chunk:
            continue
        mean = sum(x * x for x in chunk) / len(chunk)
        if mean <= 1e-12:
            out.append(_RMS_FLOOR_DB)
        else:
            out.append(round(20.0 * math.log10(math.sqrt(mean)), 2))
    return out


def _zcr_windows(samples: list[float], rate: int, window_s: float) -> list[float]:
    """Zero-crossing rate per window, crossings/second.

    AUDIT v3 fix #2: divide by the ACTUAL chunk duration (``len(chunk)
    / rate``), not by ``window_s``. The last (shorter) chunk used to
    report crossings/window_s, systematically under-counting the ZCR
    for the tail window.
    """
    win = int(rate * window_s)
    if win <= 0 or rate <= 0:
        return []
    out: list[float] = []
    for start in range(0, len(samples), win):
        chunk = samples[start : start + win]
        if len(chunk) < 2:
            continue
        crossings = sum(
            1 for i in range(1, len(chunk)) if (chunk[i - 1] >= 0) != (chunk[i] >= 0)
        )
        chunk_duration_s = len(chunk) / rate  # REAL duration, not requested
        if chunk_duration_s > 0:
            out.append(round(crossings / chunk_duration_s, 1))
    return out


def _clipping_count(samples: list[float]) -> int:
    return sum(1 for x in samples if abs(x) >= _CLIPPING_FULL_SCALE)


def _empty_spectrum() -> dict[str, float]:
    """Schema estável para o fallback quando não há espectro.

    AUDIT v3 fix #1: sempre a MESMA chave de rolloff em ambos success
    e failure paths. Antes usava ``rolloff_40db_hz`` no fallback e
    ``rolloff_99_hz`` no success — classifier lendo ``rolloff_99_hz``
    via ``.get(..., 0.0)`` recebia 0.0 no fallback e classificava como
    ``band_limited_voice`` (rolloff <= 1500) incorretamente.
    """
    return {
        "peak_freq_hz": 0.0,
        _ROLLOFF_KEY: 0.0,
        "flatness": 0.0,
        "low_band_energy_pct_0_500hz": 0.0,
        "mid_band_energy_pct_500_4000hz": 0.0,
        "high_band_energy_pct_4000_8000hz": 0.0,
    }


def _spectrum_numpy(
    samples: list[float],
    rate: int,
    start_s: float,
    end_s: float,
) -> dict[str, float]:
    """Métricas espectrais via numpy. Requer numpy."""
    import numpy as np  # noqa: PLC0415

    s = int(start_s * rate)
    e = int(end_s * rate)
    chunk = np.array(samples[s:e], dtype=np.float64)
    if chunk.size < 32:
        return _empty_spectrum()

    # Hann window reduces spectral leakage.
    win = np.hanning(chunk.size)
    spec = np.fft.rfft(chunk * win)
    mag = np.abs(spec)
    freqs = np.fft.rfftfreq(chunk.size, d=1.0 / rate)

    total_energy = float(np.sum(mag**2)) or 1e-12
    peak_idx = int(np.argmax(mag))
    peak_freq = float(freqs[peak_idx])

    # Rolloff @ 85% cumulative energy — MIR standard (librosa default).
    cumulative = np.cumsum(mag**2)
    target = _ROLLOFF_CUM_FRACTION * cumulative[-1]
    rolloff_idx = int(np.searchsorted(cumulative, target))
    rolloff_freq = float(freqs[min(rolloff_idx, len(freqs) - 1)])

    # Spectral flatness: geometric / arithmetic mean of magnitude spectrum.
    # Skip DC bin (mag[0]) and zero-magnitude bins to avoid log(0)
    # pulling the geometric mean artificially down.
    eps = 1e-12
    mag_no_dc = mag[1:]
    mag_nz = mag_no_dc[mag_no_dc > eps]
    if mag_nz.size >= 2:
        log_mag = np.log(mag_nz)
        geo = float(np.exp(np.mean(log_mag)))
        arith = float(np.mean(mag_nz))
        flatness = geo / arith if arith > 0 else 0.0
    else:
        flatness = 0.0

    def band_pct(lo: float, hi: float) -> float:
        mask = (freqs >= lo) & (freqs < hi)
        return float(np.sum(mag[mask] ** 2) / total_energy)

    return {
        "peak_freq_hz": round(peak_freq, 2),
        _ROLLOFF_KEY: round(rolloff_freq, 2),
        "flatness": round(flatness, 4),
        "low_band_energy_pct_0_500hz": round(band_pct(0, 500), 4),
        "mid_band_energy_pct_500_4000hz": round(band_pct(500, 4000), 4),
        "high_band_energy_pct_4000_8000hz": round(band_pct(4000, 8000), 4),
    }


def _classify(rms_db: float, spectral: dict[str, float], clipping: int) -> str:
    """Classificação heurística consistente com schema unificado."""
    if clipping > 10:
        return "clipped"
    if rms_db < _SILENCE_RMS_THRESHOLD_DB:
        return "silence"
    flatness = spectral.get("flatness", 0.0)
    rolloff = spectral.get(_ROLLOFF_KEY, 0.0)
    mid = spectral.get("mid_band_energy_pct_500_4000hz", 0.0)
    if flatness > 0.5 and rms_db > -50:
        return "white_noise"
    if -40 <= rms_db <= -5 and rolloff > 3000 and flatness < 0.15 and mid > 0.3:
        return "healthy_voice"
    if rolloff > 0 and rolloff <= 1500:
        return "band_limited_voice"
    return "unclassified"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        encoding="utf-8",
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, str(path))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--wav", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--source", default="")
    p.add_argument("--capture-id", default="")
    p.add_argument("--monotonic-ns", default="0")
    p.add_argument("--utc-iso-ns", default="")
    p.add_argument("--out", default="")
    args = p.parse_args()

    wav_path = Path(args.wav)
    if not wav_path.exists():
        payload = json.dumps({"error": "file_not_found", "wav": str(wav_path)})
        if args.out:
            _atomic_write(Path(args.out), payload + "\n")
        else:
            print(payload)
        return 1

    try:
        samples, rate, ch, bits = _read_wav(wav_path)
    except (wave.Error, struct.error, ValueError) as exc:
        payload = json.dumps(
            {
                "error": "wav_parse_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "wav": str(wav_path),
            },
        )
        if args.out:
            _atomic_write(Path(args.out), payload + "\n")
        else:
            print(payload)
        return 1

    duration = len(samples) / rate if rate else 0.0

    spectral_available = False
    spectral: dict[str, float] = _empty_spectrum()
    spectral_note = ""
    try:
        # V4 fix: the previous trim (start=max(0.5, 20%), end=min(dur-0.5,80%))
        # rejected every capture ≤1.0s — including the selftest's 1.0s
        # calibration tone. A forensic tool that can't measure a 1-second
        # tone is useless. New policy: trim 10% off each end (or 0.2s,
        # whichever smaller), analyze the middle. Require ≥0.3s of
        # analyzable content (≥ ~5000 samples @16kHz → FFT bin width
        # ~53Hz, enough to separate 440Hz from DC).
        trim = min(0.2, duration * 0.1)
        start = trim
        end = duration - trim
        analyzable = end - start
        if analyzable >= 0.3:
            spectral = _spectrum_numpy(samples, rate, start, end)
            spectral_available = True
            spectral_note = f"ok:window_{analyzable:.3f}s_of_{duration:.3f}s"
        else:
            spectral_note = f"capture_too_short:{duration:.3f}s_analyzable_{analyzable:.3f}s"
    except ImportError:
        spectral_note = "numpy_not_available"
    except Exception as exc:  # noqa: BLE001
        spectral_note = f"spectrum_failed:{type(exc).__name__}:{exc}"

    rms = round(_rms_db(samples), 2)
    peak = round(_peak_db(samples), 2)
    clipping = _clipping_count(samples)
    classification = (
        _classify(rms, spectral, clipping)
        if spectral_available
        else "spectrum_unavailable"
    )

    result: dict[str, object] = {
        "capture_id": args.capture_id,
        "captured_in_state": args.state,
        "capture_started_at": args.utc_iso_ns,
        "capture_monotonic_ns": int(args.monotonic_ns or 0),
        "source": args.source,
        "file": str(wav_path),
        "file_size_bytes": wav_path.stat().st_size,
        "sample_rate": rate,
        "channels": ch,
        "bits_per_sample": bits,
        "duration_s": round(duration, 3),
        "rms_dbfs": rms,
        "peak_dbfs": peak,
        "rms_by_window_500ms_dbfs": _rms_windows(samples, rate, 0.5),
        "zcr_by_window_500ms_hz": _zcr_windows(samples, rate, 0.5),
        "clipping_samples": clipping,
        "spectral_available": spectral_available,
        "spectral": spectral,
        "spectral_note": spectral_note,
        "classification": classification,
        "warnings": [],
    }

    warnings = result["warnings"]
    assert isinstance(warnings, list)
    if rms < _SILENCE_RMS_THRESHOLD_DB:
        warnings.append("rms_below_silence_floor")
    if clipping > 10:
        warnings.append("clipping_detected")
    if spectral_available:
        rolloff = float(spectral.get(_ROLLOFF_KEY, 0.0))
        # Single-source-of-truth warning: emit ONE rolloff warning
        # (the old code could emit both "below_500hz" and "band_limited"
        # for the same value).
        if 0 < rolloff < 500:
            warnings.append(f"spectral_rolloff_below_500hz:{rolloff}")
        elif 0 < rolloff < 1500:
            warnings.append(f"band_limited_voice:{rolloff}hz")

    output_json = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        _atomic_write(Path(args.out), output_json + "\n")
    else:
        sys.stdout.write(output_json + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
