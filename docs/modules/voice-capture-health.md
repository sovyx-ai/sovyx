# Module: voice.health — Voice Capture Health Lifecycle (VCHL)

## What it does

`sovyx.voice.health` keeps the microphone capture path alive across
every realistic failure mode you'll meet in a long-running daemon:
driver hangs, USB unplug events, OS power transitions, audio service
crashes, capture APOs that destroy the signal upstream of user-space,
and Bluetooth headsets that auto-switch into the lossy SCO profile.
It is platform-aware (Windows / Linux / macOS), opt-in for telemetry,
and surfaces every interesting state through structured logs +
OpenTelemetry metrics.

The whole subpackage is organised as a 12-layer architecture (L0–L12)
defined in `ADR-voice-capture-health-lifecycle.md`. Each layer owns one
concern and exposes a small, dependency-injected surface.

## Layer map

| Layer | Module                                                | Concern                                                                                |
|-------|-------------------------------------------------------|----------------------------------------------------------------------------------------|
| L0    | `contract`                                            | Vocabulary — `Diagnosis`, `ProbeMode`, `Combo`, `ProbeResult`, `RemediationHint`, etc. |
| L1    | `combo_store`, `capture_overrides`                    | Persistent JSON memo of (endpoint × winning combo) + user-pinned combos.               |
| L2    | `cascade`                                             | Platform cascade tables + lifecycle lock + wall-clock budget.                          |
| L2.5  | `_mixer_sanity`, `_mixer_roles`, `_mixer_kb`, `_hardware_detector`, `_linux_mixer_apply` | **Linux-only.** Bidirectional ALSA mixer healing — KB-matched preset apply with validation + rollback + alsactl persist. |
| L3    | `probe`                                               | Single probe entry point with cold / warm modes.                                       |
| L4    | `watchdog` + `_hotplug*`, `_power*`, `_audio_service*`| Runtime resilience — backoff re-probes, hot-plug listeners, power events, service crashes. |
| L5    | `preflight`                                           | Pre-stream validation steps (`check_portaudio`, `check_wake_word_smoke`, ...).         |
| L6    | `wizard`                                              | Setup-wizard orchestrator that walks a user through a healthy first-boot.              |
| L7    | `dashboard/routes/voice.py`                           | REST surface for the React Voice Health panel.                                         |
| L8    | `cli/commands/doctor.py`                              | `sovyx doctor voice` — preflight diagnosis from the terminal.                          |
| L9    | `_telemetry`                                          | Anonymous, opt-in cascade-outcome rollup written to `data_dir`.                        |
| L10   | Per-platform cascade tables in `voice/health/cascade/_planner.py` | `WINDOWS_CASCADE` / `LINUX_CASCADE` / `MACOS_CASCADE` (re-exported via `voice/health/cascade/__init__.py`). |
| L11   | This file + `docs/modules/voice.md`                   | Documentation.                                                                         |
| L12   | `tests/unit/voice/health/test_cascade_chaos.py`       | Failure injection — proves the cascade is resilient.                                   |

## Key concepts

### `Combo`

Immutable description of one "way of opening the mic": host API,
sample rate, channels, sample format, exclusive flag, auto-convert
flag, frames per buffer, and platform key. Cascades are tuples of
combos tried in priority order until a probe returns
`Diagnosis.HEALTHY`.

### Cascade priority

```text
1. CaptureOverrides (user-pinned)        → source = "pinned"
2. ComboStore fast path (last good)      → source = "store"
2.5. L2.5 mixer sanity (Linux, opt-in)   → heals ALSA mixer in-place
3. Platform cascade table walk           → source = "cascade"
```

The lifecycle lock (`LRULockDict`, capacity 64) ensures only one
cascade per endpoint runs at a time — eliminating hot-plug races and
doctor-vs-daemon conflicts.

