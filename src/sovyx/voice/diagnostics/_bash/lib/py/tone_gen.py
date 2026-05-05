#!/usr/bin/env python3
"""tone_gen — gera WAV de tom puro (sine) para teste de cadeia de playback.

Uso:
    tone_gen.py --out PATH.wav [--freq 440] [--duration 1.0] [--rate 48000]
                [--channels 2] [--amplitude 0.5]

Default: 440 Hz, 1 s, 48 kHz estéreo, amplitude 0.5 (pico -6 dBFS).

Stdlib only (math + struct + wave).
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--freq", type=float, default=440.0)
    p.add_argument("--duration", type=float, default=1.0)
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--channels", type=int, default=2)
    p.add_argument("--amplitude", type=float, default=0.5,
                   help="Amplitude 0..1 (0.5 = -6 dBFS pico)")
    args = p.parse_args()

    if not 0 < args.amplitude <= 1.0:
        print("amplitude must be in (0, 1]", file=sys.stderr)
        return 2

    n_samples = int(args.duration * args.rate)
    peak = int(args.amplitude * 32767)

    # Envelope de 5ms in/out para evitar clicks.
    fade = max(1, int(0.005 * args.rate))

    frames = bytearray()
    for i in range(n_samples):
        # Envelope.
        if i < fade:
            env = i / fade
        elif i > n_samples - fade:
            env = (n_samples - i) / fade
        else:
            env = 1.0
        sample = int(env * peak * math.sin(2 * math.pi * args.freq * i / args.rate))
        for _ in range(args.channels):
            frames.extend(struct.pack("<h", sample))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(args.channels)
        w.setsampwidth(2)
        w.setframerate(args.rate)
        w.writeframes(bytes(frames))

    print(f"generated {out} ({n_samples} frames, {args.freq} Hz, {args.rate} Hz, "
          f"{args.channels} ch, amp={args.amplitude})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
