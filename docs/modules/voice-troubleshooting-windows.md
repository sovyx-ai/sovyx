# Voice troubleshooting — Windows

This guide covers Windows-specific voice capture failures and the
operator levers Sovyx provides to diagnose and recover. The most
common failure mode on Windows 11 25H2+ is **Microsoft Voice Clarity
APO** silently destroying the capture signal upstream of PortAudio;
the symptoms below help you identify it and the recovery levers
explain how to disable / bypass / work around it.

## Symptom table

| Symptom | Most likely cause | First lever to try |
|---------|------------------|--------------------|
| `voice` enables fine, mic hardware light is on, but Sovyx never wakes on the wake word | Microsoft Voice Clarity APO destroying signal upstream of PortAudio | Run `sovyx doctor voice_capture_apo` (wired in v0.32.6); inspect `Detected APOs` row |
| Pipeline reports "deaf signal" then silently quarantines the endpoint | Cascade ↔ runtime drift (Furo W-4) — runtime opens MME while cascade picked DirectSound | Set `SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED=true` (v0.25.0+) |
| Same combo (DirectSound, 16 kHz, mono) keeps winning the cascade and producing silence | Cold probe accepting silent combo (Furo W-1) | Set `SOVYX_TUNING__VOICE__PROBE_COLD_STRICT_VALIDATION_ENABLED=true` |
| Microphone works in Discord/Zoom but not in Sovyx | Voice Clarity is in EFX (post-mix), not MFX (pre-mix) — only exclusive mode bypasses EFX | Keep `voice_clarity_autofix=true` (default) so the WASAPI-exclusive bypass engages — the only Windows strategy wired into the production ladder at HEAD (Tier 1 / Tier 2 flags exist but are not wired; see below) |
| `sovyx voice` reports the wrong default device after USB hot-plug | Pre-IMMNotificationClient polling loop (5 s window) | Set `SOVYX_TUNING__VOICE__MM_NOTIFICATION_LISTENER_ENABLED=true` (v0.25.0+) |
| `voice_capture_permanently_degraded` event after multiple failed cascades | Endpoint quarantined by `_quarantine_endpoint` with no viable bypass | Hot-plug the device (remove + readd) OR wait `kernel_invalidated_recheck_interval_s` (default 5 min) for automatic recheck |

## Tuning knobs by feature flag

The Voice Windows Paranoid Mission ships 5 feature flags on
`VoiceTuningConfig`. Current defaults at HEAD: only
`probe_cold_strict_validation_enabled` has flipped to `True` (at
v0.32.3); the other four remain **opt-in (`False`)** — the
default-flips once planned for v0.25.0/v0.26.0 never landed and have
no current target version.

### `probe_cold_strict_validation_enabled`

**Env var:** `SOVYX_TUNING__VOICE__PROBE_COLD_STRICT_VALIDATION_ENABLED`

**Default:** `True` (flipped `False → True` at v0.32.3; the original
v0.25.0 flip plan was deferred)

**What it does:** when `True`, the cold-probe diagnosis at
`voice/health/probe/_cold.py::_diagnose_cold` rejects silent combos
(`rms_db < probe_rms_db_no_signal`, default −70 dBFS) as
`Diagnosis.NO_SIGNAL` instead of accepting them as `HEALTHY`. The
cascade then advances to the next combo and the silent winner never
persists in `capture_combos.json`.

**When to re-enable it** (if you had set it false):

* You see `voice.probe.cold_silence_rejected{mode=lenient_passthrough}`
  WARN events in the daemon log on every boot.
* The pipeline reports a "winning" combo whose `rms_db_at_validation`
  in `capture_combos.json` is below −70 dBFS.

**When to set it false:**

* You're testing Sovyx on a known-deaf mic and need the legacy
  v0.23.x acceptance behaviour for A/B comparison.

### `bypass_tier1_raw_enabled`

**Env var:** `SOVYX_TUNING__VOICE__BYPASS_TIER1_RAW_ENABLED`

**Default:** `False` (opt-in at HEAD — the v0.26.0 default-flip never
landed; no current target version)