L2.5 is opt-in via `run_cascade(mixer_sanity=MixerSanitySetup(...))`
and runs only when `platform_key == "linux"`. It heals the ALSA
mixer state (not the PortAudio combo) so the subsequent platform
walk has a chance against a known-good baseline. See the
[Mixer sanity (L2.5)](#mixer-sanity-l25) section.

### Diagnosis ladder

```text
HEALTHY               → frame stream alive + RMS > -55 dB + VAD probability > 0.5
LOW_SIGNAL            → frames alive but RMS in [-70, -55] dB
NO_SIGNAL             → frames alive but RMS < -70 dB (or no callbacks at all)
VAD_INSENSITIVE       → healthy RMS but VAD probability in (0.05, 0.5]
APO_DEGRADED          → healthy RMS but VAD probability ≤ 0.05 (Voice Clarity etc.)
DRIVER_ERROR          → PortAudio refused the combo
DEVICE_BUSY           → exclusive contention with another process
PERMISSION_DENIED     → OS blocked microphone access
KERNEL_INVALIDATED    → kernel-side IAudioClient stuck (USB resource timeout, driver hot-swap, mid-stream PnP churn). No user-mode cure — replug or reboot. §4.4.7 quarantines the endpoint.
MIXER_ZEROED          → L2.5: attenuation regime (Capture attenuated + boost at zero)
MIXER_SATURATED       → L2.5: saturation regime (boost chain clipping internally)
MIXER_UNKNOWN_PATTERN → L2.5: out-of-range mixer state with no KB match
MIXER_CUSTOMIZED      → L2.5: intentional user tuning detected; no action taken
MUTED                 → microphone muted at OS / hardware level
```

The diagnosis drives `RemediationHint` text shown in `sovyx doctor voice`
and the dashboard panel.

## Cross-platform cascade tables

### Windows (`WINDOWS_CASCADE`, 6 entries — default)

1. WASAPI exclusive 16 kHz mono int16 (480-frame)
2. WASAPI exclusive 48 kHz mono int16 (480-frame)
3. WASAPI exclusive 48 kHz mono int16 (960-frame)
4. WASAPI shared 16 kHz mono int16 (`auto_convert=True`)
5. DirectSound 16 kHz mono int16
6. MME 16 kHz mono int16 (last-resort)

Exclusive mode bypasses the entire APO chain (Windows Voice Clarity,
device-bound effects, system-wide enhancements). The cascade is biased
toward exclusive because we've measured Voice Clarity dropping VAD
probability below 0.01 on otherwise-healthy hardware (early 2026
`VocaEffectPack` rollout via Windows Update).

#### WDM-KS removal (post-mortem 2026-04-20)

WDM-KS (Windows Driver Model Kernel Streaming) was in the default
cascade through v0.20.3 but was **removed in v0.20.4** after two
reproducible hard-reset incidents on Razer BlackShark V2 Pro
(VID_1532 / PID_0528, generic `usbaudio` driver). The kernel-streaming
IOCTL issued against a driver whose upstream `IAudioClient::Initialize`
had just failed with `AUDCLNT_E_DEVICE_INVALIDATED` wedged the
driver's event-queue thread; Windows fired a kernel resource watchdog
(`LiveKernelEvent 0x1CC`) and hard-reset (`Kernel-Power 41`,
`BugcheckCode=0`, no dump). Because WDM-KS adds **no APO-bypass
capability** beyond what WASAPI exclusive (attempts 0-2) already
covers, the risk/benefit was catastrophic. The opt-in 8-entry table
`WINDOWS_CASCADE_AGGRESSIVE` keeps WDM-KS available for operators on
verified-safe hardware — pass it as `cascade_override` to `run_cascade`.

### Linux (`LINUX_CASCADE`, 6 entries)

1. ALSA `hw:` direct 16 kHz mono int16 (bypasses PulseAudio / PipeWire)
2. ALSA `hw:` direct 48 kHz mono int16
3. JACK 48 kHz mono float32
4. PipeWire 16 kHz mono int16 with `auto_convert=True`
5. PipeWire 48 kHz mono int16
6. PulseAudio shared 16 kHz mono int16

The first two combos sidestep the session-manager APO surface entirely,
which is important when `module-echo-cancel` (PulseAudio) or a
`filter-chain` `echo-cancel` node (PipeWire) is loaded — both destroy
the raw mic signal upstream of PortAudio.

### macOS (`MACOS_CASCADE`, 4 entries)

1. CoreAudio 48 kHz mono int16
2. CoreAudio 48 kHz mono float32
3. CoreAudio 44.1 kHz mono int16 with `auto_convert=True`
4. CoreAudio 16 kHz mono int16 (narrow-band fallback)

CoreAudio has a much smaller APO surface than Windows / Linux, so the
cascade is correspondingly slim. The dominant macOS-specific failure
mode is the Bluetooth HFP/SCO switch — see the HFP guard below.

## Mixer sanity (L2.5)

A Linux-only healing layer spec'd in
[`ADR-voice-mixer-sanity-l2.5-bidirectional.md`](../../docs-internal/ADR-voice-mixer-sanity-l2.5-bidirectional.md).
Fixes the **bidirectional** ALSA mixer-failure class that makes voice
capture unusable on some laptops out-of-the-box:

| Regime | Example | Symptom | Cure |
|---|---|---|---|
| **Saturation** | HDA Realtek + Internal Mic Boost at max | RMS healthy but Silero VAD max_prob < 0.05 (APO-like internal clipping) | L2.5 resets via KB preset or generic fractions |
| **Attenuation** | VAIO VJFE69 + Conexant SN6180 (Capture at -34 dB + boost at 0) | Voice arrives at -60 dBFS — Silero trained on -25 to -15 dBFS, produces max_prob < 0.01 | L2.5 applies KB preset bringing Capture to 1.0 fraction |

### State machine (7 steps)

```text
probe → classify → detect_customization → apply → validate → (persist | rollback) → done
  ↓       ↓              ↓                    ↓          ↓             ↓
amixer   KB match    7-signal user     apply_mixer_   Silero +     alsactl store
 scan    + regime    heuristic (A..G)  preset +       SNR + WW     (best-effort)
                     respects tuning   LIFO rollback  gates
```

Hard wall-clock budget: 5 s (tunable via
`SOVYX_TUNING__VOICE__LINUX_MIXER_SANITY_BUDGET_S`).

### 7-signal user-customization heuristic

L2.5 respects intentional user tuning via a weighted score (sums
to 1.0):

| Signal | Weight | Description |
|---|---|---|
| A | 0.30 | Current mixer deviates from matched KB `factory_signature` (continuous: scales with 1 - factory_score). |
| B | 0.15 | `~/.asoundrc` exists. |
| C | 0.15 | `~/.config/pipewire/pipewire.conf.d/*.conf` present. |
| D | 0.15 | `/var/lib/alsa/asound.state` mtime within last 7 days. |
| E | 0.10 | `~/.config/wireplumber/wireplumber.conf.d/*.conf` present. |
| F | 0.10 | `ComboStore` has entry for endpoint AND factory_signature_score < 0.5. |
| G | 0.05 | `CaptureOverrides` has pinned combo for endpoint. |

Three branches (tunable via
`linux_mixer_user_customization_threshold_{apply,skip}`):

* `score < 0.5` → auto-apply KB preset
* `0.5 ≤ score ≤ 0.75` → defer (dashboard offers choice card)
* `score > 0.75` → skip silently; user tuning is sacred

### KB profile matching

Profiles live under
`src/sovyx/voice/health/_mixer_kb/profiles/*.yaml`
(F1 ships empty; F1.H populates). Each profile declares:

* Match criteria — `codec_id_glob` (required), driver_family,
  system_vendor_glob, system_product_glob, audio_stack,
  kernel_major_minor_glob.
* `factory_signature` — per-role expected raw/fraction/dB ranges
  describing what the factory-bad state looks like on this hardware.
* `recommended_preset` — what to apply on match
  (raw / fraction / dB values per role).
* `validation_gates` — RMS + peak + SNR + Silero + OpenWakeWord
  thresholds every post-apply probe must satisfy.
* `verified_on` — HIL attestations (required for merge).

Scoring: `codec_id` is a hard gate (mismatch → score 0). Remaining
fields contribute weighted to a 0..1 composite. The highest-scoring
profile above `match_threshold` (default 0.6) wins; a 2nd-place
score within 0.05 → `DEFERRED_AMBIGUOUS` (dashboard card).

### Role-based control discovery

Control names vary across codec families (HDA: `"Capture"`,
`"Internal Mic Boost"`; SOF: `"PGA1.0 1 Master Capture Volume"`;
USB-audio: `"Mic"`). L2.5 maps raw names to a canonical
`MixerControlRole` via 3-layer lookup:

1. Per-codec override (e.g., Conexant SN6180 quirks).
2. Driver-family exact match (F1: HDA only).
3. Substring fallback (superset of
   `_linux_mixer_probe._BOOST_CONTROL_PATTERNS`).

SOF / USB-audio / BT role tables land in F2.

### HardwareContext detection

`_hardware_detector.detect_hardware_context()` populates the
match inputs read-only + without subprocess:

| Field | Source |
|---|---|
| codec_id | `/proc/asound/card*/codec#*` → `Vendor Id: 0x…` → `VVVV:DDDD` |
| driver_family | `/proc/asound` shape (codec files → HDA; card id containing USB → usb-audio) |
| system_vendor | `/sys/class/dmi/id/sys_vendor` |
| system_product | `/sys/class/dmi/id/product_name` |
| distro | `/etc/os-release` → `"<id>-<version_id>"` |
| audio_stack | `$XDG_RUNTIME_DIR/pipewire-0` vs `pulse/native` vs `/proc/asound/pcm` |
| kernel | `platform.uname().release` |

Every field is optional — partial detection is fine; KB scoring
handles None correctly. No `dmidecode` (needs root, violates
invariant I7). No `lspci` subprocess.

### runtime_pm (systemd + udev)

The mixer state fix is orthogonal to runtime_pm — both are
needed on affected hardware. L2.5 handles runtime_pm via a
systemd oneshot + udev rule shipped under `packaging/`:

* `systemd/sovyx-audio-runtime-pm.service` — oneshot at boot
  writing `on` to every audio-class PCI device's
  `power/control`. Tight sandboxing: `NoNewPrivileges` + empty
  `CapabilityBoundingSet` + `ReadWritePaths` narrowed to
  `/sys/bus/pci/devices`.
* `udev/60-sovyx-audio-power.rules` — re-asserts `on` on
  hotplug (dock / USB-C) and codec driver rebind.

The daemon itself NEVER writes to `/sys` at runtime (invariant
I7). Operator escape hatch: kernel command line
`sovyx.audio.no_pm_override` disables the systemd unit globally.

See [`packaging/README.md`](../../packaging/README.md) for
distro-packaging install paths.

### Opt-in from the cascade caller

L2.5 does NOT fire by default — callers opt in by constructing a
`MixerSanitySetup` and passing it to `run_cascade(mixer_sanity=…)`:

```python
from sovyx.voice.health import (
    MixerControlRoleResolver, MixerKBLookup, MixerSanitySetup,
    detect_hardware_context, run_cascade,
)

resolver = MixerControlRoleResolver()
kb = MixerKBLookup.load_shipped(resolver=resolver)
hw = await detect_hardware_context()

setup = MixerSanitySetup(
    hw=hw,
    kb_lookup=kb,
    role_resolver=resolver,
    validation_probe_fn=my_probe_fn,  # warm-probe + SNR + WW stage-2
)
result = await run_cascade(
    endpoint_guid="...", device_index=0, mode=ProbeMode.COLD,
    platform_key="linux", mixer_sanity=setup, ...
)
```

`mixer_sanity=None` (default) → zero behaviour change for every
pre-L2.5 caller.

## Capture-APO detection

Per-platform detectors live one level up under `sovyx.voice`:

* **Windows** — `_apo_detector.py` reads MMDevices registry under
  `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture`
  for known APO CLSIDs (Voice Clarity / `voiceclarityep`, OEM enhancements).
* **Linux** — `_apo_detector_linux.py` shells out to `pactl list short
  modules` (PulseAudio / `pipewire-pulse`) and `pw-dump` (PipeWire native)
  for `module-echo-cancel`, `filter-chain` `echo-cancel`, RNNoise.
* **macOS** — `_hfp_guard.py` runs `system_profiler SPBluetoothDataType
  -json` and classifies connected Bluetooth devices via three signals
  (service records → minor type → name hint fallback) so the orchestrator
  can warn the user before the SCO switch destroys VAD.

All three detectors run with subprocess timeouts (≤ 2.5 s), `errors=
"replace"`, and short-circuit cleanly on missing tools / non-zero exits.

## L9 — Anonymous opt-in telemetry

`_telemetry.py` accumulates a tiny rollup of cascade outcomes:

```json
{
  "schema_version": 1,
  "last_updated": "2026-04-19T12:34:56+00:00",
  "buckets": [
    { "platform": "win32",  "host_api": "WASAPI",   "success": 41, "failure": 3, "total": 44, "success_rate": 0.9318 },
    { "platform": "linux",  "host_api": "ALSA",     "success": 17, "failure": 0, "total": 17, "success_rate": 1.0 },
    { "platform": "darwin", "host_api": "CoreAudio","success":  9, "failure": 1, "total": 10, "success_rate": 0.9 }
  ]
}
```

Recording is gated on `EngineConfig.telemetry.enabled` (default
`False`). The file is written atomically to
`data_dir/voice_health_telemetry.json`; nothing is sent off-machine.
The user owns the file and can ship it to Sovyx upstream manually.

What the rollup deliberately does **not** include:

* device names / friendly names / Bluetooth addresses
* USB VID/PID
* audio fingerprints (those are designed to identify a specific rig)
* user IDs, mind IDs, machine IDs, hostnames
* timestamps beyond a single `last_updated` ISO-8601 string

## L12 — Chaos tests

`tests/unit/voice/health/test_cascade_chaos.py` injects controlled
failures into the cascade and asserts the orchestrator behaves
correctly:

* probe timeouts on every attempt → `budget_exhausted=True`, no winner
* intermittent `DRIVER_ERROR` → cascade keeps walking, eventually wins
* hot-plug storm during cascade → lifecycle lock serialises, no double-open
* every diagnosis → `LOW_SIGNAL` → cascade reports no-winner fallback

These run in CI as part of the unit suite (no real audio, no
`tests/stress/` exclusion).

## Surfaces

### CLI

```bash
sovyx doctor voice           # one-shot preflight diagnosis
sovyx doctor voice --json    # machine-parseable for automation
```

### REST

```text
GET    /api/voice/service-health          # T6.20 — aggregated readiness probe (4-field, never 5xx)
GET    /api/voice/health                  # ComboStore + overrides + quarantine snapshot
GET    /api/voice/health/quarantine       # kernel-invalidated quarantine snapshot
GET    /api/voice/health/snapshot         # current cascade + watchdog state
GET    /api/voice/health/preflight        # rerun preflight on demand
POST   /api/voice/health/cascade/reset    # clear ComboStore for an endpoint
GET    /api/voice/capture-diagnostics     # APO / HFP guard report
```

### React panel

`dashboard/src/pages/voice-health.tsx` renders the snapshot live via
the regular dashboard auth + polling stack — no WebSocket because the
data is low-cadence and bounded.

## Phase 6 observability emissions

Structured-log events emitted at terminal failure states. Every
event carries the diagnostic context operators need to route alerts
without scraping per-attempt logs. Stable wire-form contract —
renames are breaking changes for monitoring tooling.

| Event | Level | Source | Trigger |
|-------|-------|--------|---------|
| `voice_cascade_exhausted` | ERROR | cascade exhaustion | Every combo failed; carries `diagnosis_histogram` (T6.11). |
| `voice_cascade_budget_exhausted` | WARNING | cascade timeout | Total budget expired mid-walk; carries `diagnosis_histogram` (T6.11). |
| `voice_cascade_user_actionable` | ERROR | cascade exhaustion | Homogeneous histogram + diagnosis is in remediation map. Carries `diagnosis` + user-facing `remediation` text (T6.12). Distinct from `voice_cascade_exhausted` so dashboards subscribe to actionable failures only. |
| `voice_capture_permanently_degraded` | ERROR | watchdog backoff exhaustion | Backoff schedule exhausted without HEALTHY. Carries `last_diagnosis` (T6.14). |
| `voice_capture_integrity_unrecoverable` | ERROR | bypass coordinator | All bypass strategies returned `NOT_APPLICABLE`. Carries `platform` + platform-specific `remediation` (T6.15). |
| `voice_quarantine_re_quarantine_event` | WARNING | quarantine add | Endpoint re-quarantined ≥ `quarantine_pingpong_threshold` times within `quarantine_pingpong_window_s` (T6.17). |
| `voice_endpoint_repeatedly_failing` | WARNING | quarantine add | Endpoint re-added within `quarantine_rapid_requarantine_window_s` of TTL expiry (T6.18). |
| `capture_integrity_inconclusive_retry` | INFO | bypass coordinator | Post-apply INCONCLUSIVE → retry attempted. Carries `retry_recovered` boolean (T6.16). |

The `diagnosis_histogram` field on cascade exhaustion logs is a
JSON object `{diagnosis_value: count}` — operators can grep one
line and see the failure-mode distribution.

## OpenTelemetry metrics

| Metric                                              | Type             | Labels                          |
|-----------------------------------------------------|------------------|---------------------------------|
| `sovyx.voice.health.cascade.attempts`               | counter          | platform, host_api, success, source |
| `sovyx.voice.health.combo_store.hits`               | counter          | endpoint_class, result          |
| `sovyx.voice.health.combo_store.invalidations`     | counter          | reason                          |
| `sovyx.voice.health.probe.diagnosis`                | counter          | diagnosis, mode                 |
| `sovyx.voice.health.probe.duration`                 | histogram (ms)   | mode                            |
| `sovyx.voice.health.preflight.failures`             | counter          | step, code                      |
| `sovyx.voice.health.recovery.attempts`              | counter          | trigger                         |
| `sovyx.voice.health.self_feedback.blocks`           | counter          | layer                           |
| `sovyx.voice.health.active_endpoint.changes`        | counter          | reason                          |
| `sovyx.voice.health.time_to_first_utterance`        | histogram (ms)   | —                               |

These names are the public contract — Grafana boards / Loki queries
depend on them. Renames are breaking changes.

## Tuning knobs

Every threshold lives under `EngineConfig.tuning.voice` (see
`src/sovyx/engine/config.py:VoiceTuningConfig`). Override via env:

```bash
SOVYX_TUNING__VOICE__CASCADE_TOTAL_BUDGET_S=45.0
SOVYX_TUNING__VOICE__VOICE_CLARITY_AUTOFIX=false
SOVYX_TUNING__VOICE__SELF_FEEDBACK_ISOLATION_MODE=gate-only
SOVYX_TUNING__VOICE__WATCHDOG_BACKOFF_SCHEDULE_S='[5.0,15.0,45.0]'
```

## See also

* `docs/modules/voice.md` — pipeline / VAD / STT / TTS overview.
* `ADR-voice-capture-health-lifecycle.md` — full design rationale (internal).
* `CLAUDE.md` anti-pattern #21 — Voice Clarity APO + WASAPI exclusive bypass.
