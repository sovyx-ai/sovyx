#!/usr/bin/env python3
"""silero_probe — roda Silero VAD no wav e emite max/mean prob + frames-above.

Usa o modelo ONNX que o Sovyx já baixou em ``~/.sovyx/models/voice/silero_vad.onnx``.

Uso:
    silero_probe.py --wav PATH [--onset 0.5] [--out OUT_JSON]
                    [--model PATH_OVERRIDE]

Saída JSON (success):
    {
      "available": true,
      "max_prob": 0.97,
      "mean_prob": 0.32,
      "frames_above_onset": 12,
      "total_frames": 219,
      "onset_threshold": 0.5,
      "frame_size": 512,
      "sample_rate_source": 48000,
      "sample_rate_vad": 16000,
      "resampler": "polyphase",
      "model_path": "..."
    }

Saída JSON (failure): ``{"available": false, "reason": "<code>", ...}``.

Exit codes:
    0 — success or a structured "available: false" result (consumer reads JSON)
    1 — argparse/path failure (unusual; result still written if possible)

AUDIT v3 (post-session-2)
-------------------------
Três bugs **críticos** foram corrigidos:

1. **Stereo downmix produzia lixo.** A versão anterior iterava
   ``range(0, len(samples), 1)`` (step=1, não ``ch``), somando tuplas
   consecutivas sobrepostas em vez de frames não-sobrepostos. Para
   stereo, o "mono" resultante tinha length=N (não N/2) e cada amostra
   era uma média rolante ruidosa. O fix usa ``step=ch``, matching
   ``analyze_wav.py``.

2. **Linear resample sem anti-alias filter.** 48k→16k (ratio=1/3) via
   linear interpolation dobra aliased content no espectro baixo, e a
   Silero VAD (treinada com anti-alias-filtered audio) reporta
   probabilities **sistematicamente inferiores** ao path de produção
   (que usa ``scipy.signal.resample_poly``). Resultado: comparação
   forense entre métricas do probe e métricas do Sovyx é inválida.
   Fix: usa ``scipy.signal.resample_poly`` se scipy disponível;
   caso contrário, emite ``reason: resample_requires_scipy`` em vez de
   degradar silenciosamente.

3. **Emite ``available: true`` mesmo sem frames.** Audio curto (<512
   samples @ 16k = 32 ms) produzia ``probs=[]`` e ``max=mean=0.0`` com
   ``available=true`` — analista não distingue "silêncio verdadeiro"
   de "audio curto demais para medir". Fix: return
   ``available: false, reason: audio_too_short_for_frames``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

_FRAME_SIZE_16K = 512  # Silero VAD v5 padrão a 16 kHz (32 ms).
_SR_VAD = 16_000


def _find_model(override: str | None = None) -> Path | None:
    """Localiza o modelo ONNX. ``override`` ganha precedência; senão tenta
    o caminho canônico de instalação do Sovyx."""
    if override:
        p = Path(override)
        return p if p.is_file() else None
    candidate = Path.home() / ".sovyx" / "models" / "voice" / "silero_vad.onnx"
    if candidate.is_file():
        return candidate
    return None


def _read_wav_to_float32_mono(path: Path) -> tuple[object, int, str | None]:
    """Lê WAV S16 LE, desentrelaça canais, downmixa para mono float32.

    NÃO faz resample aqui; retorna a taxa nativa e o caller decide.
    Return: ``(samples | None, rate, error_code | None)``.
    """
    try:
        import numpy as np
    except ImportError as exc:
        return None, 0, f"numpy_not_importable:{exc}"

    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            ch = w.getnchannels()
            width = w.getsampwidth()
            n = w.getnframes()
            raw = w.readframes(n)
    except wave.Error as exc:
        return None, 0, f"wav_parse_failed:{exc}"
    except OSError as exc:
        return None, 0, f"wav_io_failed:{exc}"

    if width != 2:
        return None, rate, f"unsupported_width_{width}"
    if n == 0:
        return None, rate, "wav_empty"

    # Unpack interleaved int16 samples in one shot — orders of
    # magnitude faster than a Python list comprehension and avoids a
    # transient list of millions of ints.
    expected_bytes = n * ch * width
    if len(raw) < expected_bytes:
        return None, rate, f"wav_truncated:{len(raw)}B<{expected_bytes}B"

    arr = np.frombuffer(raw, dtype=np.int16)
    # arr shape: (n * ch,) interleaved. Reshape to (n, ch) then mean
    # across channels. This is the CORRECT stereo downmix (bug #1 fix):
    # before, the loop read overlapping 2-tuples and produced
    # length=len(samples) mono that was rolling averages of interleaved
    # L/R — nonsense for any stereo input.
    if arr.size != n * ch:
        return None, rate, f"wav_sample_count_mismatch:{arr.size}!={n}*{ch}"
    if ch > 1:
        interleaved = arr.reshape(n, ch).astype(np.float32)
        mono = interleaved.mean(axis=1)
    else:
        mono = arr.astype(np.float32)

    # Scale to [-1.0, 1.0].
    mono = mono / 32768.0
    return mono, rate, None


def _resample_to_16k(samples, src_rate: int) -> tuple[object, str | None]:
    """Resample to 16 kHz using scipy's polyphase filter (anti-aliased).

    Return: ``(samples_16k | None, error_code | None)``.

    AUDIT v3: the previous version did linear interpolation without an
    anti-alias filter. For 48 kHz → 16 kHz (ratio 1/3), this folds
    energy from 8-24 kHz back down into 0-8 kHz, inflating high-
    frequency content in the 16 kHz output. Silero VAD was trained on
    properly-low-passed 16 kHz audio; feeding it linearly-downsampled
    content systematically REDUCES the inference probability vs.
    Sovyx's production path (which uses scipy.resample_poly). The
    effect is most pronounced on content-rich speech — exactly the
    forensic input we care about.

    If scipy is unavailable we refuse to produce misleading numbers
    rather than silently downgrade.
    """
    if src_rate == _SR_VAD:
        return samples, None
    try:
        import numpy as np
        from scipy.signal import resample_poly
    except ImportError:
        return None, "resample_requires_scipy"
    # resample_poly is anti-alias filtered polyphase; handles any
    # ratio cleanly. Compute the up/down integer pair from the
    # rational approximation (GCD-reduced).
    from math import gcd

    g = gcd(_SR_VAD, src_rate)
    up = _SR_VAD // g
    down = src_rate // g
    try:
        resampled = resample_poly(samples, up, down).astype(np.float32)
    except Exception as exc:  # noqa: BLE001
        return None, f"resample_failed:{type(exc).__name__}:{exc}"
    return resampled, None


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a tempfile in the same directory so os.replace is atomic
    # across the same filesystem.
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


def _emit(result: dict, out: str) -> None:
    """Write the JSON payload atomically to ``out`` or to stdout."""
    payload = json.dumps(result, indent=2)
    if out:
        _atomic_write(Path(out), payload + "\n")
    else:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--wav", required=True)
    p.add_argument("--onset", type=float, default=0.5)
    p.add_argument("--out", default="")
    p.add_argument(
        "--model",
        default="",
        help="Override model path (else looks in ~/.sovyx/models/voice/)",
    )
    args = p.parse_args()

    wav_path = Path(args.wav)
    if not wav_path.exists():
        _emit(
            {
                "available": False,
                "reason": "wav_not_found",
                "path": str(wav_path),
            },
            args.out,
        )
        return 0

    model = _find_model(args.model or None)
    if model is None:
        _emit(
            {
                "available": False,
                "reason": "model_not_found",
                "expected_path": str(
                    Path.home() / ".sovyx" / "models" / "voice" / "silero_vad.onnx",
                ),
                "override": args.model or None,
            },
            args.out,
        )
        return 0

    try:
        import numpy as np  # noqa: F401
        import onnxruntime as ort
    except ImportError as exc:
        _emit(
            {
                "available": False,
                "reason": "import_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            },
            args.out,
        )
        return 0

    samples, src_rate, err = _read_wav_to_float32_mono(wav_path)
    if err is not None:
        _emit({"available": False, "reason": err, "source_rate": src_rate}, args.out)
        return 0

    resampled_raw, rerr = _resample_to_16k(samples, src_rate)
    if rerr is not None or resampled_raw is None:
        _emit(
            {
                "available": False,
                "reason": rerr or "resample_returned_none",
                "source_rate": src_rate,
            },
            args.out,
        )
        return 0

    # After guard above, resampled_raw is a numpy ndarray; narrow via cast
    # so mypy accepts len() / slicing on it.
    from typing import Any, cast
    resampled = cast(Any, resampled_raw)  # runtime: np.ndarray[np.float32]

    # AUDIT v3 fix #3: audio-too-short surfaces as available:false so
    # the consumer can distinguish "0.0 probability because silent"
    # from "0.0 probability because we never ran an inference".
    if len(resampled) < _FRAME_SIZE_16K:
        _emit(
            {
                "available": False,
                "reason": "audio_too_short_for_frames",
                "source_rate": src_rate,
                "samples_16k": int(len(resampled)),
                "min_samples_required": _FRAME_SIZE_16K,
            },
            args.out,
        )
        return 0

    try:
        import numpy as np

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1  # don't starve a concurrent Sovyx daemon
        opts.inter_op_num_threads = 1
        sess = ort.InferenceSession(
            str(model),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        audio = np.asarray(resampled, dtype=np.float32)
        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr_tensor = np.array(_SR_VAD, dtype=np.int64)

        probs: list[float] = []
        # Stride by frame_size (no overlap) matches Silero v5 streaming
        # mode documentation.
        for start in range(0, len(audio) - _FRAME_SIZE_16K + 1, _FRAME_SIZE_16K):
            chunk = audio[start : start + _FRAME_SIZE_16K].reshape(1, -1)
            outputs = sess.run(None, {"input": chunk, "state": state, "sr": sr_tensor})
            probs.append(float(outputs[0][0, 0]))
            state = outputs[1]

        if not probs:
            _emit(
                {
                    "available": False,
                    "reason": "inference_produced_no_frames",
                    "source_rate": src_rate,
                },
                args.out,
            )
            return 0

        max_p = max(probs)
        mean_p = sum(probs) / len(probs)
        above = sum(1 for p in probs if p > args.onset)

        _emit(
            {
                "available": True,
                "max_prob": round(max_p, 4),
                "mean_prob": round(mean_p, 4),
                "frames_above_onset": above,
                "total_frames": len(probs),
                "onset_threshold": args.onset,
                "frame_size": _FRAME_SIZE_16K,
                "sample_rate_source": src_rate,
                "sample_rate_vad": _SR_VAD,
                "resampler": "polyphase" if src_rate != _SR_VAD else "none",
                "model_path": str(model),
            },
            args.out,
        )
    except Exception as exc:  # noqa: BLE001 — catch-all; we want structured failure
        _emit(
            {
                "available": False,
                "reason": "inference_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            },
            args.out,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
