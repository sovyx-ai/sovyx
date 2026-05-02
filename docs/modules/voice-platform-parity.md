# Voice subsystem — cross-platform parity matrix

> Phase 7 / T7.43. Operator-facing reference for which voice
> features run on which OS, what's platform-specific hardening
> vs. universal core, and where the gaps are.

This is a **map, not a guarantee**. Hardware variation (BT codecs,
USB audio interfaces, virtual audio drivers) introduces edge cases
that no matrix can fully enumerate. The "✅ shipped" cells mean the
code path runs on that platform; production validation under real
hardware is operator-side per
[`OPERATOR-VALIDATION-BACKLOG-2026.md`](../../docs-internal/OPERATOR-VALIDATION-BACKLOG-2026.md).

---

## Universal core (every platform)

Every feature in this section runs on **all three platforms**:
Windows 10/11, Linux (any distro with PortAudio + ALSA/PipeWire
working), macOS 13+. ONNX Runtime + PortAudio + Python 3.11/3.12 are
the cross-platform foundation; the universal-core features are
those that don't reach below that layer.

| Feature | Win | Linux | macOS | Implementation |
|---|---|---|---|---|
| **Wake word** — OpenWakeWord 2-stage detector | ✅ | ✅ | ✅ | `voice/wake_word.py` (ONNX Runtime) |
| **Wake-word per-mind routing (Phase 8)** | ✅ | ✅ | ✅ | `voice/_wake_word_router.py` |
| **STT-fallback wake word (T8.17)** | ✅ | ✅ | ✅ | `voice/_wake_word_stt_fallback.py` |
| **Phonetic similarity matching (T8.12)** | ⚠️ | ✅ | ✅ | espeak-ng optional dep; Win install via [GitHub releases](https://github.com/espeak-ng/espeak-ng/releases) |
| **Multi-language wake variants (T7.11-T7.16)** | ✅ | ✅ | ✅ | `compose_wake_variants_for_locale` — pure Python |
| **Diacritic + accent variants (T8.16)** | ✅ | ✅ | ✅ | `_wake_word_variants.expand_wake_word_variants` |
| **VAD** — Silero v5 | ✅ | ✅ | ✅ | `voice/vad_silero.py` (ONNX Runtime) |
| **STT (local)** — Moonshine v2 | ✅ | ✅ | ✅ | `voice/stt_moonshine.py` via `moonshine-voice` |
| **STT (cloud)** — OpenAI Whisper API | ✅ | ✅ | ✅ | `voice/stt_cloud.py` (BYOK) |
| **TTS — Piper** | ✅ | ✅ | ✅ | `voice/tts_piper.py` |
| **TTS — Kokoro** | ✅ | ✅ | ✅ | `voice/tts_kokoro.py` (`kokoro-onnx`) |
| **Wyoming server** | ✅ | ✅ | ✅ | `voice/_wyoming_server.py` (TCP) |
| **VoicePipeline state machine** | ✅ | ✅ | ✅ | `voice/pipeline/_orchestrator.py` |
| **Barge-in detection** | ✅ | ✅ | ✅ | `voice/pipeline/_barge_in.py` |
| **AEC (Acoustic Echo Cancellation)** | ✅ | ✅ | ✅ | `voice/_aec_pyaec.py` (`pyaec` extras) |
| **Audio capture** — PortAudio | ✅ | ✅ | ✅ | `sounddevice` library |
| **Audio output** — PortAudio | ✅ | ✅ | ✅ | `sounddevice` library |
| **LUFS normalisation** | ✅ | ✅ | ✅ | `voice/audio.py` |
| **Auto-selector** (hardware probe → model combo) | ✅ | ✅ | ✅ | `voice/auto_select.py` |
| **Capture diagnostics** (`/api/voice/capture-diagnostics`) | ✅ | ✅ | ✅ | `voice/_apo_detector.py` (Win-active; no-op on others) |
| **Combo store** (cascade winner persistence) | ✅ | ✅ | ✅ | `voice/health/combo_store/` |
| **Bypass tier system** | ✅ | ✅ | ✅ | `voice/health/bypass/` (per-platform bypass impls) |
| **Consent ledger** (GDPR Art. 15/17/30) | ✅ | ✅ | ✅ | `voice/_consent_ledger.py` |
| **Per-mind retention (T8.21 step 6)** | ✅ | ✅ | ✅ | `mind/retention.py` |

⚠️ = available but requires operator-side install of an optional
external binary (espeak-ng for phonetic match; gracefully degrades
to STT-only fallback when absent).

---

## Windows-specific hardening

Windows audio path has the densest set of platform-specific
hardening because of:

* The Voice Clarity APO bug shipped in early-2026 Windows Updates
  (anti-pattern #21).
* WASAPI / DirectSound / MME / WDM-KS host-API multiplicity.
* Driver-update interruptions (the audio service occasionally
  detaches an endpoint on driver upgrades).

| Feature | Module | Notes |
|---|---|---|
| **WASAPI exclusive mode** | `voice/health/bypass/_win_wasapi_exclusive.py` | Bypasses Voice Clarity APO + every other capture-side processor. Tier-3 fallback. |
| **Host-API rotate-then-exclusive** | `voice/health/bypass/_win_host_api_rotate_then_exclusive.py` | T27 (deferred ADR) — rotates DirectSound→MME→WDM-KS before falling to exclusive. |
| **RAW Communications mode** | `voice/health/bypass/_win_raw_communications.py` | Forces communication-mode endpoint for headsets that expose separate "communication" device. |
| **MM notification listener** | `voice/_mm_notification_client.py` | COM `IMMNotificationClient` for sub-second default-device change detection. |
| **Audio service watchdog** | `voice/health/_audio_service_win.py` | Polls Audiosrv state via `sc.exe` query; restarts capture on service detach. |
| **Driver update listener** | `voice/health/_driver_update_listener_win.py` | WMI subscription to driver-update events. |
| **Voice Clarity APO detection** | `voice/_apo_detector.py` | Reads HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\AudioFx for VocaEffectPack registration. |

All Windows-specific modules have a `if sys.platform != "win32":
return ...` guard at the entry point so importing them on Linux /
macOS is a no-op. The voice factory composes them conditionally;
operators on non-Windows never pay the runtime cost.

---

## Linux-specific hardening

Linux audio is bimodal: PulseAudio (legacy) and PipeWire (modern,
Fedora 35+, Ubuntu 22.10+). ALSA underpins both. Hardening targets:

* PipeWire native API for module-echo-cancel detection.
* ALSA UCM (Use-Case Manager) for endpoint identification.
* Session-manager-aware restart paths (PipeWire's
  `pipewire-pulse` may serialise capture re-open on session events).

| Feature | Module | Notes |
|---|---|---|
| **PipeWire detection** | `voice/health/_pipewire.py` | Read-only — reports presence of `module-echo-cancel`; never auto-loads. |
| **ALSA UCM detection** | `voice/health/_alsa_ucm.py` | Read-only — reports active verb / device for the ALSA card. |
| **ALSA hw: direct restart** | `voice/capture/_restart_mixin.py:request_alsa_hw_direct_restart` | Bypasses PulseAudio/PipeWire by opening `hw:` device directly. |
| **Session-manager restart escape** | `voice/health/bypass/_linux_session_manager_escape.py` | Detects PulseAudio/PipeWire ownership; coordinates re-open. |
| **PipeWire-direct bypass** | `voice/health/bypass/_linux_pipewire_direct.py` | Native PipeWire client (skips PulseAudio shim layer). |
| **ALSA mixer bypass** | `voice/health/bypass/_linux_alsa_mixer.py` | Card-mixer state alignment for capture path. |
| **Linux audio service health** | `voice/health/_audio_service_linux.py` | systemd unit-state polling for `pulseaudio` / `pipewire` / `wireplumber`. |

---

## macOS-specific hardening

macOS is the simplest audio platform of the three (Core Audio is a
single mature API). Hardening targets:

* TCC microphone permission (denied permission is a silent failure
  on naive PortAudio open).
* HAL plug-ins that intercept the capture chain (Krisp, BlackHole,
  Loopback, etc.).
* Bluetooth A2DP profile detection (input-side BT mics are
  notoriously bad — A2DP-only headsets shouldn't be selected).

| Feature | Module | Notes |
|---|---|---|
| **Microphone permission** | `voice/health/_mic_permission_mac.py` | Reads TCC database; raises `VoicePermissionError` with the literal Settings path on DENIED. |
| **HAL plug-in detection** | `voice/_hal_detector_mac.py` | Enumerates `/Library/Audio/Plug-Ins/HAL/` for virtual-audio plugins. |
| **Codesign entitlement verify** | `voice/_codesign_verify_mac.py` | Confirms hardened-runtime mic entitlement on the running binary. |
| **Bluetooth A2DP profile detect** | `voice/_bluetooth_profile_mac.py` | Spawns `system_profiler` to flag A2DP-only inputs (~2-5 s cold-start). |
| **macOS sandbox detection** | `voice/health/_macos_sandbox_detect.py` | Identifies App Store sandbox (different audio access model). |
| **macOS sysdiagnose collection** | `voice/health/_macos_sysdiagnose.py` | On-demand: invokes `sysdiagnose` for support bundles. |
| **macOS hotplug listener** | `voice/health/_hotplug_mac.py` | Core Audio `kAudioHardwarePropertyDevices` listener. |
| **macOS driver watchdog** | `voice/health/_driver_watchdog_macos.py` | Watches Core Audio driver state changes. |
| **macOS audio fingerprint** | `voice/health/_fingerprint_macos.py` | Stable endpoint fingerprint across reboots. |

---

## Operator validation status per platform

This subsection is the **truth-as-of-2026-05-01**. Hardware-blocked
items are tracked in
[`OPERATOR-DEBT-MASTER-2026-05-01.md`](../../docs-internal/OPERATOR-DEBT-MASTER-2026-05-01.md).

| Platform | Daily-driver pilot | Soak status | Notes |
|---|---|---|---|
| **Windows 11 (Razer + RTX)** | ✅ Active | Multi-month | Voice Clarity APO regression caught + Tier-3 WASAPI exclusive bypass operating. Primary-developer hardware. |
| **Windows 10** | Pending | — | No operator with Win10 daily-driver yet. Code paths are 10-and-up; smoke-tested via mock. |
| **Linux Mint 21/22** | Pending (D11) | — | Need operator pilot. PipeWire path most likely to surface issues. |
| **Linux Alpine / minimal** | Pending (D11) | — | systemd-vs-OpenRC service-watchdog paths diverge here. |
| **Linux NixOS** | Out of scope | — | Atypical layout; no operator demand. |
| **macOS 13 (Ventura)** | Pending (D11) | — | Need operator pilot. TCC + HAL plugin paths haven't been hardware-validated. |
| **macOS 14 (Sonoma)** | Pending (D11) | — | Same. |
| **macOS 15 (Sequoia)** | Pending (D11) | — | Same. |

---

## Cross-cutting features

These work on all platforms but have platform-specific behaviour
worth knowing:

### Hot-plug detection

| Platform | Mechanism | Latency |
|---|---|---|
| Windows | COM `IMMNotificationClient` | < 100 ms |
| Linux | `udev` events via `pyudev` (when available) | < 200 ms |
| macOS | Core Audio property listener | < 100 ms |

### Default device change

All platforms detect default-device changes. Windows uses the
`IMMNotificationClient` listener (T6.49 / T6.50); Linux and macOS
use platform-native APIs. The orchestrator's restart epoch
increments uniformly across platforms.

### Capture epoch counter (Phase 6)

Cross-platform invariant: every capture restart increments the
ring-buffer epoch counter so consumers can detect stale frames.
Implementation lives in `voice/capture/_epoch.py` — pure-Python,
no platform code.

---

## What's NOT in the matrix

Some features are intentionally absent because they don't have
enough operator demand to justify the platform-specific hardening:

* **JACK low-latency** (Linux pro-audio) — operator-demand-blocked
  per OPERATOR-DEBT-MASTER D13. PortAudio path works against JACK
  via JACK's PortAudio binding but pro-audio operators may want
  native JACK clients. Deferred.
* **CoreAudio Aggregate Devices** (macOS) — operators can create
  aggregate devices via Audio MIDI Setup; Sovyx sees them as a
  single PortAudio device + works. No special integration needed.
* **WSL2 audio passthrough** (Windows) — works on WSLg via
  PulseAudio shim but capture latency suffers. Use native Windows
  install for voice-pipeline workloads.
* **Docker/container audio** — works against the host's PulseAudio
  socket via `--device /dev/snd` + bind-mount; no Sovyx-side
  hardening. Operators document their setup.

---

## Cross-references

* **Public privacy story:** [`docs/modules/voice-privacy.md`](voice-privacy.md)
* **Public audio quality:** [`docs/audio-quality.md`](../audio-quality.md)
* **Public troubleshooting (Windows):** [`docs/modules/voice-troubleshooting-windows.md`](voice-troubleshooting-windows.md)
* **Per-platform device test:** [`docs/modules/voice-device-test.md`](voice-device-test.md)
* **Operator hardware pilots:** `docs-internal/OPERATOR-DEBT-MASTER-2026-05-01.md` D11–D13 (gitignored).
* **OpenTelemetry semconv:** [`docs/modules/voice-otel-semconv.md`](voice-otel-semconv.md)