> **Status at HEAD:** the Tier 1 strategy class is implemented but
> **not registered in the production bypass ladder** — the factory
> ships only the WASAPI-exclusive strategy on Windows, so flipping
> this flag currently has no production effect. The strategy is
> deprecated in favour of Tier-3 WASAPI-exclusive coverage (see the
> `_win_raw_communications.py` module docstring).

**What it would do:** when `True` (once wired), the deaf-signal
coordinator includes the Tier 1 RAW + Communications bypass strategy
(`IAudioClient3::SetClientProperties`) in its iteration order. Tier
1 is the cheapest bypass — no exclusive lock (other apps unaffected),
no admin, no registry mutation, sub-millisecond COM call — and covers
the common case where Voice Clarity sits in MFX.

**When to set it true:**

* Your hardware reports `RawProcessingSupported=true` for the capture
  endpoint (check via `sovyx doctor voice_capture_apo`, wired in v0.32.6).
* You want to evaluate Tier 1 alone before enabling Tier 2.

**When to leave it false:**

* On legacy Realtek HD drivers pre-2020 that lie about
  `RawProcessingSupported` (telemetry counter
  `sovyx.voice.health.bypass.tier1_raw.outcome{verdict=property_rejected_by_driver}`
  fires); use Tier 2 instead.

### `bypass_tier2_host_api_rotate_enabled`

**Env var:** `SOVYX_TUNING__VOICE__BYPASS_TIER2_HOST_API_ROTATE_ENABLED`

**Default:** `False` (opt-in at HEAD — the v0.26.0 default-flip never
landed; no current target version)

> **Status at HEAD:** the Tier 2 strategy class is implemented but
> **not registered in the production bypass ladder** — nothing
> constructs it in production; promotion is tracked as deferred work
> (see the `_win_host_api_rotate_then_exclusive.py` module
> docstring). The underlying capture-layer host-API-rotate primitive
> does exist and is exercised by the capture restart ladder.

**What it would do:** when `True` (once wired), the coordinator
includes the Tier 2
host-API rotate-then-exclusive bypass for endpoints whose runtime
`host_api` is MME / DirectSound / WDM-KS. The strategy rotates the
capture stream to WASAPI and then engages exclusive mode, which
bypasses every APO layer (MFX/SFX/EFX) on the capture pipeline.

**Cross-validator:** Tier 2 requires
`cascade_host_api_alignment_enabled=True`; setting one without the
other fails at boot with a remediation hint. See
`engine/config.py::_enforce_paranoid_mission_dependencies`.

**When to set it true:**

* Tier 1 RAW alone didn't fix Voice Clarity (likely VC sits in EFX,
  not MFX) — only exclusive bypasses EFX.
* The cascade winner picked MME / DirectSound / WDM-KS.

**When to leave it false:**

* You don't want exclusive-mode contention with other apps. Note
  that this is a different category from Tier 1's contention model
  — Tier 1 is contention-free; Tier 2 takes the exclusive lock.

### `mm_notification_listener_enabled`

**Env var:** `SOVYX_TUNING__VOICE__MM_NOTIFICATION_LISTENER_ENABLED`

**Default:** `False` (opt-in at HEAD — the v0.26.0 default-flip never
landed; no current target version)

**What it does:** when `True`, Sovyx registers an
`IMMNotificationClient` with Windows so it can react to default-
device changes (USB hot-plug, sound-settings panel flip) within
~100 ms instead of the legacy 5-second polling loop.

**When to set it true:**

* You frequently hot-plug headsets / dock the mic and want sub-
  second pipeline recovery.
* You're on Windows 10 1809+ / Windows 11 (any version).

**When to leave it false:**

* You're on Windows Server / Windows IoT Core where COM
  initialisation is restricted.
* The polling loop is sufficient for your usage.

### `cascade_host_api_alignment_enabled`

**Env var:** `SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED`

**Default:** `False` (opt-in at HEAD — the v0.25.0 default-flip never
landed; no current target version)

**What it does:** when `True`, the opener's `_device_chain` honours
the cascade-winner's `host_api` and the operator-ranked
`capture_fallback_host_apis` list when iterating siblings on device-
error reopens. Closes Furo W-4 (cascade ↔ runtime drift). Pre-
requisite for `bypass_tier2_host_api_rotate_enabled` (cross-validator
enforces).

