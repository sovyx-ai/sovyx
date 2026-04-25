# Pilot Run Instructions — Windows Voice Diagnostic

**Tool:** `scripts/diagnose-voice-windows.ps1` v2 (2026-04-24)
**Companion:** `scripts/voice-diag-wasapi-comparator.py`

## What it collects (16 sections)

| # | Section | Purpose |
|---|---|---|
| 1 | Windows + PowerShell fingerprint | OS build/UBR/locale/edition |
| 2 | MMDevices Capture endpoints + APO chain | FxProperties decode + CLSID extract |
| 3 | Per-endpoint policy flags | Disable_SysFx, exclusive mode allow |
| 4 | Third-party DSP processes + installed packages | Razer, NVIDIA Broadcast, Krisp, Discord, etc. |
| 5 | PortAudio enumeration via Sovyx Python | Mirror of `sd.query_devices()` |
| 6 | sovyx doctor voice_capture_apo + voice_capture_kernel_invalidated | Sovyx-side diagnosis |
| 7 | Sovyx data dir snapshot (sidecars: combos, overrides, quarantine, system.yaml) | Config + state |
| 8 | Live `/api/voice/capture-diagnostics` probe (if daemon running) | Runtime cascade view |
| 8b | Daemon log copy (`~/.sovyx/logs/sovyx.log`) | Sovyx own view |
| **10** | **Hardware (BIOS/Board/PnP audio devices + DEVPKEYs)** | OEM, driver versions, signing dates — ESSENCIAL anti-pattern #21 |
| **11** | **Recent HotFixes + audio services + System event log** | UBR-delivered KB correlation; Audio service crash detection |
| **12** | **APO CLSID → InprocServer32 DLL → AuthenticodeSignature → KB** | Smoking-gun resolution of which KB delivered which APO DLL |
| **13** | **ConsentStore (microphone) + Defender exclusions + AppLocker** | Privacy denial + AV interference + WDAC |
| **14** | **WASAPI shared vs exclusive comparator probe** | Direct proof/refutation of anti-pattern #21 |
| **15** | **Network reachability (Anthropic/OpenAI/Google/Deepgram/ElevenLabs)** | LLM/STT/TTS endpoint health |
| **16** | **ETW capture (opt-in `-CaptureEtw`)** | wpr.exe gold-standard kernel/audio telemetry |

## Phase 1 — Smoke run (no admin, no live capture)

Run with default flags. ~30s. Validates collection works on your hardware:

```powershell
cd E:\sovyx
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose-voice-windows.ps1
```

**Validate after run:**

1. Open `tmp\voice-diag\sovyx-voice-diagnostic.json` — check that:
   - `windows.build` is present
   - `audio_endpoints` array has at least 1 ACTIVE entry
   - `pnp_audio_devices` array is non-empty
   - `hotfixes_recent` has the most recent ~30 KBs sorted DESC
   - `apo_dll_resolution` has resolved at least 1 CLSID with `dll_path` populated
   - `network_llm` shows DNS+TCP for the providers you use
   - `errors` array is empty (or only has expected entries — e.g., no `~/.sovyx/system.yaml`)

2. Open `tmp\voice-diag\sovyx-voice-diagnostic.log` (transcript) — should have no PowerShell stack traces.

3. If WASAPI comparator reports `verdict: voice_clarity_destroying_apo_confirmed` → the APO chain on your default mic is destroying signal upstream of Python. Sovyx fix: enable `capture_wasapi_exclusive` in `system.yaml` (anti-pattern #21).

## Phase 2 — Full run with live capture (regular user)

Speak naturally during the WASAPI comparator step (~10s total: 5s shared + 5s exclusive):

```powershell
cd E:\sovyx
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose-voice-windows.ps1
# When prompted: speak "Sovyx, me ouça agora: um, dois, três, quatro, cinco."
# during BOTH the shared mode capture and the exclusive mode capture.
```

**Pre-req for WASAPI exclusive:** install pyaudiowpatch in your Sovyx Python:
```powershell
& C:\path\to\sovyx\python.exe -m pip install PyAudioWPatch
```

If pyaudiowpatch is unavailable, the comparator falls back to shared-only via sounddevice — verdict will be `exclusive_unavailable`.

## Phase 3 — ETW deep capture (admin, opt-in, ~50-200 MB .etl)

For diagnosing transient glitches (USB transfer errors, DPC spikes):

```powershell
# Run as Administrator
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose-voice-windows.ps1 -CaptureEtw -CaptureEtwSeconds 30
```

To use Microsoft's audio-specific ETW profile (better signal-to-noise):

```powershell
# 1. Get audio.wprp from https://github.com/microsoft/audio (MIT)
git clone https://github.com/microsoft/audio C:\tmp\msaudio
# 2. Pass it to the diag
powershell -ExecutionPolicy Bypass -File .\scripts\diagnose-voice-windows.ps1 `
    -CaptureEtw -CaptureEtwProfile C:\tmp\msaudio\Tools\audio.wprp -CaptureEtwSeconds 30
```

The `.etl` file is opaque without WPA — analyst opens it with Windows Performance Analyzer or `wpaexporter.exe -i etw_audio_capture.etl -profile <wpaProfile> -o csv`.

## What to send back

Zip the entire `tmp\voice-diag\` directory:

```powershell
Compress-Archive -Path E:\sovyx\tmp\voice-diag\* -DestinationPath sovyx-voice-diag-windows.zip
```

Send `sovyx-voice-diag-windows.zip`. Total size ~1-5 MB without ETW, ~50-200 MB with `-CaptureEtw`.

## Quick success criteria

✅ Phase 1 passes if `errors: []` (or only `python_or_sounddevice_unavailable` if Python missing)
✅ Phase 2 passes if `live_captures.shared.ok = true` AND verdict is one of `voice_clarity_destroying_apo_confirmed`, `apo_not_culprit`, or `exclusive_unavailable`
✅ Phase 3 passes if `etw_capture.ok = true` and `.etl` file is non-empty

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `mmdevices_enum_failed` | Running as non-admin on locked-down enterprise build | Try as admin OR check HKLM permissions |
| `pnp_enum_failed` | PnP cmdlets not loaded (rare) | `Import-Module PnpDevice` |
| `python_or_sounddevice_unavailable` | Python not in PATH or sounddevice not in venv | Activate Sovyx venv first |
| `verdict: exclusive_unavailable` | pyaudiowpatch missing OR endpoint blocks exclusive control | `pip install PyAudioWPatch`; check Properties > Advanced > Allow apps to take exclusive control |
| `etw_capture.reason = requires_admin` | Phase 3 needs elevation | Re-run as Administrator |
