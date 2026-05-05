#!/usr/bin/env python3
"""sd_capture — captura via PortAudio (sounddevice) e salva WAV + metadados.

Resolve o device index DINAMICAMENTE via nome-substring, nunca hardcodado
(R7 do ADR-001). Salva:
    - <out_path> (.wav S16_LE)
    - <out_path>.capture_meta.json (device, host_api, sample_rate, latency,
      cpu_load, underflow/overflow counts, duration_s_actual, sanity_pass)

Uso:
    sd_capture.py --device-substring "default" --rate 16000 --channels 1 \
                  --duration 7 --out PATH.wav [--device-index N] [--silent]
                  [--min-duration-ratio 0.95] [--min-samples-ratio 0.90]

Exit codes:
    0 — captura ok (todos os gates de sanidade passaram)
    1 — sounddevice não importável
    2 — device não resolvido
    3 — captura falhou (exceção no callback ou abertura do stream)
    4 — captura encerrou cedo (duration_s_actual < min-duration-ratio * requested)
    5 — captura completou mas com poucos frames
        (total_frames < min-samples-ratio * rate * duration)

Sanity floor (v2 — audit post-SVX-VOICE-LINUX-20260422):
    Toda chamada de abertura de stream que falhar IMEDIATAMENTE deixa de
    pular o ``time.sleep`` previsto — o script volta rc=3 com traceback
    completo em stderr, e o runner bash detecta que a duração real ficou
    abaixo do mínimo e sinaliza a captura como FAIL (em vez de "ok com
    arquivo vazio").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
import wave
from pathlib import Path


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` atomically via tempfile in same dir + os.replace.

    AUDIT v3: previously ``meta_path.write_text(...)`` wrote directly,
    leaving a partial JSON on disk if the process was killed mid-write.
    Forensic artifacts can't tolerate half-written meta.
    """
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


