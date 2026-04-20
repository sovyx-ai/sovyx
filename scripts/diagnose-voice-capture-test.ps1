# ======================================================================
# Sovyx — Voice Capture Isolation Test (Windows)
#
# Captures 5 seconds of mic audio in 4 different configurations to
# isolate EXACTLY where the signal is lost between hardware and VAD:
#
#   Test A: WASAPI shared, STEREO 48 kHz (device native format)
#           -> reveals if one of the two channels is silent
#   Test B: WASAPI shared, MONO 16 kHz auto_convert=true
#           -> reproduces EXACTLY what Sovyx does (and fails)
#   Test C: DirectSound, MONO 44.1 kHz
#           -> bypasses WASAPI quirks, still routes through APOs
#   Test D: WDM-KS, MONO 44.1 kHz
#           -> bypasses the entire Windows audio engine incl. APOs
#
# For each capture we measure RMS/peak per channel, save a WAV to
# listen back, and run Silero VAD offline on the buffer to detect
# whether the spectral content survives.
#
# REQUIRED: you must speak during each 5-second window (count 1..5).
#
# OUTPUT
#   tmp\voice-diag\capture-tests\
#     ├── A_wasapi_stereo.wav
#     ├── B_wasapi_mono_autoconvert.wav
#     ├── C_directsound_mono.wav
#     ├── D_wdmks_mono.wav
#     └── test-summary.json     (all RMS/VAD metrics)
#
# USAGE (NO admin required)
#   cd E:\sovyx
#   powershell -ExecutionPolicy Bypass -File .\scripts\diagnose-voice-capture-test.ps1
# ======================================================================