**When to set it true:**

* You see deaf-signal events on a multi-host_api endpoint where the
  cascade picked DirectSound / WDM-KS but the runtime drifted to MME.
* You're enabling Tier 2 bypass.

**When to leave it false:**

* You're testing Sovyx on a single-host_api endpoint where drift is
  impossible and want to keep the v0.23.x enumeration order
  semantics for A/B comparison.

## Master kill switch

`voice_clarity_autofix=True` (default) is the master switch for every
bypass strategy. Set
`SOVYX_TUNING__VOICE__VOICE_CLARITY_AUTOFIX=false` to disable every
auto-bypass while preserving APO detection (the
`voice_apo_detected` event still fires — the operator just doesn't
get auto-recovery).

Do **NOT** add a parallel master switch to a tier-specific flag; the
single-master pattern is intentional (anti-pattern #12 — one
understood layer beats three mysterious ones).

## Diagnostics

### `sovyx doctor voice_capture_apo` (wired in v0.32.6)

Renders a Rich table with one row per check:

* **Active endpoint name** — what PortAudio reports as the current
  capture device.
* **Detected APOs** — per-endpoint APO list from the registry scan
  (`voice/_apo_detector.py`).
* **Host API** — `host_api_name` of the runtime stream
  (cascade-winner vs runtime-actual; alignment SLI lives here).
* **Signal-processing mode** — `RAW` / `Default` / `Communications` /
  `Unknown`. Tier 1 RAW success surfaces here.
* **`RawProcessingSupported` flag** — pulled from `IPropertyStore`.
* **`IMMNotificationClient` active** — listener registration health.
* **Current bypass tier** — 0 (none) / 1 (RAW) / 2 (host_api_rotate)
  / 3 (WASAPI exclusive).
* **Capture restart count** — bounded by the frame ring buffer; a
  high number is a flapping signal.
* **Last restart latency** — `recovery_latency_ms` from the most
  recent `CaptureRestartFrame`. Hint emitted when >500 ms.

Exit code: 0 when all OK; non-zero == count of anomalous rows
(matches the existing `sovyx doctor voice` contract).

### `sovyx doctor voice --full-diag` (Windows-native since v0.49.58)

Produces a forensic diagnostic tarball plus an in-process triage
verdict. On Windows the command dispatches to a native producer
(`_run_windows_full_diag` in `cli/commands/doctor.py`) that composes
the WASAPI / APO / mic-consent registry + COM probes into the
Windows-v2 tarball — no bash toolkit required, and the run is
non-interactive (no speech-capture windows, unlike the 8-12 min
interactive Linux bash toolkit; macOS remains unsupported).

* The tarball lands under `~/.sovyx/diagnostics` (parity with the
  Linux toolkit) so it can be re-triaged later.
* The tarball is triaged in-process and the **Triage hypotheses**
  table is rendered, surfacing the operator-facing fix command for
  the highest-confidence hypothesis.
* Exit code: 0 on a clean run with verdict rendered; generic failure
  if the producer or the triage step fails. (`--surgical` shortens
  the Linux toolkit run; the Windows producer is already fast and
  prompt-free.)

### `GET /api/voice/capture-diagnostics`

The dashboard endpoint extends with the same fields the doctor
subcommand surfaces. Operators using the dashboard get the same
diagnosis without dropping to the CLI.

### `GET /api/voice/restart-history?limit=N` (stub v0.24.0; payload v0.25.0)

Returns the last N `CaptureRestartFrame` entries from the orchestrator's
ring buffer. v0.24.0 ships an empty array (route stub for forward
compatibility); v0.25.0 wire-up populates the real payload.

## Telemetry events to grep for