def _resolve_device(sd, substring: str | None, explicit_index: int | None) -> tuple[int, dict]:
    """Retorna (index, device_info). Raises ValueError se não achar."""
    devices = sd.query_devices()
    if explicit_index is not None and explicit_index >= 0:
        if explicit_index >= len(devices):
            raise ValueError(f"explicit index {explicit_index} out of range ({len(devices)} devices)")
        return explicit_index, devices[explicit_index]

    if not substring:
        # Usa default input.
        default = sd.default.device
        if isinstance(default, (list, tuple)):
            idx = default[0] if default[0] is not None else 0
        else:
            idx = default if default is not None else 0
        return int(idx), devices[int(idx)]

    # Case-insensitive substring match; prioriza devices de INPUT.
    needle = substring.lower()
    candidates: list[tuple[int, dict]] = []
    for i, dev in enumerate(devices):
        name = str(dev.get("name", ""))
        if needle in name.lower() and int(dev.get("max_input_channels", 0) or 0) > 0:
            candidates.append((i, dev))

    if not candidates:
        # Relaxa para qualquer device com o substring (para playback tests).
        for i, dev in enumerate(devices):
            name = str(dev.get("name", ""))
            if needle in name.lower():
                candidates.append((i, dev))

    if not candidates:
        raise ValueError(f"no device matched substring {substring!r}")

    # Prefer exatch-lowercase match se houver.
    for i, dev in candidates:
        if str(dev.get("name", "")).lower() == needle:
            return i, dev
    return candidates[0]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--device-substring", default="",
                   help="Substring do nome do device (case-insensitive)")
    p.add_argument("--device-index", type=int, default=-1,
                   help="Index explícito; ignora substring se ≥ 0")
    p.add_argument("--rate", type=int, default=16000)
    p.add_argument("--channels", type=int, default=1)
    p.add_argument("--duration", type=float, default=7.0)
    p.add_argument("--out", required=True)
    p.add_argument("--silent", action="store_true",
                   help="Não emite stdout (modo silencioso para baseline de ruído)")
    p.add_argument("--min-duration-ratio", type=float, default=0.95,
                   help="Fração mínima da duração solicitada que a captura real "
                        "precisa atingir (default 0.95). Se a captura terminar "
                        "antes (p.ex. stream abriu e morreu), retorna rc=4.")
    p.add_argument("--min-samples-ratio", type=float, default=0.90,
                   help="Fração mínima de total_frames/expected_frames (default "
                        "0.90). Tolera perda de buffers por input_overflow mas "
                        "detecta capturas truncadas (rc=5).")
    args = p.parse_args()

    try:
        import sounddevice as sd  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:
        print(json.dumps({"error": "sounddevice_or_numpy_not_importable", "detail": str(exc)}))
        return 1

    explicit = args.device_index if args.device_index >= 0 else None
    try:
        idx, dev_info = _resolve_device(sd, args.device_substring, explicit)
    except ValueError as exc:
        print(json.dumps({"error": "device_not_resolved", "detail": str(exc)}))
        return 2

    host_api_idx = int(dev_info.get("hostapi", -1))
    host_apis = sd.query_hostapis()
    host_api_name = host_apis[host_api_idx]["name"] if 0 <= host_api_idx < len(host_apis) else "unknown"

    # Captura com callback — acumula em lista para evitar buffer fixo que
    # pode overflown em sistemas lentos.
    frames: list = []
    overflow_count = 0
    underflow_count = 0
    errors: list[str] = []

    def _cb(indata, _frames_in_buffer, _time_info, status):
        nonlocal overflow_count, underflow_count
        if status:
            if getattr(status, "input_overflow", False):
                overflow_count += 1
            if getattr(status, "input_underflow", False):
                underflow_count += 1
            if getattr(status, "output_overflow", False) or getattr(status, "output_underflow", False):
                # Não deveria acontecer em InputStream, registra por segurança.
                errors.append(f"unexpected_output_status: {status}")
        frames.append(indata.copy())

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = out_path.with_suffix(out_path.suffix + ".capture_meta.json")

    start_wall = time.time()
    # Build the stream FIRST and only start the capture clock after the
    # stream is running — otherwise a slow ``InputStream`` init (PortAudio
    # negotiating sample rate with PipeWire can take 50-300 ms) inflates
    # ``duration_s_actual`` and masks a short real recording window.
    stream: "sd.InputStream | None" = None  # type: ignore[name-defined]
    try:
        stream = sd.InputStream(
            device=idx,
            samplerate=args.rate,
            channels=args.channels,
            dtype="int16",
            blocksize=512,
            latency="low",
            callback=_cb,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced via stderr + JSON
        # PortAudio failures (busy device, unsupported rate, driver panic)
        # land here. Without the traceback, the forensic log only sees a
        # one-line message like "Error opening InputStream: ..." — not
        # enough to tell PortAudio-busy from format-mismatch from driver-
        # crash. Traceback goes to stderr so the bash caller's tee still
        # captures it into ``sd_capture.log``.
        print(
            json.dumps(
                {
                    "error": "stream_open_failed",
                    "detail": str(exc),
                    "device_index": idx,
                    "requested_rate": args.rate,
                    "requested_channels": args.channels,
                },
            ),
        )
        traceback.print_exc(file=sys.stderr)
        return 3

    # Clock start AFTER a successful open so the reported duration
    # reflects real capture time, not init overhead.
    start_mono = time.monotonic_ns()
    try:
        with stream:
            time.sleep(args.duration)
            cpu_load = float(getattr(stream, "cpu_load", 0.0))
            actual_sr = int(getattr(stream, "samplerate", args.rate))
            actual_latency = float(getattr(stream, "latency", 0.0))
    except Exception as exc:  # noqa: BLE001 — mid-capture failures
        print(
            json.dumps(
                {
                    "error": "stream_runtime_failed",
                    "detail": str(exc),
                    "device_index": idx,
                },
            ),
        )
        traceback.print_exc(file=sys.stderr)
        return 3
    end_mono = time.monotonic_ns()

    # Save WAV S16_LE.
    #
    # AUDIT v3: assert dtype int16 BEFORE serialization so a future
    # refactor that accidentally changes the callback to float32
    # doesn't silently write garbage (2× bytes-per-sample mismatch vs
    # ``setsampwidth(2)``). A silent WAV corruption here would feed
    # all downstream analysis (analyze_wav, silero_probe) a broken
    # signal.
    if frames:
        data = np.concatenate(frames)
    else:
        data = np.zeros((0, args.channels), dtype="int16")
    if data.dtype != np.int16:
        print(
            json.dumps(
                {
                    "error": "callback_dtype_mismatch",
                    "detail": f"expected int16, got {data.dtype!s}",
                    "device_index": idx,
                },
            ),
        )
        return 3

    try:
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(args.channels)
            w.setsampwidth(2)
            w.setframerate(actual_sr)
            w.writeframes(data.tobytes())
    except (OSError, wave.Error) as exc:
        print(
            json.dumps(
                {
                    "error": "wav_write_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "device_index": idx,
                },
            ),
        )
        return 3

    # ── Sanity gates (audit post-SVX-VOICE-LINUX-20260422, v3) ───────
    #
    # AUDIT v3 — report BOTH timings so analyst can spot the divergence:
    #
    #   * duration_s_wall = (monotonic end − monotonic start) / 1e9
    #     Wall-clock of the ``with stream:`` block. Includes PortAudio
    #     close overhead and anything blocking the event loop.
    #
    #   * duration_s_from_audio = total_frames / actual_sr
    #     Strictly the count of samples the callback delivered,
    #     divided by the stream's actual sample rate. This is the
    #     ONLY audio-accurate duration — if a device disconnects
    #     mid-sleep, wall-clock still reaches ``args.duration`` but
    #     from_audio reports the real gap.
    #
    # ``duration_s_actual`` kept for schema back-compat (= wall-clock)
    # but analysts should prefer from_audio for truth.
    duration_s_wall = (end_mono - start_mono) / 1e9
    total_frames = int(data.shape[0]) if data.ndim > 0 else 0
    duration_s_from_audio = total_frames / actual_sr if actual_sr else 0.0
    expected_frames = int(args.rate * args.duration)
    min_duration_s = args.duration * args.min_duration_ratio
    min_frames = int(expected_frames * args.min_samples_ratio)

    # Gate on the MORE CONSERVATIVE of wall-clock and audio-derived
    # duration — refuse "ok" unless both satisfy the floor.
    duration_pass_wall = duration_s_wall >= min_duration_s
    duration_pass_audio = duration_s_from_audio >= min_duration_s
    duration_pass = duration_pass_wall and duration_pass_audio
    frames_pass = total_frames >= min_frames

    sanity_checks: list[dict[str, object]] = [
        {
            "gate": "duration_wall_floor",
            "pass": duration_pass_wall,
            "actual_s": round(duration_s_wall, 3),
            "required_s": round(min_duration_s, 3),
            "ratio_required": args.min_duration_ratio,
        },
        {
            "gate": "duration_from_audio_floor",
            "pass": duration_pass_audio,
            "actual_s": round(duration_s_from_audio, 3),
            "required_s": round(min_duration_s, 3),
            "ratio_required": args.min_duration_ratio,
        },
        {
            "gate": "frame_count_floor",
            "pass": frames_pass,
            "actual_frames": total_frames,
            "required_frames": min_frames,
            "ratio_required": args.min_samples_ratio,
        },
    ]

    meta = {
        # AUDIT v3 — schema unification with C_alsa + D_pipewire.
        "capture_id": Path(args.out).stem,
        "layer": "E_portaudio",
        "tool": "sounddevice",
        "device_index": idx,
        "device_name": str(dev_info.get("name", "")),
        "host_api": host_api_name,
        "requested_rate": args.rate,
        "actual_rate": actual_sr,
        "channels": args.channels,
        "format": "S16_LE",  # enforced by ``dtype="int16"`` above
        "duration_s_requested": args.duration,
        "duration_s_actual": round(duration_s_wall, 3),
        "duration_s_wall": round(duration_s_wall, 3),
        "duration_s_from_audio": round(duration_s_from_audio, 3),
        "wall_start": start_wall,
        "monotonic_ns_start": start_mono,
        "monotonic_ns_end": end_mono,
        "blocksize": 512,
        "latency_actual_s": actual_latency,
        "cpu_load": cpu_load,
        "input_overflow_count": overflow_count,
        "input_underflow_count": underflow_count,
        "callback_errors": errors,
        "total_frames": total_frames,
        "expected_frames": expected_frames,
        "output_wav": str(out_path),
        "sanity_checks": sanity_checks,
        "sanity_pass": duration_pass and frames_pass,
    }
    _atomic_write_text(meta_path, json.dumps(meta, indent=2))

    if not duration_pass:
        print(
            json.dumps(
                {
                    "error": "capture_too_short",
                    "detail": (
                        f"wall {duration_s_wall:.3f}s / audio {duration_s_from_audio:.3f}s "
                        f"< mínimo {min_duration_s:.3f}s (ratio {args.min_duration_ratio:.2f}) — "
                        "provável stream aberto e fechado sem gravar"
                    ),
                    "duration_s_wall": round(duration_s_wall, 3),
                    "duration_s_from_audio": round(duration_s_from_audio, 3),
                    "duration_pass_wall": duration_pass_wall,
                    "duration_pass_audio": duration_pass_audio,
                    "meta": str(meta_path),
                },
            ),
        )
        return 4
    if not frames_pass:
        print(
            json.dumps(
                {
                    "error": "capture_too_few_frames",
                    "detail": (
                        f"{total_frames} frames < mínimo {min_frames} "
                        f"(ratio {args.min_samples_ratio:.2f}); overflow_count="
                        f"{overflow_count}"
                    ),
                    "meta": str(meta_path),
                },
            ),
        )
        return 5

    if not args.silent:
        sys.stdout.write(json.dumps({"ok": True, "meta": str(meta_path)}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
