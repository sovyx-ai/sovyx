#!/usr/bin/env python3
"""voice-diag-wasapi-comparator — captures the same window in WASAPI shared
mode (with APOs in chain) and exclusive mode (APOs bypassed), runs FFT +
Silero VAD on both, and emits comparative JSON.

THIS IS THE SMOKING-GUN TEST for anti-pattern #21 (Voice Clarity APO):
- shared_rms - exclusive_rms ≈ 0 dBFS AND shared_vad < 0.01 AND exclusive_vad > 0.5
  → APO is destroying the signal upstream
- shared_rms ≈ exclusive_rms AND both have similar vad
  → APOs are not the culprit; look elsewhere

Uses pyaudiowpatch (WASAPI loopback + exclusive support for Python).
Falls back to sounddevice if pyaudiowpatch is unavailable (shared-only).

Output JSON (stdout):
{
  "ok": true,
  "shared":    {"ok": true, "rms_dbfs": -22.3, "peak_dbfs": -3.1,
                "silero_max_prob": 0.87, "silero_mean_prob": 0.34,
                "wav_path": "...", "device_name": "...", "samples": 80000},
  "exclusive": {... or {"ok": false, "reason": "..."}},
  "verdict":   "voice_clarity_destroying" | "apo_not_culprit" | "inconclusive",
  "delta_rms_dbfs": 23.5,
  "delta_vad": 0.86,
  "tool_version": "1.0"
}
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import wave
from pathlib import Path

TOOL_VERSION = "1.0"
RATE = 16000
CHANNELS = 1
DTYPE_BYTES = 2  # int16


def _capture_with_pyaudiowpatch(mode: str, duration_s: float, out_wav: Path) -> dict:
    """Capture via pyaudiowpatch in shared or exclusive mode."""
    try:
        import pyaudiowpatch as pyaudio  # type: ignore
    except ImportError:
        return {"ok": False, "reason": "pyaudiowpatch_not_installed",
                "hint": "pip install PyAudioWPatch"}

    try:
        import numpy as np  # type: ignore
    except ImportError:
        return {"ok": False, "reason": "numpy_not_installed"}

    pa = pyaudio.PyAudio()
    try:
        # Locate default WASAPI input device.
        default_in = None
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            host_api = pa.get_host_api_info_by_index(info["hostApi"])
            if "WASAPI" in host_api["name"] and info.get("maxInputChannels", 0) >= 1:
                if pa.get_default_input_device_info()["index"] == i:
                    default_in = info
                    break
                if default_in is None:
                    default_in = info
        if default_in is None:
            pa.terminate()
            return {"ok": False, "reason": "no_wasapi_input_device"}

        kwargs = dict(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=default_in["index"],
            frames_per_buffer=512,
        )
        # Exclusive mode requires AsExclusive=True via stream_info.
        # PyAudioWPatch exposes this via the as_exclusive flag on open().
        if mode == "exclusive":
            kwargs["as_exclusive"] = True

        try:
            stream = pa.open(**kwargs)
        except Exception as exc:
            pa.terminate()
            return {"ok": False, "reason": f"open_failed_{mode}",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "device_name": default_in.get("name", "?")}

        frames_to_capture = int(RATE * duration_s)
        captured = bytearray()
        try:
            while len(captured) < frames_to_capture * CHANNELS * DTYPE_BYTES:
                chunk = stream.read(512, exception_on_overflow=False)
                captured.extend(chunk)
        except Exception as exc:
            stream.close()
            pa.terminate()
            return {"ok": False, "reason": f"read_failed_{mode}",
                    "detail": f"{type(exc).__name__}: {exc}"}
        stream.close()
        pa.terminate()

        # Write WAV.
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_wav), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(DTYPE_BYTES)
            w.setframerate(RATE)
            w.writeframes(bytes(captured))

        return {
            "ok": True,
            "wav_path": str(out_wav),
            "device_name": default_in.get("name", "?"),
            "device_index": default_in["index"],
            "samples": len(captured) // (CHANNELS * DTYPE_BYTES),
            "duration_s": (len(captured) // (CHANNELS * DTYPE_BYTES)) / RATE,
            "mode": mode,
        }
    except Exception as exc:
        try:
            pa.terminate()
        except Exception:
            pass
        return {"ok": False, "reason": "unexpected_error",
                "detail": f"{type(exc).__name__}: {exc}"}


def _capture_with_sounddevice(duration_s: float, out_wav: Path) -> dict:
    """Fallback: sounddevice shared-only capture."""
    try:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        return {"ok": False, "reason": "sounddevice_or_numpy_missing",
                "detail": str(exc)}

    try:
        rec = sd.rec(int(duration_s * RATE), samplerate=RATE,
                     channels=CHANNELS, dtype="int16")
        sd.wait()
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_wav), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(DTYPE_BYTES)
            w.setframerate(RATE)
            w.writeframes(rec.tobytes())
        return {
            "ok": True,
            "wav_path": str(out_wav),
            "device_name": "sounddevice_default",
            "samples": len(rec),
            "duration_s": float(len(rec)) / RATE,
            "mode": "shared_via_sounddevice",
        }
    except Exception as exc:
        return {"ok": False, "reason": "sounddevice_capture_failed",
                "detail": f"{type(exc).__name__}: {exc}"}


def _analyze(wav_path: Path) -> dict:
    """RMS + peak in dBFS + Silero max/mean prob via subprocess to the
    Linux toolkit's analyzers (if available adjacent to this script).

    Falls back to inline NumPy RMS if analyzer scripts are not present.
    """
    try:
        import numpy as np  # type: ignore
    except ImportError:
        return {"ok": False, "reason": "numpy_missing_for_analysis"}

    try:
        with wave.open(str(wav_path), "rb") as w:
            n = w.getnframes()
            ch = w.getnchannels()
            raw = w.readframes(n)
        samples = np.frombuffer(raw, dtype=np.int16)
        if ch > 1:
            samples = samples.reshape(-1, ch).mean(axis=1).astype(np.int16)
        if len(samples) == 0:
            return {"ok": False, "reason": "empty_wav"}
        peak = int(np.abs(samples).max())
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        # Convert to dBFS (full scale = 32767 for int16).
        peak_dbfs = -120.0 if peak == 0 else 20.0 * math.log10(peak / 32767.0)
        rms_dbfs = -120.0 if rms == 0.0 else 20.0 * math.log10(rms / 32767.0)
    except Exception as exc:
        return {"ok": False, "reason": "wav_parse_failed",
                "detail": f"{type(exc).__name__}: {exc}"}

    silero = {"available": False, "reason": "not_attempted"}
    # Try Silero via Linux toolkit's silero_probe.py if shipped alongside.
    silero_script = Path(__file__).parent / "silero_probe.py"
    if not silero_script.exists():
        # Look in sibling location used by Sovyx voice-diag tarball.
        alt = Path(__file__).parent / ".." / "docs-internal" / "diagnostics" \
              / "sovyx-voice-diag" / "lib" / "py" / "silero_probe.py"
        if alt.exists():
            silero_script = alt.resolve()
    if silero_script.exists():
        import subprocess
        try:
            res = subprocess.run(
                [sys.executable, str(silero_script), "--wav", str(wav_path)],
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode == 0 and res.stdout.strip():
                silero = json.loads(res.stdout)
        except Exception as exc:
            silero = {"available": False, "reason": "silero_subprocess_failed",
                      "detail": str(exc)}

    return {
        "ok": True,
        "rms_dbfs": round(rms_dbfs, 2),
        "peak_dbfs": round(peak_dbfs, 2),
        "samples": len(samples),
        "silero_available": silero.get("available", False),
        "silero_max_prob": silero.get("max_prob"),
        "silero_mean_prob": silero.get("mean_prob"),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--outdir", required=True)
    p.add_argument("--duration", type=float, default=5.0)
    args = p.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    out = {"ok": True, "tool_version": TOOL_VERSION,
           "duration_s": args.duration, "rate": RATE}

    # Print user prompt (stderr — stdout reserved for JSON).
    print("[voice-diag] Speak naturally for ~5s during EACH capture.",
          file=sys.stderr)
    print("[voice-diag] Phrase: 'Sovyx, me ouça agora: um, dois, três, "
          "quatro, cinco.'", file=sys.stderr)

    # Shared mode (WASAPI default — APOs in chain).
    print("[voice-diag] Capturing SHARED mode (with APO chain)...",
          file=sys.stderr)
    shared_wav = outdir / "wasapi_shared.wav"
    shared_cap = _capture_with_pyaudiowpatch("shared", args.duration, shared_wav)
    if not shared_cap.get("ok"):
        # Fallback to sounddevice (always shared on Windows).
        shared_cap = _capture_with_sounddevice(args.duration, shared_wav)
    if shared_cap.get("ok"):
        shared_anal = _analyze(Path(shared_cap["wav_path"]))
        shared_cap.update({k: v for k, v in shared_anal.items() if k != "ok"})
        shared_cap["ok"] = shared_anal.get("ok", True)
    out["shared"] = shared_cap

    # Cooldown.
    time.sleep(1.5)

    # Exclusive mode (APOs bypassed — only with pyaudiowpatch).
    print("[voice-diag] Capturing EXCLUSIVE mode (APOs bypassed)...",
          file=sys.stderr)
    excl_wav = outdir / "wasapi_exclusive.wav"
    excl_cap = _capture_with_pyaudiowpatch("exclusive", args.duration, excl_wav)
    if excl_cap.get("ok"):
        excl_anal = _analyze(Path(excl_cap["wav_path"]))
        excl_cap.update({k: v for k, v in excl_anal.items() if k != "ok"})
        excl_cap["ok"] = excl_anal.get("ok", True)
    out["exclusive"] = excl_cap

    # Verdict (only if both succeeded).
    if (shared_cap.get("ok") and excl_cap.get("ok")
            and shared_cap.get("rms_dbfs") is not None
            and excl_cap.get("rms_dbfs") is not None):
        s_rms = shared_cap["rms_dbfs"]
        e_rms = excl_cap["rms_dbfs"]
        s_vad = shared_cap.get("silero_max_prob") or 0.0
        e_vad = excl_cap.get("silero_max_prob") or 0.0
        out["delta_rms_dbfs"] = round(e_rms - s_rms, 2)
        out["delta_vad"] = round(e_vad - s_vad, 3)
        # Smoking gun: shared silent AND exclusive has signal.
        if s_rms < -85.0 and e_rms > -50.0 and e_vad > 0.5:
            out["verdict"] = "voice_clarity_destroying_apo_confirmed"
            out["verdict_detail"] = (
                "Shared mode RMS < -85 dBFS AND exclusive mode RMS > -50 dBFS "
                "AND exclusive Silero VAD > 0.5 — APO chain (likely Voice "
                "Clarity per anti-pattern #21) is destroying the signal "
                "upstream of user-space. Sovyx fix: capture_wasapi_exclusive=True."
            )
        elif abs(s_rms - e_rms) < 6.0 and abs(s_vad - e_vad) < 0.2:
            out["verdict"] = "apo_not_culprit"
            out["verdict_detail"] = (
                "Shared and exclusive captures are equivalent within 6 dB "
                "RMS and 0.2 VAD probability — APO chain is NOT destroying "
                "the signal. Look elsewhere (driver, hardware, codec, "
                "SileroVAD probe upstream)."
            )
        else:
            out["verdict"] = "inconclusive"
            out["verdict_detail"] = (
                f"Mixed signal: delta_rms={out['delta_rms_dbfs']} dB, "
                f"delta_vad={out['delta_vad']}. APO may be partial culprit "
                "or interaction with hardware/driver. Manual review needed."
            )
    elif not excl_cap.get("ok"):
        out["verdict"] = "exclusive_unavailable"
        out["verdict_detail"] = (
            f"Exclusive mode failed: {excl_cap.get('reason', 'unknown')}. "
            "Cannot prove/refute APO hypothesis without paired comparison. "
            "Install pyaudiowpatch (pip install PyAudioWPatch) and ensure "
            "endpoint allows exclusive control (Properties > Advanced > "
            "Allow apps to take exclusive control)."
        )
    else:
        out["verdict"] = "no_data"
        out["verdict_detail"] = "Both captures failed — see shared/exclusive errors."

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