[CmdletBinding()]
param(
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$repoRoot  = Split-Path -Parent $scriptDir
if (-not $OutDir) { $OutDir = Join-Path $repoRoot 'tmp\voice-diag\capture-tests' }
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

Write-Host ''
Write-Host '================================================================'
Write-Host ' Sovyx Voice Capture Isolation Test'
Write-Host '================================================================'
Write-Host 'This script captures 5 seconds of audio in 4 different modes.'
Write-Host ''
Write-Host 'FOR EACH TEST, when you see "SPEAK NOW":'
Write-Host '  Count clearly from 1 to 5 in your normal speaking voice.'
Write-Host ''
Write-Host "Output dir: $OutDir"
Write-Host ''

$pythonScript = @'
import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

# Four targeted test configurations. device_id matches the indices from
# the earlier portaudio-enum.json on this machine.
TESTS = [
    {
        "code": "A",
        "name": "wasapi_stereo_native",
        "desc": "WASAPI shared, STEREO 48 kHz (device native)",
        "device": 15, "channels": 2, "rate": 48000,
        "exclusive": False, "auto_convert": False,
    },
    {
        "code": "B",
        "name": "wasapi_mono_autoconvert",
        "desc": "WASAPI shared, MONO 16 kHz auto_convert=True (what Sovyx does)",
        "device": 15, "channels": 1, "rate": 16000,
        "exclusive": False, "auto_convert": True,
    },
    {
        "code": "C",
        "name": "directsound_mono",
        "desc": "DirectSound, MONO 44.1 kHz (APO chain still active)",
        "device": 7, "channels": 1, "rate": 44100,
        "exclusive": False, "auto_convert": False,
    },
    {
        "code": "D",
        "name": "wdmks_mono",
        "desc": "WDM-KS, MONO 44.1 kHz (bypasses Windows audio engine)",
        "device": 18, "channels": 1, "rate": 44100,
        "exclusive": False, "auto_convert": False,
    },
]

DURATION_S = 5


def build_extra(cfg):
    if "wasapi" not in cfg["name"]:
        return None
    try:
        return sd.WasapiSettings(
            exclusive=cfg["exclusive"],
            auto_convert=cfg["auto_convert"],
        )
    except TypeError:
        # Older sounddevice: only exclusive kwarg
        try:
            return sd.WasapiSettings(exclusive=cfg["exclusive"])
        except Exception:
            return None


def capture(cfg, duration_s):
    extra = build_extra(cfg)
    try:
        rec = sd.rec(
            int(duration_s * cfg["rate"]),
            samplerate=cfg["rate"],
            channels=cfg["channels"],
            dtype="int16",
            device=cfg["device"],
            extra_settings=extra,
        )
        sd.wait()
        return True, rec, None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def channel_stats(buf, label, channel=None):
    if buf.ndim == 1:
        data = buf.astype(np.float64)
    elif channel is None:
        data = buf.astype(np.float64).mean(axis=1)
    else:
        data = buf[:, channel].astype(np.float64)

    if data.size == 0:
        return None
    rms = float(np.sqrt(np.mean(data ** 2)))
    peak = float(np.max(np.abs(data)))
    rms_db = 20 * np.log10(rms / 32768 + 1e-10) if rms > 0 else -200.0
    peak_db = 20 * np.log10(peak / 32768 + 1e-10) if peak > 0 else -200.0
    zero_ratio = float(np.mean(data == 0))
    nonzero_frames = int(np.sum(np.abs(data) > 32))  # above ~10 bits of signal
    return {
        "label": label,
        "samples": int(data.size),
        "rms_db": round(rms_db, 2),
        "peak_db": round(peak_db, 2),
        "zero_ratio": round(zero_ratio, 4),
        "nonzero_frames_gt_32": nonzero_frames,
    }


def save_wav(path, buf, rate, channels):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(buf.tobytes())


def simple_resample_to_16k(buf_mono_int16, src_rate):
    """Polyphase-ish decimation via linear interp. Good enough for VAD probes."""
    if src_rate == 16000:
        return buf_mono_int16
    dst_n = int(len(buf_mono_int16) * 16000 / src_rate)
    xp = np.linspace(0, len(buf_mono_int16) - 1, dst_n)
    interp = np.interp(xp, np.arange(len(buf_mono_int16)), buf_mono_int16.astype(np.float64))
    return interp.astype(np.int16)


def run_silero_vad(buf_int16, src_rate):
    try:
        import onnxruntime as ort
    except ImportError:
        return {"error": "onnxruntime_not_available"}

    model_path = Path.home() / ".sovyx" / "models" / "voice" / "silero_vad.onnx"
    if not model_path.exists():
        return {"error": f"model_not_found: {model_path}"}

    # Always mix to mono and resample to 16 kHz for Silero v5
    if buf_int16.ndim > 1:
        mono = buf_int16.astype(np.float64).mean(axis=1).astype(np.int16)
    else:
        mono = buf_int16.copy()
    mono = simple_resample_to_16k(mono, src_rate)

    try:
        sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    except Exception as e:
        return {"error": f"session_init_failed: {e!r}"}

    # Silero v5 expects 512-sample windows at 16 kHz, with stateful LSTM
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(16000, dtype=np.int64)
    probs = []
    window = 512
    try:
        for i in range(0, len(mono) - window + 1, window):
            chunk = mono[i:i + window].astype(np.float32) / 32768.0
            inputs = {
                "input": chunk.reshape(1, -1),
                "state": state,
                "sr": sr,
            }
            out = sess.run(None, inputs)
            probs.append(float(out[0][0][0]))
            state = out[1]
    except Exception as e:
        return {"error": f"inference_failed: {e!r}", "frames_done": len(probs)}

    if not probs:
        return {"frames": 0, "error": "no_frames"}

    arr = np.array(probs)
    return {
        "frames": int(arr.size),
        "max_prob": round(float(arr.max()), 4),
        "mean_prob": round(float(arr.mean()), 4),
        "p95_prob": round(float(np.percentile(arr, 95)), 4),
        "frames_above_0_5": int(np.sum(arr > 0.5)),
        "frames_above_0_3": int(np.sum(arr > 0.3)),
    }


def countdown(n, prefix=""):
    for i in range(n, 0, -1):
        sys.stdout.write(f"\r{prefix}{i}...")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r" + " " * (len(prefix) + 10) + "\r")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot PortAudio state for reproducibility
    enum_snapshot = {
        "default_input": sd.default.device[0] if sd.default.device else None,
        "hostapis": [dict(a) for a in sd.query_hostapis()],
    }

    results = []
    for idx, cfg in enumerate(TESTS):
        print()
        print("=" * 64)
        print(f"Test {cfg['code']}: {cfg['desc']}")
        print(f"  device={cfg['device']}  rate={cfg['rate']} Hz  channels={cfg['channels']}")
        print("=" * 64)
        print("Get ready... speak when you see 'SPEAK NOW'")
        countdown(3, prefix="  Starting in ")
        print(f"  >>> SPEAK NOW for {DURATION_S} seconds (count 1..5) <<<")

        t0 = time.perf_counter()
        ok, rec, err = capture(cfg, DURATION_S)
        t1 = time.perf_counter()

        record = {
            "code": cfg["code"],
            "name": cfg["name"],
            "cfg": cfg,
            "ok": ok,
            "error": err,
            "capture_seconds": round(t1 - t0, 3),
        }

        if not ok:
            print(f"  !! OPEN FAILED: {err}")
            results.append(record)
            continue

        # Save WAV
        wav_path = out_dir / f"{cfg['code']}_{cfg['name']}.wav"
        save_wav(wav_path, rec, cfg["rate"], cfg["channels"])
        record["wav"] = str(wav_path)
        record["wav_size_bytes"] = wav_path.stat().st_size

        # Per-channel stats
        ch_stats = []
        ch_stats.append(channel_stats(rec, "mixed"))
        if rec.ndim > 1:
            for c in range(rec.shape[1]):
                ch_stats.append(channel_stats(rec, f"ch{c}", channel=c))
        record["channels"] = [s for s in ch_stats if s]

        for s in record["channels"]:
            marker = "ALIVE" if s["rms_db"] > -60 else ("WEAK " if s["rms_db"] > -80 else "SILENT")
            print(f"  [{s['label']:>6}] rms={s['rms_db']:+7.2f} dB  peak={s['peak_db']:+7.2f} dB  zeros={s['zero_ratio']:.1%}  {marker}")

        # Run Silero VAD offline on the captured buffer
        vad = run_silero_vad(rec, cfg["rate"])
        record["silero_vad"] = vad
        if "error" in vad:
            print(f"  [vad   ] ERROR: {vad['error']}")
        else:
            print(f"  [vad   ] max={vad['max_prob']:.3f}  mean={vad['mean_prob']:.3f}  "
                  f"frames>0.5={vad['frames_above_0_5']}/{vad['frames']}")

        results.append(record)
        time.sleep(1)

    summary = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "portaudio": enum_snapshot,
        "tests": results,
    }
    summary_path = out_dir / "test-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print()
    print("=" * 64)
    print(" VERDICT TABLE")
    print("=" * 64)
    print(f"{'test':<8} {'config':<45} {'rms_db':>10} {'vad_max':>8}")
    print("-" * 72)
    for r in results:
        label = f"{r['code']}"
        desc = r['cfg']['desc'][:44]
        if not r['ok']:
            print(f"{label:<8} {desc:<45} {'FAILED':>10} {'-':>8}")
            continue
        mix = next((s for s in r['channels'] if s['label'] == 'mixed'), None)
        rms = f"{mix['rms_db']:+.1f}" if mix else "-"
        vad = r.get('silero_vad', {})
        vmax = f"{vad.get('max_prob', 0):.3f}" if 'max_prob' in vad else "err"
        print(f"{label:<8} {desc:<45} {rms:>10} {vmax:>8}")
        # Per-channel for stereo
        for s in r['channels']:
            if s['label'].startswith('ch'):
                print(f"         (channel {s['label']})                              {s['rms_db']:+.1f}")
    print()
    print(f"Summary JSON: {summary_path}")
    print(f"WAV files in: {out_dir}")
    print("Listen to each WAV to verify what your mic really delivered.")


if __name__ == "__main__":
    main()
'@

$pyFile = Join-Path $OutDir '_run_tests.py'
Set-Content -Path $pyFile -Value $pythonScript -Encoding UTF8

Write-Host 'Launching Python test runner via uv (installs sounddevice/onnxruntime if needed)...'
Write-Host ''

Push-Location $repoRoot
try {
    & uv run python $pyFile --out-dir $OutDir
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($exit -ne 0) {
    Write-Warning "Python runner exited with code $exit"
}

Write-Host ''
Write-Host '================================================================'
Write-Host " DONE. Artifacts under: $OutDir"
Write-Host '================================================================'
Write-Host ''
Write-Host 'Next: send the test-summary.json contents to the assistant.'
Write-Host 'Optional: listen to each WAV to sanity-check what was captured.'
