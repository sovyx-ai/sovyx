# Voice subsystem — OpenTelemetry semantic conventions

Stable naming + attribute conventions for the `sovyx.voice.*` OTel
namespace. This document is the **wire contract** that downstream
dashboards, Grafana alerts, Prometheus rules, Datadog log queries,
and external auditors depend on — names listed here are stable
across minor versions; deprecation follows the policy at the bottom.

> **Phase 7 / T7.41** — industry-contribution artefact for the
> v0.30.0 GA tag. Mirrors the
> [OpenTelemetry Semantic Conventions for Telephony](https://opentelemetry.io/docs/specs/semconv/general/)
> style; aimed at the broader voice-AI ecosystem to reuse where
> beneficial.

---

## Scope

Defines the OTel instruments + attributes the Sovyx voice subsystem
emits. Out of scope: log-event names (covered by JSON-schema files
under `src/sovyx/observability/log_schema/`), tracing span names
(covered by saga conventions in `docs/modules/observability.md`).

The 53 instruments under `sovyx.voice.*` decompose into seven
functional sub-namespaces:

| Sub-namespace | Purpose | Cluster |
|---|---|---|
| `sovyx.voice.aec.*` | Acoustic echo cancellation | Phase 4 / T4 |
| `sovyx.voice.audio.*` | Per-frame signal-quality observations | Phase 4 / T4 |
| `sovyx.voice.capture.*` | Capture-task restart verdicts | v0.24.x paranoid mission |
| `sovyx.voice.driver_update.*` | Windows audio driver-update detection | v0.27.0 / T5.49 |
| `sovyx.voice.health.*` | Cascade + bypass + watchdog observability | Voice health subsystem |
| `sovyx.voice.hotplug.*` | Device hot-plug listener | v0.24.x paranoid mission |
| `sovyx.voice.ns.*` | Noise suppression | Phase 4 / T4 |
| `sovyx.voice.opener.*` | Stream opener attempts | Voice cascade |
| `sovyx.voice.pipeline.*` | Per-pipeline alerts (drift, low-SNR) | Phase 4 / T4 |
| `sovyx.voice.queue.*` | USE-pattern queue observability | v0.26.0 |
| `sovyx.voice.stage.*` | Per-stage RED metrics | v0.26.0 |
| `sovyx.voice.stream.*` | PortAudio stream opens | Pre-v0.24.0 |
| `sovyx.voice.test.*` | Voice test session telemetry | Voice device test |
| `sovyx.voice.tts.*` | TTS synthesis latency | v0.26.0 |
| `sovyx.voice.vad.*` | VAD gating | Phase 4 / T4.39 |
| `sovyx.voice.wake_word.*` | Wake-word detection profile | Phase 7 / T7.1, T7.4, T7.6, T7.7 |

---

## Naming conventions

Follows OTel general conventions plus three Sovyx-specific rules:

### Rule 1 — Sub-namespace > unit > suffix

Every instrument name is `sovyx.voice.<sub-namespace>.<unit_or_action>`
with optional dotted suffix describing the bucket / qualifier.
Examples:

* `sovyx.voice.aec.erle_db` — sub-namespace=aec, unit-suffix=db.
* `sovyx.voice.wake_word.detection_latency` — sub-namespace=wake_word,
  unit-suffix=latency (implicit `_ms` per Rule 2).
* `sovyx.voice.health.bypass_strategy.verdicts` — sub-namespace=health,
  qualifier=bypass_strategy, unit-suffix=verdicts.

### Rule 2 — Latency = ms unless suffixed otherwise

All histograms whose name ends in `_latency`, `_ms`, or `.duration`
record values in **milliseconds**. The `unit` field on the OTel
instrument carries the explicit unit; readers SHOULD respect it
but the naming convention is the contract. Exceptions:

* `_db` suffix → decibels
* `_pct` suffix → percentage 0-100
* `_count` (no suffix on counters) → unitless
* No suffix on counters that emit boolean / categorical events →
  unit is `1` (count of occurrences)

### Rule 3 — Counters end in noun-plural; histograms end in singular-with-unit

| Instrument kind | Suffix pattern | Example |
|---|---|---|
| Counter (cumulative) | `<noun>` plural / `<verb>` past-participle | `sovyx.voice.opener.attempts`, `sovyx.voice.health.kernel_invalidated_events` |
| Histogram (latency) | `<noun>_latency` or `<noun>_ms` | `sovyx.voice.tts.synthesis_latency`, `sovyx.voice.health.bypass.probe_wait_ms` |
| Histogram (gauge-like, e.g. dB) | `<noun>_<unit>` | `sovyx.voice.aec.erle_db`, `sovyx.voice.audio.snr_db` |
| Histogram (depth, count) | `<noun>` (with explicit `unit="1"`) | `sovyx.voice.queue.depth` |

---

## Common attribute reference

These attributes recur across multiple instruments. Each attribute
is **cardinality-bounded** (≤ 50 values per series) so the
dashboard's TopN widgets are tractable.

### `host_api` (string)

PortAudio host API in use. Cardinality-bounded by platform:

* Windows: `WASAPI`, `Windows WASAPI`, `MME`, `Windows DirectSound`,
  `Windows WDM-KS`
* Linux: `ALSA`, `JACK`, `PipeWire`, `PulseAudio`, `OSS`
* macOS: `CoreAudio`, `Core Audio`
* Sentinel: `unknown` (used when the host_api couldn't be resolved
  at the emission site)

### `platform` (string)

Operating-system family; values: `win32`, `linux`, `darwin`. Aligns
with `sys.platform`. Used on cascade attempt counters + driver-update
metrics so dashboards can split per-OS contribution.

### `diagnosis` (string)

Voice probe diagnosis from `sovyx.voice.health.contract.Diagnosis`
StrEnum. 23 values as of v0.27.0:

`healthy`, `muted`, `no_signal`, `low_signal`, `format_mismatch`,
`apo_degraded`, `vad_insensitive`, `driver_error`, `device_busy`,
`exclusive_mode_not_available`, `insufficient_buffer_size`,
`invalid_sample_rate_no_auto_convert`, `permission_denied`,
`permission_revoked_runtime`, `kernel_invalidated`,
`stream_open_timeout`, `heartbeat_timeout`, `mixer_zeroed`,
`mixer_saturated`, `mixer_unknown_pattern`, `mixer_customized`,
`unknown`.

The enum is closed; new values land via additive minor releases
+ stay backward-compatible (consumers ignoring unknown values is
the recommended pattern).

### `outcome` (string)

Generic verdict label. Conventional values: `success`, `error`,
`drop`, `confirmed`, `rejected`, `applied`, `not_applicable`. Each
counter / histogram documents the specific subset it uses in its
description string.

### `model_name` (string)

ONNX checkpoint identifier (file stem). Used by wake-word + STT +
TTS metrics so dashboards can split by deployed model variant.
Cardinality bounded by the small set of installed model files.

### `mode` (string)

Probe mode discriminator: `cold`, `warm`. From
`sovyx.voice.health.contract.ProbeMode`.

---

## Instrument catalog

### `sovyx.voice.aec.*` (Phase 4 — AEC)

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.aec.erle_db` | Histogram | dB | Echo Return Loss Enhancement; target p50 ≥ 35 dB, p95 ≥ 30 dB sustained when render+capture both active |
| `sovyx.voice.aec.engaged` (counter via aec.bypass_combo + aec.windows) | Counter | 1 | AEC engagement events |
| `sovyx.voice.aec.bypass_combo` | Counter | 1 | AEC bypass-combo detection events. Labels: `bypass_combo_kind` |
| `sovyx.voice.aec.double_talk` | Counter | 1 | Double-talk detection events |
| `sovyx.voice.aec.windows` | Counter | 1 | Per-AEC-window emission counter for sliding-window ERLE p50/p95 telemetry |

### `sovyx.voice.audio.*` (per-frame signal quality)

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.audio.snr_db` | Histogram | dB | Signal-to-noise ratio at the FrameNormalizer output |
| `sovyx.voice.audio.phase_inversion_recovery` | Counter | 1 | Phase-inversion auto-recovery events |
| `sovyx.voice.audio.resample_peak_clip` | Counter | 1 | Resampler peak-clip warnings |
| `sovyx.voice.audio.signal_destroyed` | Counter | 1 | "Signal destroyed upstream" emissions (Voice Clarity APO + analogues) |

### `sovyx.voice.wake_word.*` (Phase 7 — wake-word profile)

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.wake_word.stage1_inference_latency` | Histogram | ms | Per-frame ONNX inference (T7.1). Labels: `model_name`. Pi 5 typical: ~5 ms; N100: ~1 ms |
| `sovyx.voice.wake_word.stage2_collection_latency` | Histogram | ms | Stage-1-trigger → evaluation wall clock (T7.1). Labels: `outcome=confirmed\|rejected_threshold\|rejected_verifier` |
| `sovyx.voice.wake_word.stage2_verifier_latency` | Histogram | ms | STT verifier call duration (T7.1). Labels: `outcome=verified\|rejected` |
| `sovyx.voice.wake_word.detection_latency` | Histogram | ms | End-to-end stage-1-trigger → confirmed detection (T7.1). **v0.30.0 GA gate target: p95 ≤ 500 ms** |
| `sovyx.voice.wake_word.confidence` | Histogram | 1 | ONNX score at confirmed detection (T7.6). Labels: `detection_path=two_stage\|fast_path` |
| `sovyx.voice.wake_word.fast_path_engaged` | Counter | 1 | T7.4 fast-path engagement events. Labels: `score_bucket=<0.80\|0.80-0.85\|0.85-0.90\|0.90-0.95\|0.95-1.00` |
| `sovyx.voice.wake_word.false_fire_count` | Counter | 1 | Wake fired + STT discarded transcript (T7.7). Labels: `reason=empty_transcription\|rejected_transcription\|sub_confidence` |
| `sovyx.voice.wake_word.detection_method` | Counter | 1 | Per-detection method label. T8.19. Labels: `method=onnx\|stt_fallback`, `mind_id` |
| `sovyx.voice.wake_word.resolution_strategy` | Counter | 1 | Per-mind boot wake-word model resolution. T8.12. Labels: `strategy=exact\|phonetic\|none`, `mind_id` |

### `sovyx.voice.audio_error.*` (Phase 7 — error translation)

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.audio_error.translated` | Counter | 1 | Increments per call to ``translate_audio_error``. T7.27 + T7.28. Labels: `class=device_not_found\|device_in_use\|device_disconnected\|permission_denied\|unsupported_format\|buffer_size_error\|exclusive_mode_denied\|driver_failure\|invalid_argument\|service_not_running\|unknown` (closed-set, cardinality bounded by `AudioErrorClass` enum). Operator dashboards: histogram of error patterns over time — spike in `permission_denied` = TCC/Group-Policy regression after OS update; spike in `device_in_use` = competing app installed; rising `unknown` ratio = translation table needs new entries |

### `sovyx.voice.health.*` (cascade + bypass + watchdog)

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.health.cascade.attempts` | Counter | 1 | Cascade attempt outcomes. Labels: `host_api`, `platform`, `outcome=won\|exhausted\|raised` |
| `sovyx.voice.health.bypass_strategy.verdicts` | Counter | 1 | Per-strategy bypass verdicts. Labels: `strategy`, `verdict=applied_healthy\|not_applicable\|...` |
| `sovyx.voice.health.bypass.improvement_resolution` | Counter | 1 | Bypass coordinator improvement-heuristic outcomes |
| `sovyx.voice.health.bypass.probe_wait_ms` | Histogram | ms | Bypass probe-window wait |
| `sovyx.voice.health.bypass.probe_window_contaminated` | Counter | 1 | Bypass probe-window contamination events (mark/tap regression guard) |
| `sovyx.voice.health.bypass_tier` | Counter | 1 | Bypass-tier engagement |
| `sovyx.voice.health.capture_integrity.verdicts` | Counter | 1 | CaptureIntegrityCoordinator post-apply verdicts. Labels: `verdict=healthy\|applied_still_dead\|inconclusive\|...` |
| `sovyx.voice.health.combo_store.hits` | Counter | 1 | ComboStore lookup hits |
| `sovyx.voice.health.combo_store.invalidations` | Counter | 1 | ComboStore invalidations. Labels: `reason` |
| `sovyx.voice.health.probe.diagnosis` | Counter | 1 | Per-diagnosis probe-result counter. Labels: `diagnosis`, `host_api`, `platform`, `mode` |
| `sovyx.voice.health.probe.duration` | Histogram | ms | Probe wall-clock duration |
| `sovyx.voice.health.probe.cold_silence_rejected` | Counter | 1 | Furo W-1 cold-probe silence rejection. Labels: `mode=strict_reject\|lenient_passthrough`, `host_api` |
| `sovyx.voice.health.probe.start_time_errors` | Counter | 1 | Stream-start exceptions (post-open). Labels: `diagnosis`, `host_api`, `platform` |
| `sovyx.voice.health.recovery.attempts` | Counter | 1 | Watchdog recovery cycle counter |
| `sovyx.voice.health.kernel_invalidated.events` | Counter | 1 | KERNEL_INVALIDATED emissions |
| `sovyx.voice.health.apo_degraded.events` | Counter | 1 | APO_DEGRADED emissions |
| `sovyx.voice.health.preflight.failures` | Counter | 1 | Preflight check failures |
| `sovyx.voice.health.active_endpoint.changes` | Counter | 1 | Default-device-change events |
| `sovyx.voice.health.self_feedback.blocks` | Counter | 1 | Self-feedback gate blocks |
| `sovyx.voice.health.time_to_first_utterance` | Histogram | ms | KPI per ADR §5.14: Wake → SpeechStarted. Target p95 ≤ 200 ms |

### `sovyx.voice.capture.*`, `sovyx.voice.driver_update.*`, `sovyx.voice.hotplug.*`, `sovyx.voice.opener.*`, `sovyx.voice.stream.*`

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.capture.exclusive_restart.verdicts` | Counter | 1 | Capture-task exclusive-restart verdicts |
| `sovyx.voice.capture.shared_restart.verdicts` | Counter | 1 | Capture-task shared-restart verdicts |
| `sovyx.voice.driver_update.detected` | Counter | 1 | Windows audio driver-update events (WMI subscription, T5.49) |
| `sovyx.voice.hotplug.listener.registered` | Counter | 1 | Hot-plug listener registration outcomes. Labels: `registered=true\|false` |
| `sovyx.voice.opener.attempts` | Counter | 1 | Opener-pyramid attempt outcomes. Labels: `host_api`, `error_code`, `result=ok\|fail` |
| `sovyx.voice.opener.host_api_alignment` | Counter | 1 | Cascade winner ↔ opener alignment events |
| `sovyx.voice.stream.open.attempts` | Counter | 1 | Pre-cascade PortAudio stream-open attempts |

### `sovyx.voice.ns.*`, `sovyx.voice.pipeline.*`, `sovyx.voice.tts.*`, `sovyx.voice.vad.*`

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.ns.suppression_db` | Histogram | dB | Noise-suppression delta |
| `sovyx.voice.ns.windows` | Counter | 1 | NS window emissions |
| `sovyx.voice.pipeline.snr_low_alerts` | Counter | 1 | SNR-low alert dedup-flap counter |
| `sovyx.voice.pipeline.noise_floor_drift_alerts` | Counter | 1 | Noise-floor drift trend-alert counter |
| `sovyx.voice.tts.synthesis_latency` | Histogram | ms | Per-engine-family TTS synthesis duration. Labels: `engine_family=kokoro:<lang>\|piper:<lang>`, `outcome` |
| `sovyx.voice.vad.quiet_signal_gated` | Counter | 1 | T4.39 VAD quiet-signal gate events. Labels: `state=would_gate\|gated_by_user_flag` |

### `sovyx.voice.queue.*`, `sovyx.voice.stage.*`

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.queue.depth` | Histogram | 1 (count) | USE-Utilization — current async-queue depth. Labels: `owner=capture\|vad\|stt\|tts\|output` |
| `sovyx.voice.queue.saturation_pct` | Histogram | % | USE-Saturation — depth as percentage of capacity reference |
| `sovyx.voice.stage.events` | Counter | 1 | Per-stage RED-Rate event counter. Labels: `stage`, `kind=success\|error\|drop`, `error_type=<top-N bucketed>` |
| `sovyx.voice.stage.duration` | Histogram | ms | Per-stage RED-Duration. Labels: `stage`, `outcome=success\|error` |

### `sovyx.voice.test.*`

| Instrument | Kind | Unit | Description |
|---|---|---|---|
| `sovyx.voice.test.sessions` | Counter | 1 | Voice test session counters |
| `sovyx.voice.test.clipping.events` | Counter | 1 | Clipping detected during voice test |
| `sovyx.voice.test.stream.open.latency` | Histogram | ms | Stream-open latency for voice test |
| `sovyx.voice.test.output.synthesis.latency` | Histogram | ms | Synth-side latency for voice test |
| `sovyx.voice.test.output.playback.latency` | Histogram | ms | Playback-side latency for voice test |

---

## Stability + deprecation policy

The names listed above are **stable wire contracts** — adding new
instruments + label values is allowed in any minor release; renaming
or repurposing existing names is a **breaking change** and follows
this policy:

1. **Deprecation in minor release N**: docstring on the instrument
   announces deprecation + names the replacement; both old + new
   instruments emit in parallel.
2. **Removal in minor release N + 2 (≥ 2 minors after deprecation)**:
   the old name is dropped from the codebase. Dashboards / alerts
   must migrate within the deprecation window.
3. **Major releases (1.0+)**: deprecation window may extend across
   multiple minors at maintainers' discretion to give the broader
   ecosystem time to migrate.

The `sovyx.voice.*` namespace itself is reserved exclusively for
Sovyx-emitted instruments. Third parties extending Sovyx with
plugins or hooks SHOULD emit under their own namespace
(e.g. `<vendor>.sovyx.<feature>.*`) to avoid collisions.

---

## Industry alignment

The conventions above intentionally mirror established voice-AI
telemetry baselines so dashboards built for one platform translate
with minimal effort:

| Convention | Source | Sovyx equivalent |
|---|---|---|
| Per-call quality dashboard (CQD) | Microsoft Teams CQD | T7.42 per-session structured event (planned) |
| ERLE histograms | Pexip / RingCentral / Zoom telemetry | `sovyx.voice.aec.erle_db` |
| Time-to-first-utterance | Alexa / Google Assistant industry KPI | `sovyx.voice.health.time_to_first_utterance` |
| Wake-word p95 ≤ 500 ms | Alexa / Google / Siri industry baseline | `sovyx.voice.wake_word.detection_latency` |

---

## See also

* [`docs/modules/voice.md`](voice.md) — voice subsystem architecture overview
* [`docs/modules/observability.md`](observability.md) — cross-subsystem
  observability conventions (logs, traces, sagas)
* `docs-internal/missions/MISSION-voice-final-skype-grade-2026.md`
  §Phase 7 / T7.41 — design spec for this artefact
* [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/) —
  upstream conventions Sovyx aligns with
