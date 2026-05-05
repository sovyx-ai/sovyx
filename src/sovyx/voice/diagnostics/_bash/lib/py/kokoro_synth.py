#!/usr/bin/env python3
"""kokoro_synth — sintetiza TTS Kokoro via a classe interna do Sovyx.

Esta é a rota de FALLBACK da camada K (R12 do ADR-001). A rota primária
é `POST /api/voice/test/output` via bash + curl (usa o pipeline completo
inclusive playback). Este helper existe para casos onde o endpoint
recusa (pipeline ativo, modelo ausente no daemon) ou queremos o WAV
isolado para análise espectral sem tocar no sink.

Uso:
    kokoro_synth.py --text "teste de áudio" --voice pf_dora \
                    --language pt-br --out PATH.wav

Requisitos:
    - Sovyx python com `sovyx.voice.tts_kokoro` importável
    - Modelo em ~/.sovyx/models/voice/kokoro/{kokoro-v1.0.onnx,voices-v1.0.bin}
      (ou kokoro-v1.0.int8.onnx)

Saída JSON em stdout:
    {"ok": true, "out": "...", "duration_s": 1.87, "sample_rate": 24000}
ou
    {"ok": false, "reason": "...", "detail": "..."}

Exit codes:
    0 — sucesso
    1 — Sovyx/Kokoro não importável
    2 — modelo ausente
    3 — síntese falhou
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path


def _model_dir_default() -> Path:
    return Path.home() / ".sovyx" / "models" / "voice" / "kokoro"


def _check_model(model_dir: Path) -> str | None:
    """Retorna None se ok, ou mensagem de erro."""
    if not model_dir.is_dir():
        return f"model_dir_not_found: {model_dir}"
    has_full = (model_dir / "kokoro-v1.0.onnx").is_file()
    has_q8 = (model_dir / "kokoro-v1.0.int8.onnx").is_file()
    if not (has_full or has_q8):
        return f"no_onnx_in: {model_dir}"
    if not (model_dir / "voices-v1.0.bin").is_file():
        return f"voices_bin_missing_in: {model_dir}"
    return None


async def _synth(text: str, voice: str, language: str, out: Path, model_dir: Path) -> dict:
    try:
        from sovyx.voice.tts_kokoro import KokoroTTS  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "import_failed", "detail": str(exc)}

    try:
        engine = KokoroTTS(model_dir=model_dir)
        await engine.initialize()
        synth_with = getattr(engine, "synthesize_with", None)
        if callable(synth_with):
            chunk = await synth_with(text, voice=voice, language=language)
        else:
            chunk = await engine.synthesize(text)
    except FileNotFoundError as exc:
        return {"ok": False, "reason": "model_files_missing", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "synth_failed", "detail": f"{type(exc).__name__}: {exc}"}

    # Escreve WAV S16_LE.
    try:
        audio = chunk.audio  # numpy int16 1D
        sample_rate = int(chunk.sample_rate)
    except AttributeError as exc:
        return {"ok": False, "reason": "chunk_shape_unexpected", "detail": str(exc)}

    # Forensic: a Kokoro chunk com sample_rate=0 ou audio shape errado
    # antigamente caía em um WAV inválido + duration_s=0.0 com ok=true,
    # mascarando síntese quebrada como sucesso. Agora falha explicitamente
    # antes de tocar no filesystem para que o analista veja a causa real.
    if sample_rate <= 0:
        return {"ok": False, "reason": "invalid_sample_rate",
                "detail": f"chunk.sample_rate={sample_rate}"}
    if getattr(audio, "ndim", 1) != 1:
        return {"ok": False, "reason": "invalid_audio_shape",
                "detail": f"audio.ndim={getattr(audio, 'ndim', 'n/a')} (esperado 1D)"}
    if len(audio) == 0:
        return {"ok": False, "reason": "empty_audio",
                "detail": "chunk.audio length=0"}

    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(audio.tobytes())

    duration = len(audio) / sample_rate
    return {
        "ok": True,
        "out": str(out),
        "sample_rate": sample_rate,
        "duration_s": round(duration, 3),
        "samples": int(len(audio)),
        "voice": voice,
        "language": language,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--text", required=True)
    p.add_argument("--voice", default="pf_dora")
    p.add_argument("--language", default="pt-br")
    p.add_argument("--model-dir", default="")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    model_dir = Path(args.model_dir) if args.model_dir else _model_dir_default()
    model_err = _check_model(model_dir)
    if model_err:
        print(json.dumps({"ok": False, "reason": "model_check_failed", "detail": model_err}))
        return 2

    try:
        result = asyncio.run(_synth(args.text, args.voice, args.language,
                                     Path(args.out), model_dir))
    except ImportError as exc:
        print(json.dumps({"ok": False, "reason": "sovyx_not_importable", "detail": str(exc)}))
        return 1

    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
