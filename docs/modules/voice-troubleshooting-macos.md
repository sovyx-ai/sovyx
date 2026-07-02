# Voice troubleshooting — macOS

Companion to
[voice-troubleshooting-windows.md](voice-troubleshooting-windows.md)
and [voice-troubleshooting-linux.md](voice-troubleshooting-linux.md).

Read this first: **macOS support is detection-only at HEAD.** Sovyx
ships probes that identify the common macOS capture failure modes, but
— unlike Windows and Linux — there are **zero automatic bypass/cure
strategies** for macOS (`voice/health/bypass/` contains Windows and
Linux strategies only). When a macOS capture problem is detected, the
remediation is always manual, using the steps below. Automatic cures
are blocked on dedicated Mac hardware for validation (mission W4.1).

## Symptom table

| Symptom | Most likely cause | First lever to try |
|---------|------------------|--------------------|
| Pipeline starts, every frame is all-zero, no error anywhere | TCC silent-deny — macOS never granted the mic to the process running Sovyx | Grant mic permission (see below). This is **the canonical macOS failure mode for new operators**. |
| AirPods / BT headset plays TTS fine but Sovyx never hears you | Headset is in A2DP profile (playback-only — the mic is OFF) | Switch the headset to its hands-free profile or use another input (see below) |
| Wake word almost never fires on a BT headset that *does* capture | HFP/SCO narrow-band audio (8 kHz CVSD / 16 kHz mSBC) degrades VAD + wake-word accuracy | Use a wired / USB mic for capture; keep BT for playback |
| Mic works in other apps but Sovyx capture is silent or distorted | A HAL plug-in (Krisp, BlackHole, Loopback, …) intercepts the capture chain | Inspect `/Library/Audio/Plug-Ins/HAL/`; remove/disable the plug-in or select a physical device |
| Audio devices vanish / capture wedges after sleep or device churn | `coreaudiod` distress | Restart Core Audio (see below) |
| New USB mic not picked up for up to ~30 s | macOS hot-plug detection is a `system_profiler` polling subprocess (30 s default interval) — the native Core Audio listener never shipped | Wait one poll cycle, or lower `SOVYX_TUNING__VOICE__VOICE_MACOS_HOTPLUG_SUBPROCESS_INTERVAL_S` |

## What Sovyx detects on macOS

All of these are **detection surfaces** — they classify and report;
none of them changes OS state:

* **Microphone permission (TCC)** — gated by
  `voice_check_mic_permission_enabled` (default **True on macOS**
  since v0.32.0). The voice factory probes the TCC database
  (`~/Library/Application Support/com.apple.TCC/TCC.db`) *before*
  creating the pipeline; on DENIED it raises `VoicePermissionError`
  carrying the literal System Settings path instead of booting a
  pipeline that captures silence. The verdict is scoped to the app
  actually hosting the process (bundle ID from the environment,
  terminal/IDE ancestry, or the interpreter path) — a grant for an
  unrelated app (Zoom, Chrome) never reports GRANTED, and when the
  microphone rows all belong to other apps the probe reports UNKNOWN
  with an explanatory note. Caveat: reading TCC.db requires
  Full Disk Access — without it the probe reports UNKNOWN (with a
  structured note) and the cascade's deaf-detection covers the rest.
* **Bluetooth A2DP-vs-HFP profile** (`voice/_bluetooth_profile_mac.py`)
  — spawns `system_profiler` (~2–5 s cold start; `-json` first, text
  fallback) to flag A2DP-only inputs; surfaced via the boot
  diagnostic log line `voice.macos.bluetooth_profile_detected` and
  the dashboard's platform-diagnostics Bluetooth card, which renders
  a warn pill + remediation hint for A2DP-only devices. Note: the
  `-json` output does not expose the active A2DP/HFP state, so on
  current macOS connected devices may report `unknown` (inconclusive)
  rather than a definitive profile.
* **HFP/SCO guard** (`voice/_hfp_guard.py`) — identifies when the
  active capture device is a narrow-band HFP-mode BT headset, whose
  signal quality collapses wake-word detection.
* **HAL plug-in detection** (`voice/_hal_detector_mac.py`) —
  enumerates `/Library/Audio/Plug-Ins/HAL/` for virtual-audio
  plug-ins that may intercept capture.
* **coreaudiod distress scan** (`voice/health/_driver_watchdog_macos.py`)
  — reads the unified system log (`log show --predicate`) scoped to
  `coreaudiod` / `AudioComponentRegistrar` and emits structured
  `voice_driver_watchdog_macos_*` records. Detection-tier only.
* **App Sandbox detection** (`voice/health/_macos_sandbox_detect.py`)
  — detects the App Sandbox (which can block capture even with TCC
  granted) so remediation hints are sandbox-aware.
