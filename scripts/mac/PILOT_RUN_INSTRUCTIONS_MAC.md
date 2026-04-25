# Pilot Run Instructions — macOS Voice Diagnostic

**Tool:** `sovyx-voice-diag-mac.sh` v1.0
**Helpers:** `coreaudio-dump.swift`, `tcc-mic-reader.py`, `hal-plugin-classifier.sh`
**Output:** `~/sovyx-diag-mac-<host>-<ts>-<uuid>.tar.gz`

## What it collects (15 layers)

| Layer | Purpose | Tools |
|---|---|---|
| A — Hardware | BIOS/board, audio chipset, USB+Bluetooth, IOReg audio devices | `system_profiler`, `ioreg` |
| B — Kernel/OS | macOS version, recent updates, kexts, system extensions, SIP, boot args | `sw_vers`, `softwareupdate`, `kmutil`, `systemextensionsctl`, `csrutil` |
| C — CoreAudio engine | All devices + per-device formats/latency/data sources | `coreaudio-dump.swift` (custom Swift CLI) + `SwitchAudioSource` if installed |
| D — coreaudiod + HAL | Daemon state + HAL plug-ins + AU components + DEXTs (interceptor classification) | `launchctl`, `hal-plugin-classifier.sh` (Krisp/BlackHole/Loopback detection) |
| E — PortAudio | sounddevice version + query_devices + pip list | Python in Sovyx venv |
| F — Session/TCC | TCC.db microphone consents + Python/sovyx codesign + entitlements + AudioMIDISetup prefs | `tcc-mic-reader.py`, `codesign`, `osascript` |
| G — Sovyx runtime | sovyx version, doctor voice, data dir snapshot, log tail | sovyx CLI |
| W — Live capture | 5s mic recording + analyze_wav.py + silero_probe.py | sounddevice + Linux toolkit analyzers (reused) |
| I — Network | DNS+TCP to LLM endpoints + macOS firewall state | `dig`, `nc`, `socketfilterfw` |
| K — Unified logs | log show coreaudio + crash reports | `log show` |
| O — sysdiagnose (opt-in) | Apple's official 200MB forensic dump | `sudo sysdiagnose` |

## Pre-requisites

```bash
# Optional (better device names): SwitchAudioSource via Homebrew
brew install switchaudio-osx

# REQUIRED for layer F (TCC.db read): grant Full Disk Access to Terminal
# 1. System Settings > Privacy & Security > Full Disk Access
# 2. Enable Terminal.app (or iTerm.app, Warp.app, etc.)
# 3. Restart the terminal
```

## Phase 1 — Smoke run (~2 min, no admin, no live capture)

```bash
cd ~/path/to/sovyx-voice-diag-mac
bash sovyx-voice-diag-mac.sh --skip-captures --non-interactive --yes
```

**Validate after run:**

1. `~/sovyx-diag-mac-*/SUMMARY.json` — check `tool_version`, `macos_version` populated
2. `A_hardware/system_profiler_audio.json` — non-empty (your audio devices listed)
3. `C_coreaudio/coreaudio_dump.json` — `device_count > 0`, `system_default_input` populated
4. `D_coreaudiod/hal_classifier.json` — `interceptors_detected` array (may be empty if clean install)
5. `F_session/tcc_mic_consents.json` — `fda_status: "granted"` (if NOT, grant FDA + retry layer F)
6. `G_sovyx/sovyx_log_tail.txt` — daemon log present (or note "(not present)" if Sovyx not installed)

If FDA is not granted, you'll see `fda_status: "denied"` and TCC consents will be empty. That's a known gap — grant FDA and re-run for full coverage.

## Phase 2 — Full run with live capture (~5 min)

```bash
cd ~/path/to/sovyx-voice-diag-mac
bash sovyx-voice-diag-mac.sh
# When prompted, speak naturally for 5s during the W capture step.
```

**Pre-req:** Sovyx Python venv must have `sounddevice` installed:
```bash
~/.local/share/pipx/venvs/sovyx/bin/python -m pip show sounddevice
```

## Phase 3 — sysdiagnose (opt-in, ~5 min, ~200 MB, requires admin)

For diagnosing transient/intermittent issues that need cross-system traces:

```bash
bash sovyx-voice-diag-mac.sh --with-sysdiagnose
# You'll be prompted for sudo password.
```

`sysdiagnose` is Apple's own forensic dump. It captures: spindump (process activity), tailspin, log archive, ioreg, pmset history, all DiagnosticReports, kext list, and much more. Heavy artifact; use only if Phase 1+2 didn't reveal the cause.

## What to send back

```bash
# The orchestrator already produces the tarball at ~/sovyx-diag-mac-<host>-<ts>-<uuid>.tar.gz
# Send that file plus its .sha256 sidecar.
ls -la ~/sovyx-diag-mac-*.tar.gz*
```

Send both `.tar.gz` and `.sha256`. Total size ~5-50 MB without sysdiagnose, ~200 MB with.

## Quick success criteria

✅ Phase 1 passes if `SUMMARY.json` exists + `A_hardware/system_profiler_audio.json` non-empty + at least 1 layer artifact per A,B,C,D,E,F,G,I,K
✅ Phase 2 additionally requires `W_capture/capture.wav` non-zero size + `silero.json` with `available: true`
✅ Phase 3 produces `O_sysdiagnose/sysdiagnose_*.tar.gz` >= 50 MB

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `swift: command not found` | Xcode Command Line Tools not installed | `xcode-select --install` |
| `tcc-reader: fda_status: denied` | Terminal lacks FDA | System Settings > Privacy > Full Disk Access |
| `E layer: SOVYX_PYTHON unresolved` | pipx venv at non-standard path | Set `SOVYX_PYTHON_OVERRIDE=/path/python` env (TODO: implement override flag) |
| `gtimeout: command not found` | GNU coreutils not installed | `brew install coreutils` (orchestrator falls back to perl alarm) |
| W capture has no signal | Mic muted in System Settings, OR app interceptor active (Krisp/Loopback rerouted default), OR Sovyx Python lacks mic permission | Check `D_coreaudiod/hal_classifier.json::interceptors_detected` and `F_session/tcc_mic_consents.json::interesting_chain_clients` |