| Event | When it fires | Lever to flip |
|-------|---------------|---------------|
| `voice.probe.cold_silence_rejected{mode=lenient_passthrough}` | Cold probe saw silence but ran in lenient mode (legacy v0.23.x acceptance) | `probe_cold_strict_validation_enabled=true` |
| `voice.probe.cold_silence_rejected{mode=strict_reject}` | Cold probe rejected silent combo — cascade advanced | (none — this is the post-fix success event) |
| `voice_pipeline_deaf_warning` | Per-heartbeat deaf-signal warning (capture alive but no speech energy) | Watch for `voice.deaf.detected` if the streak continues |
| `voice.deaf.detected` | Consecutive deaf warnings reached the auto-bypass threshold | Bypass strategies fire next |
| `sovyx.voice.health.bypass.tier1_raw.outcome{verdict=raw_engaged}` | Tier 1 succeeded | (none — success) |
| `sovyx.voice.health.bypass.tier1_raw.outcome{verdict=property_rejected_by_driver}` | Driver lied about `RawProcessingSupported` | Try Tier 2 |
| `sovyx.voice.health.bypass.tier2_host_api_rotate.outcome{phase_a_verdict=rotated_success, phase_b_verdict=<ExclusiveRestartVerdict>}` | Tier 2 rotate + exclusive completed (`phase_b_verdict="skipped"` means Phase A failed) | (none — success when both phases engaged) |
| `voice.opener.host_api_alignment{aligned=false}` | Opener drifted off cascade winner's host_api (Furo W-4 trigger) | `cascade_host_api_alignment_enabled=true` |
| `voice.hotplug.listener.registered` | IMMNotificationClient registration succeeded | (none — success) |
| `voice_apo_bypass_ineffective` (legacy) / `voice.capture_integrity.bypass_ineffective` (Mission H2 v0.49.7 neutral sibling — same payload + `voice.platform` + `voice.bypass_family` metadata) | All tiers exhausted; pipeline degraded | Hot-plug the device or change the mic |

> **Tier 1 / Tier 2 series are inert at HEAD.** The three
> `sovyx.voice.health.bypass.tier*` rows above are OTel counters
> (registered in `observability/metrics.py`) whose recording helpers
> (`voice/health/_metrics_bypass_tier.py`) have no production caller —
> the Tier 1/2 strategy classes are not registered in the production
> bypass ladder (see the flag sections above). The series stay at zero
> until the strategies are wired.

## Rolling back

Each flag rolls back independently — set the env var to `false` and
restart the daemon. The master switch
(`SOVYX_TUNING__VOICE__VOICE_CLARITY_AUTOFIX=false`) disables every
bypass in one knob.

To roll back to **v0.23.x cold-probe behaviour** specifically:

```
SOVYX_TUNING__VOICE__PROBE_COLD_STRICT_VALIDATION_ENABLED=false
```

To roll back to **v0.23.x opener enumeration order** specifically:

```
SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED=false
SOVYX_TUNING__VOICE__BYPASS_TIER2_HOST_API_ROTATE_ENABLED=false
```

(Tier 2 is gated on alignment; flipping alignment alone needs Tier
2 disabled too or the cross-validator rejects at boot.)

## Reporting a bug

If after running `sovyx doctor voice_capture_apo` and flipping the
flags above the pipeline still doesn't recover, file an issue at
<https://github.com/sovyx-ai/sovyx/issues> with:

1. The full `sovyx doctor voice_capture_apo` Rich table output.
2. The last 100 lines of `~/.sovyx/logs/sovyx.log` showing the
   `voice_pipeline_deaf_warning` events, the `voice.deaf.detected`
   event, and everything downstream.
3. The contents of `~/.sovyx/voice/capture_combos.json`.
4. Output of `Get-WinEvent -LogName "Microsoft-Windows-Audio*"` for
   the relevant time window.
5. The exact mic hardware (USB ID / vendor / model).

## Related documents

* [voice-capture-health.md](voice-capture-health.md) — Voice Capture
  Health Lifecycle (cascade, ComboStore, watchdog).
* [voice-device-test.md](voice-device-test.md) — Device-test session
  surface used by the dashboard's mic-test panel.
* [voice.md](voice.md) — Voice pipeline architecture.
* `docs-internal/ADR-voice-bypass-tier-system.md` — design of the
  3-tier bypass system.
* `docs-internal/ADR-voice-cascade-runtime-alignment.md` — design of
  the opener's 3-tier bucket sort.
* `docs-internal/ADR-voice-imm-notification-recovery.md` — design of
  the IMMNotificationClient device-change listener.