* **Hot-plug polling** (`voice/health/_hotplug_mac.py`) —
  `system_profiler` polling subprocess, default every 30 s
  (`voice_macos_hotplug_subprocess_interval_s`). The native
  `AudioObjectAddPropertyListener` path was never shipped.

The probe cascade itself (cold/warm probes, ComboStore, quarantine)
runs on macOS with a macOS-specific combo order (`MACOS_CASCADE`), so
detection, failover between devices, and quarantine reporting all work
— only the *automatic cure* layer is absent.

## What `sovyx doctor` can and cannot do on macOS

| Command | macOS behaviour |
|---|---|
| `sovyx doctor voice` | **Works** — preflight diagnosis + quarantine / failover-history / degraded-banner / bundle-integrity / LLM-health surfaces. |
| `sovyx doctor platform` | **Works** — cross-OS platform-diagnostics report. Since MACOS-4 (2026-07-02) the macOS branch also surfaces the coreaudiod daemon state (MA10, with the manual `sudo killall coreaudiod` recovery hint), the App Sandbox verdict (MA13), and recent `com.apple.audio` unified-log events (MA14) — the same three probes `GET /api/voice/platform-diagnostics` exposes; previously they were boot-log-only. |
| `sovyx doctor stt_language_match` | **Works** — cross-platform; checks the mind's language against Moonshine's STT model set (WARN = speech transcribed in English). |
| `sovyx doctor voice --fix` | **Refuses** — exits `5` (`EXIT_DOCTOR_UNSUPPORTED`); the mixer auto-fix is Linux-only (`amixer`). |
| `sovyx doctor voice --calibrate` | **Refuses** — Linux-only. |
| `sovyx doctor voice --full-diag` | **Refuses** — exits `5` (`EXIT_DOCTOR_UNSUPPORTED`); the forensic producer exists for Linux (bash toolkit) and Windows (native probes) only. |

## Manual remediation

These are standard macOS operations (standard-OS guidance, not
Sovyx-specific mechanisms):

### Grant microphone permission (TCC)

1. Open **System Settings → Privacy & Security → Microphone**.
2. Enable the toggle for the app that runs Sovyx (your terminal
   emulator — e.g. Terminal, iTerm2 — or the Python launcher).
3. Restart the Sovyx daemon.

If Sovyx reports the permission probe as UNKNOWN, additionally grant
**Full Disk Access** to the same app so the TCC probe can read its
database — or simply verify the Microphone toggle manually.

### Bluetooth headset in A2DP (no mic)

Either select the headset as an **input** device in **System Settings
→ Sound → Input** (macOS then flips it to the hands-free profile —
audio quality drops on both directions), or — recommended — keep the
headset for playback only and use the built-in / a wired / USB mic as
`voice_input_device_name`.

### Restart Core Audio

```bash
sudo killall coreaudiod
```

`coreaudiod` restarts automatically; apps re-attach within seconds.
Use when devices vanish or capture wedges after sleep.

### HAL plug-in conflicts

List `/Library/Audio/Plug-Ins/HAL/` — if a virtual-audio plug-in
(Krisp, BlackHole, Loopback, …) sits in the capture path, disable or
remove it, or select a physical device explicitly, then restart
`coreaudiod`.

## Known limitations (honest status at HEAD)

* **No automatic cures.** Every bypass strategy in
  `voice/health/bypass/` targets Windows or Linux. On macOS a
  `voice.capture_integrity.bypass_ineffective` outcome means: follow
  the manual steps above. Shipping macOS strategies is hardware-blocked
  (mission W4.1 — no Mac hardware for validation).
* **Hot-plug latency is seconds-to-30 s**, not sub-second like the
  Windows/Linux listeners, because detection is `system_profiler`
  polling.
* **No forensic `--full-diag` producer**, no `--fix`, no
  `--calibrate` — troubleshooting depth on macOS is below Windows and
  Linux.
* **CI signal is advisory** — the hosted macOS CI leg is
  `continue-on-error` due to a nondeterministic native segfault on
  hardware-less runners; macOS voice paths have not been
  operator-validated on real hardware (see the pilot table in
  [voice-platform-parity.md](voice-platform-parity.md)).

## Related documents

* [voice-platform-parity.md](voice-platform-parity.md) — cross-platform
  parity matrix (what ships where).
* [voice-capture-health.md](voice-capture-health.md) — cascade,
  ComboStore, quarantine lifecycle.
* [voice-troubleshooting-windows.md](voice-troubleshooting-windows.md) /
  [voice-troubleshooting-linux.md](voice-troubleshooting-linux.md) —
  the platforms with automatic cure ladders.
