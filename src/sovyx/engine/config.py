"""Sovyx engine configuration.

Loads configuration from system.yaml, environment variables (SOVYX_ prefix),
and programmatic overrides. Priority: overrides > env > yaml > defaults.
"""

from __future__ import annotations

import os
from datetime import datetime  # noqa: TC003 — pydantic resolves field type at runtime.
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sovyx.engine._home_path import resolve_home_dir
from sovyx.engine.errors import ConfigNotFoundError, ConfigValidationError


class LoggingConfig(BaseModel):
    """Structured logging configuration.

    Console and file outputs use **independent** formats:

    - **console_format** controls ``StreamHandler`` output:
      ``"text"`` (default) for colored human-readable logs,
      ``"json"`` for machine-parseable output (CI/systemd).

    - **File handler** always writes JSON (for dashboard log viewer
      and ``sovyx logs --json``).  This is by design — the file is a
      machine interface, not a human one.

    Backward compatibility:
        Legacy ``format`` key in system.yaml is silently migrated
        to ``console_format`` with a deprecation warning.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console_format: Literal["json", "text"] = "text"
    log_file: Path | None = None


class DatabaseConfig(BaseModel):
    """SQLite database configuration."""

    data_dir: Path = Field(
        # #32 — defensive home resolution. ``Path.home()`` raises
        # RuntimeError on POSIX containers without HOME and the daemon
        # would crash before any structured error fires. ``resolve_home_dir``
        # falls back to a per-user tempdir with a structured WARN.
        default_factory=lambda: resolve_home_dir() / ".sovyx",
    )
    wal_mode: bool = True
    mmap_size: int = 256 * 1024 * 1024  # 256MB
    cache_size: int = -64000  # 64MB (negative = KB)
    read_pool_size: int = 3


class TelemetryConfig(BaseModel):
    """Telemetry opt-in/out configuration."""

    enabled: bool = False


class RelayConfig(BaseModel):
    """Relay server configuration."""

    enabled: bool = False


class APIConfig(BaseModel):
    """REST API configuration."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 7777
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:7777"])


class HardwareConfig(BaseModel):
    """Hardware tier detection configuration."""

    tier: Literal["auto", "pi", "n100", "gpu"] = "auto"
    mmap_size_mb: int = 128


class LLMProviderConfig(BaseModel):
    """Configuration for a single LLM provider."""

    name: str
    model: str
    api_key_env: str = ""
    endpoint: str | None = None
    timeout_seconds: int = 30
    circuit_breaker_failures: int = 3
    circuit_breaker_reset_seconds: int = 300


class LLMDefaultsConfig(BaseModel):
    """Engine-level LLM defaults. MindConfig.llm can override per-Mind."""

    routing_strategy: Literal["auto", "always-local", "always-cloud"] = "auto"
    providers: list[LLMProviderConfig] = Field(default_factory=list)
    degradation_message: str = (
        "I'm having trouble thinking clearly right now — "
        "my language models are unavailable. I can still "
        "remember things and listen to you."
    )


class SafetyTuningConfig(BaseSettings):
    """Tunable thresholds for the cognitive safety subsystem.

    All defaults match the previously hardcoded module-level constants —
    overriding via env vars (``SOVYX_TUNING__SAFETY__*``) or
    ``system.yaml`` is purely additive (zero behaviour change at default).

    Inherits from ``BaseSettings`` so that direct instantiation
    (``SafetyTuningConfig()``) honours ``SOVYX_TUNING__SAFETY__*`` env
    overrides — the module-level ``_CONST = _Tuning().field`` pattern
    used by subsystem modules relies on this.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_TUNING__SAFETY__", extra="ignore")

    audit_flush_interval_seconds: float = 10.0
    audit_buffer_max: int = 100
    pii_ner_timeout_seconds: float = 2.0
    notification_debounce_seconds: float = 900.0  # 15 minutes


class BrainTuningConfig(BaseSettings):
    """Tunable thresholds for the Brain memory subsystem.

    See :class:`SafetyTuningConfig` for the ``BaseSettings`` rationale.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_TUNING__BRAIN__", extra="ignore")

    star_topology_k: int = 15
    novelty_high_similarity: float = 0.85  # >= -> novelty 0.05 (near-dup)
    novelty_low_similarity: float = 0.30  # <= -> novelty 0.95 (very novel)
    cold_start_threshold: int = 10
    cold_start_novelty: float = 0.70
    model_download_cooldown_seconds: int = 900  # 15 minutes


class VoiceTuningConfig(BaseSettings):
    """Tunable thresholds for the voice pipeline + STT/TTS engines.

    See :class:`SafetyTuningConfig` for the ``BaseSettings`` rationale.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_TUNING__VOICE__", extra="ignore")

    # ── Mission #11 hardening: pydantic Field bounds on the highest-
    #    risk timeout / threshold fields. Catches misconfiguration at
    #    config-load time (e.g.
    #    ``SOVYX_TUNING__VOICE__TRANSCRIBE_TIMEOUT_SECONDS=0`` would
    #    instant-fail every transcription; pre-hardening it loaded
    #    silently) instead of at usage time. Bounds chosen from
    #    perceptual + operational ceilings; widen via deliberate code
    #    change, never via env override.
    transcribe_timeout_seconds: float = Field(default=10.0, ge=0.5, le=120.0)
    """STT transcription budget. Floor 0.5 s prevents instant-fail
    misconfigurations; ceiling 120 s caps the worst-case wait so a
    wedged backend doesn't hang the daemon."""

    streaming_drain_seconds: float = Field(default=0.5, ge=0.0, le=10.0)
    """Post-stop drain budget for the moonshine streaming listener.
    Above 10 s the next turn is wedged behind a tail of stale events."""

    cloud_stt_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    """Cloud STT (OpenAI Whisper API) request timeout. Floor 1 s
    prevents instant-fail; ceiling 300 s = 5 min, beyond which the
    request is structurally hung and the user should retry."""

    cloud_stt_max_audio_seconds: float = Field(default=120.0, ge=1.0, le=600.0)
    """Maximum audio duration accepted by the cloud STT endpoint.
    OpenAI's documented cap is 25 MB raw audio (~10 min @ 16 kHz);
    the 600 s ceiling matches that with headroom for retries."""
    auto_select_min_gpu_vram_mb: int = 4_000
    auto_select_high_ram_threshold_mb: int = 16_000
    auto_select_low_ram_threshold_mb: int = 2_048
    capture_reconnect_delay_seconds: float = 2.0
    capture_queue_maxsize: int = 256

    # Band-aid #20 + Phase 6 / T6.19: per-entry probe-history size in
    # :class:`sovyx.voice.health.combo_store.ComboStore`. Default raised
    # from 10 to 100 in v0.28.0 — the previous value was tuned for
    # memory-constrained embedded targets at the time of band-aid #20,
    # but operators triaging recurring failures (T6.17 ping-pong, T6.18
    # rapid re-quarantine) need deeper rolling history per endpoint.
    # 100 entries at ~150 bytes per ``ProbeHistoryEntry`` = ~15 KB per
    # endpoint. With the default 64-entry combo-store maxsize, that
    # caps ComboStore at ~1 MB on disk + RAM — negligible.
    #
    # Cross-boot persistence is INHERENT to the design: ``probe_history``
    # is serialised by ``_history_to_dict`` / ``_entry_to_dict`` and
    # reloaded by ``_build_live_entry`` on every ``load()`` — no extra
    # work needed for T6.19's "persist across reboots" sub-requirement.
    #
    # Lower bound 1 (degenerate single-entry mode for memory-tight
    # embedded targets); upper bound 1000 (10-day session of hourly
    # reconnects fits without rotation). Operators can downscale via
    # ``SOVYX_TUNING__VOICE__COMBO_PROBE_HISTORY_MAX=10`` to restore
    # pre-v0.28.0 behaviour without code change.
    combo_probe_history_max: int = Field(default=100, ge=1, le=1_000)

    # Band-aid #28: LLM provider reachability preflight budget.
    # The ADR §4.5 step 7 (LLM_UNREACHABLE) check has no enforced
    # ceiling pre-band-aid #28 — a hung provider would block boot
    # indefinitely. 3 s default matches the spec's "3 s HEAD ping"
    # cadence; bounded ``[0.5, 60]`` so a misconfigured 0 doesn't
    # instant-fail every call and a runaway 600 doesn't wedge the
    # daemon past any operator's patience window.
    llm_preflight_timeout_seconds: float = Field(default=3.0, ge=0.5, le=60.0)

    # ── Voice factory preflight gates (band-aid #34 / #28 wire-ups) ──
    # Both default to False to preserve pre-wire-up behaviour. Operators
    # opt in once they've validated the gate doesn't false-fire on
    # their hardware. Per the staged-adoption discipline saved as the
    # ``Staged adoption — foundation → wire-up incrementally`` feedback
    # memory: never bundle foundation + 5 call-site adoptions in one
    # commit; lenient default for new validators.
    voice_check_mic_permission_enabled: bool = False
    """Band-aid #34 wire-up: when True the voice factory probes the
    OS microphone-permission state via
    :func:`sovyx.voice.health._mic_permission.check_microphone_permission`
    BEFORE creating the pipeline. On Windows DENIED → raises
    :class:`VoicePermissionError` with the literal Settings path
    operators must navigate. On UNKNOWN / non-Windows the gate is a
    no-op (the cascade's own deaf-detection covers the residual)."""

    voice_check_llm_reachable_enabled: bool = False
    """Band-aid #28 wire-up: when True the voice factory probes the
    LLM router's provider list via
    :func:`sovyx.voice.health.preflight.check_llm_reachable` BEFORE
    creating the pipeline. On FAIL the factory logs a structured
    WARN but does NOT raise — the LLM might be the kind that takes
    a few seconds to come up after Sovyx boots, and blocking the
    whole voice pipeline on it would surface as "voice broken"
    rather than "LLM not yet ready"."""

    voice_pipewire_detection_enabled: bool = True
    """F3 wire-up: when True, voice factory runs
    :func:`sovyx.voice.health._pipewire.detect_pipewire` on Linux
    startup and emits the verdict via structured event so dashboards
    surface Layer-1 status. PURE OBSERVABILITY — never auto-loads
    ``module-echo-cancel`` (mutating the user's PipeWire state
    without explicit consent would surprise operators). Default True
    because read-only detection is safe; non-Linux platforms skip
    silently via the platform guard inside detect_pipewire."""

    voice_alsa_ucm_detection_enabled: bool = True
    """F4 wire-up: when True, voice factory runs
    :func:`sovyx.voice.health._alsa_ucm.detect_ucm` on Linux startup
    for the active ALSA card. PURE OBSERVABILITY — never auto-sets
    a verb. Default True for the same safety reason as
    ``voice_pipewire_detection_enabled``."""

    voice_audio_service_watchdog_enabled: bool = False
    """WI2 wire-up: when True (Windows only), voice factory
    instantiates :class:`AudioServiceWatchdog` and starts it
    alongside the pipeline. Default False because the watchdog's
    rolling 30 s sc.exe queries are an additional process per
    Sovyx instance — operators opt in when they've observed
    audio-service-related issues. Non-Windows platforms skip."""

    voice_probe_macos_diagnostics_enabled: bool = True
    """MA1+MA5+MA6 wire-up (mission §1.5 Step 5): when True (darwin
    only), voice factory runs the macOS audio diagnostic trio at boot:

    * :func:`sovyx.voice._hal_detector_mac.detect_hal_plugins` —
      enumerates ``/Library/Audio/Plug-Ins/HAL/`` for virtual-audio
      and audio-enhancement plug-ins (Krisp, BlackHole, Loopback,
      etc.) that intercept capture before Sovyx sees it.
    * :func:`sovyx.voice._codesign_verify_mac.verify_microphone_entitlement`
      — confirms the running binary's Hardened-Runtime mic
      entitlement.
    * :func:`sovyx.voice._bluetooth_profile_mac.detect_bluetooth_audio_profile`
      — flags A2DP-only headphones connected as input devices.

    Default True: every probe is read-only filesystem-or-subprocess
    observability. The Bluetooth probe spawns ``system_profiler``
    (cold-start ~2-5 s) — the boot-time cost is acceptable for a
    once-per-pipeline diagnostic on macOS, where these failure modes
    are the load-bearing silent-failure paths the mission is designed
    to surface. Capability-gated via :data:`Capability.COREAUDIO_VPIO`
    (validates darwin + system_profiler on PATH); non-darwin platforms
    skip silently."""

    voice_probe_windows_etw_events_enabled: bool = False
    """WI1 wire-up (mission §1.5 Step 4): when True (Windows only),
    voice factory queries the Microsoft-Windows-Audio* ETW operational
    channels at boot and logs structured ``voice.windows.etw_events``
    records.

    Default False because the probe spawns three ``wevtutil.exe``
    subprocesses (one per channel) with a 5 s timeout each — up to
    15 s of additional cold-boot latency on a Windows host with a
    busy event log. The cost is acceptable only when an operator is
    actively debugging audio-service / APO chain issues.

    Non-Windows platforms skip silently via the
    :data:`Capability.ETW_AUDIO_PROVIDER` resolver gate, so enabling
    this on Linux / macOS is a no-op (logged at INFO so operators
    see the mismatch)."""

    voice_apo_dll_introspection_enabled: bool = False
    """WI3 wire-up: when True, the APO detector enriches each
    :class:`~sovyx.voice._apo_detector.CaptureApoReport` with
    DLL version-info for any CLSID NOT already in the static
    catalog. Default False because:
    (1) Each unknown CLSID triggers a registry lookup + DLL header
        read on the audio-enable path, adding ~5-10 ms per
        unknown APO.
    (2) The static catalog covers the vast majority of MS APOs;
        enrichment only matters when investigating a vendor-shipped
        or post-Windows-Update APO not yet in the catalog.
    Operators opt in via ``SOVYX_TUNING__VOICE__VOICE_APO_DLL_INTROSPECTION_ENABLED=true``
    when they need forensic detail in the dashboard."""

    # ── F7 — Layer 4 community telemetry (privacy-first) ──────────
    voice_community_telemetry_enabled: bool = False
    """F7 wire-up: when True AND
    :attr:`voice_community_telemetry_endpoint` is non-empty, the voice
    factory POSTs an anonymised capture-diagnostics payload to the
    community endpoint after a successful capture-enable. Default
    False (privacy-first — telemetry NEVER leaves the user's machine
    without explicit opt-in). All PII fields (endpoint GUID, device
    name) are M1-hashed before transmission; raw values never leave
    the process. See
    :mod:`sovyx.voice.health._telemetry_client` for the payload
    contract + privacy model."""

    voice_community_telemetry_endpoint: str = ""
    """Community-telemetry POST URL. Default empty — even with
    ``voice_community_telemetry_enabled=True`` the client short-
    circuits when the URL is empty. Operators set this via
    ``SOVYX_TUNING__VOICE__VOICE_COMMUNITY_TELEMETRY_ENDPOINT=https://...``
    only when the community-knowledge service exists and the
    privacy review has cleared per-deployment opt-in."""

    # AudioCaptureTask stream health — catches the silent-zeros failure
    # mode where sd.InputStream opens cleanly but delivers all-zero
    # frames (MME + unsupported rate, driver hang, privacy block). See
    # :mod:`sovyx.voice.device_enum` for the root-cause writeup.
    capture_validation_seconds: float = 0.6  # how long to observe frames post-open
    capture_validation_min_rms_db: float = -80.0  # any signal above this = "alive"
    # Default validator only checks *frame presence* — proving the PortAudio
    # callback is firing. Set True to also require RMS above
    # ``capture_validation_min_rms_db`` (setup-wizard / diagnostic mode).
    # Rationale: a user who is silent at boot has legitimately quiet input
    # and a pure-RMS gate rejects perfectly good variants, sending the
    # opener into its worst fallback (e.g. 48 kHz / 2 ch / auto_convert=False).
    capture_validation_require_signal: bool = False
    # Minimum number of callback-delivered frames required for the default
    # presence-mode validator to accept a variant. Three frames at 512
    # samples / 16 kHz = ~96 ms of real callback activity.
    capture_validation_min_frames: int = 3
    capture_heartbeat_interval_seconds: float = 2.0  # RMS/frames log cadence
    # VoicePipeline observability — emits ``voice_pipeline_heartbeat`` every
    # interval with max VAD probability observed, frames processed, and the
    # current FSM state. Essential for diagnosing "VAD never fires" scenarios
    # where audio is captured but the orchestrator stays in IDLE.
    pipeline_heartbeat_interval_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    """Mission #11: floor 0.5 s prevents log-storm from a fast
    heartbeat misconfiguration (would emit ~120 events/min);
    ceiling 60 s ensures the deaf detector's window is bounded
    enough to react before the user gives up."""
    # Deaf-pipeline heuristic: if the orchestrator has processed at
    # least ``pipeline_deaf_min_frames`` frames in the current heartbeat
    # window and the max observed VAD probability never crossed
    # ``pipeline_deaf_vad_max_threshold``, emit
    # ``voice_pipeline_deaf_warning``. This surfaces the class of bug
    # where audio is captured (``audio_capture_heartbeat`` shows real
    # RMS) but VAD silently rejects every frame — typically because the
    # frames reaching :meth:`VoicePipeline.feed_frame` are not 16 kHz
    # mono (FrameNormalizer misconfigured / bypassed).
    pipeline_deaf_min_frames: int = Field(default=150, ge=10, le=10_000)
    """Mission #11: floor 10 prevents instant-trigger from a single
    quiet frame (would cause deaf false-positives on every silent
    pause); ceiling 10 000 caps the window at ~5 min @ 32 ms/frame
    so a misconfigured value can't disable the deaf detector
    entirely (which would re-introduce the silent-failure mode the
    detector exists to surface)."""

    pipeline_deaf_vad_max_threshold: float = Field(default=0.05, ge=0.0, le=0.5)
    """Mission #11: ceiling 0.5 prevents a misconfigured value from
    accepting normal-speech frames as "deaf" (a value above the
    onset threshold would mark every speech window as silent input).
    Floor 0.0 is permissive — equality with 0.0 means "absolutely
    no signal" which is a legitimate strictness level."""
    # Auto-bypass: when the deaf heuristic fires ``N`` heartbeats in a row
    # on an endpoint the :mod:`sovyx.voice._apo_detector` flagged as
    # running Windows Voice Clarity (``voice_clarity_active=True``), the
    # orchestrator asks the capture task to reopen the stream in WASAPI
    # exclusive mode — the only reliable client-side bypass for
    # ``VocaEffectPack`` / ``voiceclarityep`` since early 2026.
    # ``voice_clarity_autofix`` is a master kill-switch — set to ``False``
    # (or ``SOVYX_TUNING__VOICE__VOICE_CLARITY_AUTOFIX=false``) to keep
    # the detector running (it still emits ``voice_apo_detected``) but
    # never auto-retry. The retry attempt is one-shot per pipeline
    # session: if exclusive also fails, we do not oscillate.
    voice_clarity_autofix: bool = True
    agc2_enabled: bool = True
    """F5/F6 — promote AGC2 from opt-in (introduced 2026-04-25) to
    default-on. AGC2 is the in-process closed-loop digital gain
    controller that replaces the legacy ``apply_mixer_boost_up``
    band-aid mixer-fractions path on Linux attenuated-mic
    hardware. With ``agc2_enabled=True`` (the default since this
    promotion), every FrameNormalizer constructed by the capture
    task gets an AGC2 attached on its non-passthrough path; the
    passthrough fast-path stays bit-exact (operators rely on that
    for A/B comparisons + golden-recording playback).

    Set to ``False`` (or
    ``SOVYX_TUNING__VOICE__AGC2_ENABLED=false``) to revert to the
    pre-F5 behaviour: no in-process gain control, signal flows
    untouched through the cascade. Useful for users on hardware
    where the OS / mixer chain already delivers correctly-levelled
    audio + the AGC2 overhead is unnecessary, OR for A/B testing
    AGC2 vs raw signal during pilots.

    Risk profile of default-on: AGC2 has slew-rate limiting
    (default 6 dB/sec, perceptually transparent), saturation
    protector (post-gain peak clamped 1 LSB below int16 rail),
    and silence-floor gating (estimator only updates above
    -60 dBFS so noise floor doesn't pump up). In steady state
    with healthy input, AGC2 stays near 0 dB gain and is a
    no-op. Bad-case behaviour is bounded by the [min_gain_db,
    max_gain_db] config — default [-10, 30] dB.
    """
    deaf_warnings_before_exclusive_retry: int = Field(default=2, ge=1, le=20)
    """Mission #11: floor 1 ensures the auto-bypass eventually fires
    (zero would disable it entirely, defeating the autofix feature);
    ceiling 20 caps the wait so a sustained-deaf condition gets the
    bypass within ~100 s @ 5 s heartbeat (the user has already
    started troubleshooting by then; longer is operationally
    pointless)."""

    voice_snr_low_alert_enabled: bool = True
    """Phase 4 / T4.35 — emit a structured WARN
    ``voice_pipeline_snr_low_alert`` from
    :class:`VoicePipeline._track_vad_for_heartbeat` when the
    drained SNR p50 stays below
    :attr:`voice_snr_low_alert_threshold_db` for
    :attr:`voice_snr_low_alert_consecutive_heartbeats` consecutive
    heartbeats.

    Default ``True``: this is OBSERVABILITY (no behaviour change),
    so the foundation ships enabled. Operators on noisy fleets
    can either flip the threshold (looser bound) or disable
    entirely until calibration completes."""

    voice_snr_low_alert_threshold_db: float = Field(default=9.0, ge=-60.0, le=60.0)
    """Per-heartbeat SNR p50 floor below which the low-SNR alert
    accumulates a consecutive-heartbeat counter.

    9 dB is the canonical Moonshine STT degradation threshold per
    master mission §Phase 4 / T4.35 — below it the substitution
    rate climbs sharply. Operators using a different STT engine
    can raise the floor (e.g. 12 dB for Whisper-tiny) or lower it
    (e.g. 6 dB for fine-tuned models tolerant of room noise).

    Bounds ``[-60, 60]`` dB cover every realistic SNR; values
    outside reject at config validation rather than silently
    disable the alert."""

    voice_agc2_vad_feedback_enabled: bool = False
    """Phase 4 / T4.52 — gate AGC2's speech-level estimator
    update on a fresh VAD verdict in addition to the RMS
    floor. When True, the orchestrator publishes each VAD
    verdict to a thread-safe feedback channel after every
    inference; AGC2 reads the freshest verdict on the next
    frame's ``process()`` call and only updates the speech-
    level estimator when the verdict says "speech".

    This eliminates the classic AGC2 noise-pumping failure
    mode: ambient bursts above the RMS silence floor (door
    slams, keyboard typing, HVAC ramp-up, kitchen sounds)
    that pre-T4.52 AGC2 would adapt to as if they were quiet
    speech, producing a few seconds of over-amplified silence
    afterwards.

    Default ``False`` per ``feedback_staged_adoption`` —
    foundation behaviour is unchanged. Operators flip after
    pilot validation confirms the orchestrator's VAD verdict
    rate is reliable on their hardware (a flaky VAD that
    misses real speech would freeze AGC2 mid-utterance).
    Default-flip planned for v0.28.0+ once the gate-rate
    floor (``frames_vad_silenced / frames_processed``) is
    measured on a pilot fleet."""

    voice_noise_floor_drift_alert_enabled: bool = True
    """Phase 4 / T4.38 — emit a structured WARN
    ``voice_pipeline_noise_floor_drift_warning`` from
    :class:`VoicePipeline._track_vad_for_heartbeat` when the
    rolling noise-floor short-window average exceeds the long-
    window baseline by
    :attr:`voice_noise_floor_drift_threshold_db` for
    :attr:`voice_noise_floor_drift_consecutive_heartbeats`
    consecutive heartbeats.

    Default ``True``: pure observability, no behaviour change.
    Operators on hardware with naturally-drifty floors (laptop
    fans cycling, mobile use cases) can flip to False if the
    alert spam outweighs the diagnostic value."""

    voice_noise_floor_drift_threshold_db: float = Field(default=10.0, ge=1.0, le=60.0)
    """Decibels of upward floor drift (short-window avg minus
    long-window avg) that triggers the alert.

    10 dB is the master mission's §Phase 4 / T4.38 contract —
    "alert if floor raised >10 dB (room got noisier)". 10 dB =
    10× linear-power increase, well outside the natural
    variation of a steady environment (typical floor jitter
    is ±2 dB).

    Operators in noisy environments may raise this to 15 dB
    (suppress alarms during routine HVAC cycles) or lower it
    to 6 dB for studio-quality deployments where any drift
    matters. Bounds ``[1, 60]`` reject values below 1 dB
    (would alert on every breath) or above 60 dB (would never
    alert in practice)."""

    voice_noise_floor_drift_consecutive_heartbeats: int = Field(default=3, ge=1, le=60)
    """Number of consecutive heartbeats with drift above
    threshold before the alert WARN fires.

    Default 3 ≈ 90 s at the 30 s heartbeat interval —
    matches the T4.35 SNR low-alert de-flap to keep operator
    intuition uniform across the heartbeat-driven alert family.
    Floor 1 = alert on first sustained crossing; ceiling 60
    caps the wait."""

    voice_snr_low_alert_consecutive_heartbeats: int = Field(default=3, ge=1, le=60)
    """Number of consecutive heartbeats with SNR p50 below the
    threshold before the alert WARN fires.

    Default 3: at the 30 s heartbeat interval that's ~90 s of
    sustained low SNR, well past any transient (door slam, fan
    spike) that would otherwise produce false-positive WARN
    storms. Mirrors the de-flap pattern used by
    :attr:`deaf_warnings_before_exclusive_retry`.

    Floor 1 = alert on the first low heartbeat (no de-flap, for
    very latency-sensitive deployments); ceiling 60 caps the wait
    at half an hour so a chronically-noisy mic still surfaces."""

    # Mission Phase 1 / T1.28 — pipeline-tuning constants migrated from
    # ``voice/pipeline/_orchestrator.py`` module-level. These knobs were
    # originally hardcoded in the orchestrator; promotion here makes
    # them discoverable via the centralised tuning schema, env-var
    # overridable via ``SOVYX_TUNING__VOICE__<NAME>``, and bound-
    # validated against operationally-meaningful ranges.

    pipeline_frame_drop_absolute_budget_seconds: float = Field(default=0.064, ge=0.020, le=1.0)
    """O3 frame-drop detector — per-frame absolute inter-arrival
    budget. Default 64 ms = 2× the nominal 32 ms cadence at 16 kHz /
    512-sample window — the perceptual threshold above which a
    real-time voice loop gains audible latency artefacts (Bencina,
    "Real-Time Audio Programming 101", 2020). Floor 20 ms prevents
    constant misfire on any healthy host (40 ms / 16 kHz cadence
    plus jitter); ceiling 1.0 s prevents a misconfigured value from
    masking real frame drops entirely. A frame exceeding this budget
    fires ``voice.frame.drop_detected`` with
    ``threshold_kind=absolute_budget``."""

    pipeline_frame_drop_drift_window_frames: int = Field(default=32, ge=8, le=256)
    """O3 frame-drop detector — rolling-window size for the
    cumulative-drift detector. 32 frames at 16 kHz / 512-sample
    window = ~1.024 s of audio. Floor 8 prevents false-positives
    from a single jittery frame; ceiling 256 keeps the window
    short enough to react to sustained drift before the user
    notices."""

    pipeline_frame_drop_drift_ratio: float = Field(default=1.10, ge=1.05, le=3.0)
    """O3 frame-drop detector — mean inter-arrival ÷ expected
    interval threshold above which the cumulative-drift detector
    fires. Default 1.10 = 10 % sustained drift. Floor 1.05 prevents
    near-baseline jitter false-positives; ceiling 3.0 prevents
    misconfiguration from disabling the detector entirely (any
    sustained drift this large is structurally broken)."""

    pipeline_frame_drop_drift_rate_limit_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    """O3 frame-drop detector — minimum gap between successive
    ``voice.frame.cumulative_drift_detected`` emissions. Default
    1.0 s — sustained drift produces one event per second, not one
    per window. Floor 0.1 s prevents log-storm; ceiling 60 s keeps
    the cadence operationally useful."""

    pipeline_vad_inference_timeout_seconds: float = Field(default=0.250, ge=0.050, le=2.0)
    """Per-frame VAD inference budget. Silero VAD on a modern CPU
    runs in ~5–20 ms; default 250 ms is ~10× typical, generous
    enough that healthy deployments never trip but tight enough
    that a wedged inference doesn't stall the pipeline for >0.25 s.
    Floor 50 ms accommodates GC pauses without false-firing;
    ceiling 2.0 s guards against the misconfigured-to-disable case."""

    pipeline_vad_inference_timeout_warn_interval_seconds: float = Field(
        default=5.0, ge=0.5, le=300.0
    )
    """Minimum gap between two ``voice.vad.inference_timeout`` WARN
    logs. Default 5.0 s matches the heartbeat cadence so an operator
    sees the issue within the first frame batch after onset. Floor
    0.5 s prevents log-storm; ceiling 300 s prevents misconfiguration
    from suppressing the signal entirely."""

    pipeline_cancellation_task_timeout_seconds: float = Field(default=1.0, ge=0.1, le=30.0)
    """T1 atomic-cancellation chain — per-task timeout when awaiting
    a cancelled in-flight TTS task. Default 1.0 s is the SRE-canonical
    "if it isn't dead by now it's hung" budget — long enough for a
    graceful CancelledError teardown (typical: <50 ms) but short
    enough that a wedged task doesn't block the next turn. Floor
    0.1 s prevents misconfiguration from racing the normal teardown;
    ceiling 30 s caps user-visible barge-in stall on the worst-case
    wedged TTS backend."""

    pipeline_consecutive_tts_failure_threshold: int = Field(default=3, ge=1, le=100)
    """T1.21 streaming TTS abort threshold — number of consecutive
    per-segment failures after which ``stream_text`` aborts the
    streaming session. Default 3 absorbs the typical "first
    inference warm-up failure" pattern (1-2 retries) while still
    aborting within ~3 segments × ~200 ms = ~600 ms when the backend
    is wedged. Floor 1 ensures the abort fires on the first
    failure if the operator wants strict no-retry semantics;
    ceiling 100 prevents misconfiguration from disabling the abort
    entirely (which would re-introduce the pre-T1.21 silent
    compute-burning failure mode). Counter resets on the first
    successful segment so a transient mid-stream hiccup doesn't
    poison the rest of the response."""

    pipeline_coordinator_pending_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    """T1.14 watchdog deadline — maximum wall-clock seconds the
    ``_coordinator_invocation_pending`` flag may stay True before a
    background watchdog force-clears it. Default 30.0 s is the SRE-
    canonical "if it isn't dead by now it's hung" budget for an
    asyncio coordinator task; long enough that a slow-but-progressing
    coordinator (mixer probe, KB lookup, network round-trip) finishes
    cleanly via the T1.23 outer-finally before the watchdog fires,
    short enough that a wedged coordinator doesn't permanently lock
    out subsequent deaf-signal handling. Floor 1.0 s prevents
    misconfiguration from racing the normal teardown; ceiling 300 s
    caps the operator-visible "deaf for 5 minutes after one wedge"
    worst case. The watchdog uses an invocation-counter guard so a
    fired-late watchdog from a completed invocation cannot
    accidentally clear the flag of a SUBSEQUENT invocation."""

    pipeline_speaker_consistency_enabled: bool = Field(default=True)
    """T1.39 — gate the spectral-centroid drift detector. ``True``
    enables :class:`sovyx.voice._speaker_consistency.SpeakerConsistencyMonitor`
    on every successful TTS chunk synthesis (~50 µs DSP cost per chunk).
    Default True; set ``False`` for resource-constrained deployments
    (Pi, low-power embedded) that don't need the drift signal."""

    pipeline_speaker_drift_window_size: int = Field(default=5, ge=2, le=50)
    """T1.39 — rolling-window size for the spectral-centroid baseline.
    The first N-1 chunks build the baseline (no alert); from chunk N
    onward each new centroid is compared to the rolling mean. Default
    5 is noise-resistant (a single anomalous chunk falls out of the
    window after N synthesis cycles) yet quick to detect a sustained
    drift. Floor 2 prevents single-sample baselines (any drift would
    fire); ceiling 50 prevents a baseline so smooth that a real drift
    takes minutes to surface."""

    pipeline_speaker_drift_ratio_threshold: float = Field(default=0.05, ge=0.01, le=1.0)
    """T1.39 — relative-drift threshold above which the WARN +
    PipelineErrorEvent fires. Computed as
    ``|centroid - baseline| / baseline > threshold``. Default 0.05
    (5%) matches the spec text and is empirically the perceptual
    threshold for "the voice changed" on a typical 2.0 kHz speech
    centroid (≈ 100 Hz drift, comparable to a semitone). Operators
    seeing false positives on high-variance voices (operatic,
    expressive synthesis) should bump this; operators wanting a
    tighter alert can reduce it. Floor 0.01 prevents alarm-storm on
    natural variance; ceiling 1.0 is the upper-bound (a 100 % drift
    means the centroid moved by an octave — pathological)."""
    # Ordered host-API preference for the opener's pyramid fallback.
    # VLX-007 added "PipeWire" + "PulseAudio" + "JACK" to cover builds
    # of PortAudio that expose those backends as standalone host APIs
    # (Arch/NixOS futures). The list is a superset — entries absent
    # from the host's PortAudio build are silently skipped.
    capture_fallback_host_apis: list[str] = Field(
        default_factory=lambda: [
            "Windows WASAPI",
            "Core Audio",
            "ALSA",
            "PipeWire",
            "PulseAudio",
            "JACK Audio Connection Kit",
            "Windows DirectSound",
        ],
    )
    # WASAPI-specific opener behaviour. ``auto_convert`` lets the WASAPI
    # backend resample + rechannel + rechannel-type transparently; critical
    # for devices whose Windows mixer format is 2 ch float32 @ 48 kHz but
    # sovyx asks for 1 ch int16 @ 16 kHz (e.g. Razer BlackShark V2 Pro in
    # shared mode). Disable only to reproduce legacy behaviour for A/B.
    capture_wasapi_auto_convert: bool = True
    capture_wasapi_exclusive: bool = False
    # Let the opener upgrade ``channels`` to ``device.max_input_channels``
    # when a mono request is rejected (post-opener mixdown handled by the
    # callback). Hardware that only exposes stereo in shared mode depends
    # on this to pass through without auto_convert.
    capture_allow_channel_upgrade: bool = True

    # Voice model download — mirror failover + cooldown. See
    # :class:`~sovyx.engine._model_downloader.ModelDownloader` for
    # retry/backoff semantics. 15-min cooldown mirrors the brain tier.
    model_download_cooldown_seconds: int = 900

    # ── Voice Capture Health Lifecycle (VCHL) — see ADR-voice-capture-health-lifecycle.md.
    # Probe thresholds (ADR §4.3). Hard timeout per probe is 5 s — anything
    # longer blocks the cascade behind a misbehaving driver.
    probe_cold_duration_ms: int = 1_500
    probe_warm_duration_ms: int = 3_000
    probe_warmup_discard_ms: int = 200
    probe_hard_timeout_s: float = 5.0
    probe_rms_db_no_signal: float = -70.0  # below this → Diagnosis.NO_SIGNAL
    probe_rms_db_low_signal: float = -55.0  # between no_signal and low → LOW_SIGNAL
    # Above this healthy-RMS threshold a dead VAD is diagnosed as APO-corrupted;
    # between this ceiling and the healthy floor it's VAD_INSENSITIVE (gain
    # too low, speaker too far). See ADR §4.3 diagnosis table.
    probe_vad_apo_degraded_ceiling: float = 0.05
    probe_vad_healthy_floor: float = 0.5
    # Cascade budgets (ADR §5.6). Total = 8 attempts × ~3 s;
    # per-attempt = one probe's hard timeout. Wizard budget is higher
    # because a human is watching and can tolerate a slower cascade.
    cascade_total_budget_s: float = 30.0
    cascade_attempt_budget_s: float = 5.0
    cascade_wizard_total_budget_s: float = 45.0
    # Upper bound on concurrent endpoints tracked by the lifecycle-lock
    # LRULockDict (ADR §5.5). 64 is generous — rigs with that many active
    # capture endpoints are exceedingly rare.
    cascade_lifecycle_lock_max: int = 64
    # VLX-005 — when a Linux ``hw:X,Y`` device reports a non-canonical
    # native sample rate (e.g. 44100), prepend a combo targeting that
    # rate to the cascade table so we don't burn attempts on
    # ``paInvalidSampleRate`` (-9997) failures. Bounds protect against
    # drivers reporting junk (4 Hz ultrasonic, 10 MHz, etc.) — a rate
    # outside the window is ignored and the default cascade table is
    # used unchanged.
    cascade_native_rate_min_hz: int = 8_000
    cascade_native_rate_max_hz: int = 192_000
    # T10 — Linux session-manager grab detector (``sovyx doctor
    # linux_session_manager_grab`` + ``/api/voice/capture-diagnostics``).
    # pactl wall-clock cap: 2 s covers laptop + server (~200 ms typical).
    detector_pactl_timeout_s: float = 2.0
    # /proc scan wall-clock cap: 1.5 s protects against hosts with
    # tens of thousands of PIDs.
    detector_proc_timeout_s: float = 1.5
    # /proc scan PID count cap: 5 000 is ~1 000× the typical desktop.
    detector_proc_max_scan: int = 5_000
    # Evidence string cap for the report payload. 2 KiB fits in OTLP
    # attribute limits and is enough for the pactl section that
    # mentions the grabbing app.
    detector_evidence_max_chars: int = 2_048

    # L4 runtime resilience (ADR §4.4, Sprint 2). Master kill-switch that
    # disables the whole watchdog + hot-plug + power + default-change
    # listener surface while preserving L0–L3 (cascade + ComboStore +
    # pre-flight still work). See ADR §7 rollback path.
    runtime_resilience_enabled: bool = True
    # §4.4.1 exponential-backoff schedule for warm re-probes on sustained
    # deafness. Defaults are the ADR commitment (+10 s, +30 s, +90 s, max
    # 3 attempts per session). On exhaustion the watchdog emits
    # ``voice_capture_permanently_degraded`` and drops to push-to-talk.
    watchdog_backoff_schedule_s: list[float] = Field(
        default_factory=lambda: [10.0, 30.0, 90.0],
    )
    watchdog_max_attempts: int = 3
    # §4.4.4 power-event settle window. After PBT_APMRESUMEAUTOMATIC (or
    # its Linux/macOS equivalents) the watchdog waits this long before
    # re-cascading — USB hubs and BT audio stacks routinely take 1-2 s to
    # re-enumerate after resume. 2.0 s mirrors the ADR commitment.
    watchdog_resume_settle_s: float = 2.0
    # §4.4.5 audio-service crash recovery ceiling. When ``audiosrv``
    # (Windows) or its equivalents transition to Stopped, the watchdog
    # waits up to this many seconds for the service to come back before
    # emitting ``voice_audio_service_down`` (ERROR) and going DEGRADED.
    watchdog_audio_service_restart_timeout_s: float = 30.0
    # §4.4.3 default-device poll interval. Platforms without a native
    # notification path (and the Windows fallback when IMM registration
    # fails) poll ``sounddevice`` at this cadence to notice the user
    # changing the default mic in the Sound Settings panel.
    watchdog_default_device_poll_s: float = 5.0
    # §4.4.5 audio-service poll interval. Between polls the monitor
    # sleeps this long before querying the service state again. Tighter
    # than default-device polling because ``audiosrv`` death is a P0
    # interruption for the user.
    watchdog_audio_service_poll_s: float = 2.0

    # §4.4.6 self-feedback isolation. When OS echo-cancel is bypassed
    # (cascade attempts 1-4 Windows, 1-2 Linux) TTS playback leaks into
    # the mic and can trigger our own wake-word. Three layers:
    #   - (a) half-duplex gate: wake-word inference only runs in IDLE;
    #         barge-in requires 5 sustained frames. Structural, always on.
    #   - (b) mic ducking: during TTS playback apply attenuation to the
    #         mic stream before it reaches VAD; released within
    #         ``self_feedback_duck_release_ms`` of TTS-end.
    #   - (c) spectral self-cancel: deferred to Sprint 4.
    # ``gate+duck`` is the default because (a) alone cannot stop a
    # loud TTS from tripping the VAD threshold repeatedly.
    self_feedback_isolation_mode: Literal["off", "gate-only", "gate+duck"] = "gate+duck"
    # Attenuation applied to the mic stream while TTS is playing. Must
    # be <= 0 (the normalizer stage is an attenuator, never an
    # amplifier). -18 dB is the ADR-recommended default — aggressive
    # enough to suppress leak, gentle enough that a genuine barge-in
    # spike (≥ +6 dB above leak level) still trips VAD.
    self_feedback_duck_gain_db: float = -18.0
    # Soft-release window for the duck. The FrameNormalizer ramps gain
    # back to unity over roughly this duration after TTS-end to avoid
    # a pop at the step edge. Tracks ``ducking_ramp_ms`` downstream.
    self_feedback_duck_release_ms: float = 50.0

    # §4.4.7 kernel-invalidated endpoint quarantine — see
    # :class:`~sovyx.voice.health._quarantine.EndpointQuarantine`.
    # When a probe returns :attr:`~sovyx.voice.health.contract.Diagnosis.KERNEL_INVALIDATED`
    # (USB-stack-timeout-driven IAudioClient invalidated state, Windows
    # LiveKernelEvent 0x1cc class), no user-mode cure exists. The cascade
    # short-circuits, adds the endpoint to an in-memory quarantine, and
    # :mod:`sovyx.voice.health._factory_integration` falls over to the
    # next viable :class:`DeviceEntry`. Quarantine clears on hot-plug
    # remove+readd or after ``kernel_invalidated_recheck_interval_s`` so
    # a reboot-cured endpoint is retried without operator action.
    kernel_invalidated_failover_enabled: bool = True
    kernel_invalidated_quarantine_s: float = 3_600.0  # 1 h wall clock
    kernel_invalidated_recheck_interval_s: float = 300.0  # 5 min

    # Windows-only driver-watchdog pre-flight (v0.20.4, see
    # :mod:`sovyx.voice.health._driver_watchdog_win`). When the
    # Kernel-PnP Driver Watchdog (event IDs 900/901) has fired for the
    # target mic's hardware ID in the last ``driver_watchdog_lookback_hours``,
    # the cascade is downgraded to shared-mode for this boot so an
    # exclusive-init IOCTL can never wedge the already-fragile driver
    # into a kernel-resource hard-reset (Razer BlackShark V2 Pro
    # 2026-04-20 post-mortem). The subprocess scan is bounded by
    # ``driver_watchdog_scan_timeout_s`` so voice-enable never blocks
    # past this on an unresponsive PowerShell host.
    driver_watchdog_lookback_hours: int = 24
    driver_watchdog_scan_timeout_s: float = 3.0

    # ── APO integrity + bypass strategy framework (Phase 1 — OS-agnostic).
    # Background: on Windows 11 25H2 the Microsoft "Voice Clarity" APO
    # (VocaEffectPack, CLSID {96BEDF2C-18CB-4A15-B821-5E95ED0FEA61})
    # is injected into every USB mic's shared-mode capture chain and
    # destroys the spectral envelope Silero VAD relies on while
    # preserving human audibility (RMS healthy, VAD ≈ 0). The durable
    # fix routes around the APO chain via a per-platform bypass
    # strategy; detection itself is OS-agnostic and lives in
    # :class:`~sovyx.voice.health.capture_integrity.CaptureIntegrityProbe`.

    # Apply the PortAudio-group endpoint matcher fallback in
    # :mod:`sovyx.voice._apo_detector` so MME-truncated device names
    # ("Microfone (Razer BlackShark V2 ") correlate to their WASAPI
    # sibling's full name ("Razer BlackShark V2 Pro"). Off reverts to
    # the strict-name-match-only matcher.
    apo_detector_use_portaudio_group_hint: bool = True

    # CaptureIntegrityProbe — warm probe that reads a snapshot of the
    # live capture task's ring buffer (never opens a second stream) and
    # classifies the signal by RMS + VAD max prob + spectral flatness
    # + spectral 85%-roll-off. ``duration_s`` is the tap window size;
    # the thresholds below define the APO_DEGRADED branch.
    integrity_probe_duration_s: float = 3.0
    # Wiener entropy above which a VAD-dead, RMS-alive signal is
    # classified APO_DEGRADED. Empirical baseline: clean speech ≈
    # 0.10–0.15, Voice-Clarity-destroyed speech ≈ 0.28–0.35.
    integrity_spectral_flatness_apo_ceiling: float = 0.25
    # 85 %-energy spectral roll-off ceiling (Hz). Voice Clarity's
    # aggressive low-pass under mono resample drops this below 4 kHz;
    # clean speech sits at 6–8 kHz for the same VAD-dead pattern.
    integrity_spectral_rolloff_apo_ceiling_hz: int = 4_000
    # Peak SileroVAD probability that counts as "VAD responsive". At or
    # above this, the integrity probe short-circuits to HEALTHY — the
    # downstream orchestrator already passed through
    # :attr:`VADConfig.onset_threshold`, so the probe's floor is
    # intentionally lower than onset to avoid a race between a live VAD
    # event and an in-flight integrity snapshot.
    integrity_vad_healthy_max_prob_floor: float = 0.30
    # Peak SileroVAD probability that counts as "VAD destroyed". Below
    # this, and with RMS above :attr:`integrity_apo_rms_floor_db`, the
    # probe advances to the spectral-envelope check to distinguish
    # APO_DEGRADED from genuine VAD_MUTE. Empirical Voice-Clarity
    # baseline: 0.001–0.01 on a sustained hum that clean WASAPI returns
    # as 0.3–0.9.
    integrity_vad_dead_max_prob_ceiling: float = 0.05
    # RMS ceiling (dBFS) below which the signal is classified
    # DRIVER_SILENT instead of APO_DEGRADED. The driver is not
    # delivering audio — a different remediation path (reopen /
    # re-enumerate) applies. Sits above the ``-96 dBFS`` int16 noise
    # floor so a privacy-muted mic or a mid-reopen window does not get
    # misread as an APO attack.
    integrity_driver_silent_rms_ceiling_db: float = -80.0
    # RMS floor (dBFS) below which APO_DEGRADED is rejected. The APO
    # attack requires an audible carrier — a user not speaking can't
    # exhibit it. Clean speech at conversational loudness typically
    # sits at -30 to -15 dBFS; -50 is the quietest still-voiced signal
    # we'd expect in practice.
    integrity_apo_rms_floor_db: float = -50.0

    # APO_DEGRADED quarantine — mirrors the KERNEL_INVALIDATED
    # precedent. When a strategy iterator exhausts without restoring
    # integrity, the endpoint is quarantined and the factory fails
    # over to an alternative. Periodic re-probes clear the quarantine
    # if an OS update or driver re-install retires the APO.
    apo_quarantine_enabled: bool = True
    apo_quarantine_s: float = 3_600.0
    apo_quarantine_recheck_interval_s: float = 300.0

    # T6.13 — DEGRADED state periodic re-probe. When the watchdog's
    # backoff schedule exhausts without recovery the state lands on
    # WatchdogState.DEGRADED and stays there until a hot-plug add
    # arrives (§4.4.2). On environments where the cause is transient
    # OS pressure (CPU saturation, driver paging, brief WASAPI service
    # blip) the device may recover spontaneously without any user
    # action. The DEGRADED loop runs a low-priority background
    # re-cascade every ``watchdog_degraded_reprobe_interval_s``
    # seconds (default 5 min) so the pipeline self-heals without
    # waiting for replug. Set to 0 to disable; the apo_recheck loop
    # already handles the APO-quarantine subset, this complements
    # it by covering the broader DEGRADED state (max-attempts
    # exhausted with non-APO root cause).
    watchdog_degraded_reprobe_interval_s: float = 300.0
    """T6.13 — interval between background re-cascades while the watchdog
    is in :attr:`WatchdogState.DEGRADED`. Default 5 min. Disabled with 0."""

    # T6.16 — post-apply INCONCLUSIVE retry. The
    # ``CaptureIntegrityCoordinator`` post-apply probe can return
    # INCONCLUSIVE on transient causes (tap timed out short before
    # ``_MIN_SAMPLES_FOR_ANALYSIS``, user silent during the apply
    # settle window, brief tap exception). Pre-T6.16 behaviour treated
    # INCONCLUSIVE as APPLIED_STILL_DEAD → reverted strategies that
    # may have actually worked. The retry path captures a FRESH mark
    # after the first inconclusive tap and re-probes ONCE; if the
    # retry yields a definitive verdict (HEALTHY / VAD_MUTE+improvement
    # / APO_DEGRADED / DRIVER_SILENT), it drives the rest of the path.
    # If retry also returns INCONCLUSIVE, the coordinator falls through
    # to the canonical APPLIED_STILL_DEAD + revert flow (conservative
    # default — same as pre-T6.16 behaviour). The retry's bounded cost
    # (one extra probe ≈ probe_duration_s + jitter_margin_s, typically
    # 1-2 s) is negligible vs the false-revert cost of dropping a
    # working strategy.
    capture_integrity_inconclusive_retry_enabled: bool = True
    """T6.16 — single-retry on post-apply INCONCLUSIVE verdict in the
    bypass coordinator. ``True`` (default) is strictly safer than
    pre-T6.16 behaviour: recovers transient inconclusives (user silent
    / tap-window timeout) without changing the deterministic STILL_DEAD
    path. Operators wanting to restore pre-T6.16 single-probe behaviour
    set ``SOVYX_TUNING__VOICE__CAPTURE_INTEGRITY_INCONCLUSIVE_RETRY_ENABLED=false``.
    The retry decision is documented architecturally — flipping the
    flag should be the rare exception, not a routine config change.
    """

    probe_stream_open_timeout_threshold_ms: int = Field(default=5_000, ge=500, le=60_000)
    """T6.2 — threshold above which a probe with ``callbacks_fired == 0``
    is classified as :attr:`Diagnosis.STREAM_OPEN_TIMEOUT` instead of
    :attr:`Diagnosis.NO_SIGNAL`.

    Distinguishes two failure modes that look identical at the
    callback layer:

    * **NO_SIGNAL** — probe duration shorter than the threshold (e.g.
      1.5 s default cold probe): the absence of callbacks isn't
      conclusive evidence of a wedged stream; the probe just didn't
      wait long enough. Default cascade behaviour treats it as
      transient and proceeds.
    * **STREAM_OPEN_TIMEOUT** — probe duration ≥ threshold AND zero
      callbacks: the driver accepted the open + start but never
      delivered audio. Symptoms: USB resource timeout, IAudioClient
      stuck mid-init, kernel-side wedge that doesn't surface as a
      PortAudio error. Cure is physical (replug / reboot) — same
      class as :attr:`Diagnosis.KERNEL_INVALIDATED` but observed
      via the callback-not-fired surface.

    Bound ``[500, 60_000]`` ms — at 500 ms the threshold is
    aggressive (false-positives common for short cold probes); at
    60 s the threshold is permissive (genuinely-wedged drivers
    might still escape detection). 5 s default per master mission
    spec — matches typical USB driver settle plus headroom.
    """

    # T6.17 — quarantine ping-pong detection. When the same endpoint is
    # re-added to quarantine ``threshold`` times within ``window_s``,
    # the layer emits ``voice_quarantine_re_quarantine_event`` so
    # operators can identify endpoints whose underlying condition is
    # fundamentally unrecoverable (driver bug, hardware fault). Pure
    # observability — does not gate further quarantine adds.
    quarantine_pingpong_threshold: int = 3
    """T6.17 — re-quarantine count within
    :attr:`quarantine_pingpong_window_s` that triggers the
    ``voice_quarantine_re_quarantine_event`` emission. Set to a
    large value (e.g. 10_000) to effectively disable the signal
    without removing the wire-up; set to 1 to fire on every add
    (debug only — high log volume)."""

    quarantine_pingpong_window_s: float = 300.0
    """T6.17 — rolling window for ping-pong detection. 5 min default
    matches typical user iteration ("plug, fail, replug, fail,
    replug, fail") without triggering on overnight quarantine
    cycles. Tune up for slow-burn detection, down for tighter
    bursts."""

    # T6.18 — TTL-expiry rapid re-quarantine warning. When an entry
    # whose TTL just expired (or expired within the last
    # ``rapid_requarantine_window_s``) is re-added, the layer emits
    # ``voice_endpoint_repeatedly_failing`` — the signal that the
    # quarantine TTL is set too short for actual recovery OR the
    # underlying condition recurs deterministically.
    quarantine_rapid_requarantine_window_s: float = 60.0
    """T6.18 — window after a TTL-expiry-triggered purge during
    which a re-add fires the rapid-requarantine warning. 60 s
    default matches the master mission spec ("within 1 minute of
    expiry"). Pure observability."""

    # CaptureIntegrityCoordinator — iterates registered platform
    # strategies in ``cost_rank`` order. Hard cap per session guards
    # against pathological oscillation (strategy A applies, reverts,
    # strategy B applies, reverts, ad infinitum). The post-apply
    # settle window is the driver-side debounce before re-probing.
    bypass_strategy_max_attempts: int = 3
    # v1.3 §14.E3 — post-apply settle window. Was 1.5 s in v0.21.2 and
    # earlier, which violated the invariant ``settle >= integrity_probe_duration_s``
    # (both consumed by :meth:`CaptureIntegrityCoordinator.handle_deaf_signal`):
    # the probe's 3.0 s ring-buffer tap window reached back further in
    # time than the settle window advanced forward, so the post-apply
    # probe contained pre-apply frames and classified the (now-fixed)
    # signal as still APO_DEGRADED. 3.2 s = ``integrity_probe_duration_s``
    # (3.0) plus a 200 ms driver-side settle margin — the same margin
    # Windows WASAPI APO teardown has historically needed. Paired with
    # the L4-A validator below, which enforces the invariant even when
    # an operator overrides one knob without the other.
    bypass_strategy_post_apply_settle_s: float = 3.2
    # v1.3 §14.E1 — jitter margin added to ``integrity_probe_duration_s``
    # when computing ``tap_frames_since_mark``'s ``max_wait_s``. The
    # mark-based tap returns as soon as ``min_samples`` accumulate, so
    # this margin only kicks in when the event loop is under scheduler
    # pressure; 0.5 s covers p99 of observed jitter without bloating
    # the bypass-attempt budget.
    probe_jitter_margin_s: float = 0.5
    # v1.3 §14.E2 — improvement heuristic factor. When the post-apply
    # probe returns ``VAD_MUTE`` (user stopped speaking during settle)
    # AND ``before.spectral_rolloff_hz * improvement_rolloff_factor``
    # exceeds ``after.spectral_rolloff_hz``, the coordinator treats the
    # attempt as resolved instead of reverting — the fix demonstrably
    # improved the spectrum even if the VAD did not re-fire. 5.0x is
    # large enough to be unambiguous (clean speech → clipped speech
    # rolloff ratio is typically 20-40x) while rejecting noise-floor
    # shifts that do not represent real spectral recovery.
    improvement_rolloff_factor: float = 5.0

    # Phase 4 hardware-fingerprint catalog placeholders. Disabled in
    # Phase 1; surfaced here so the tuning contract is stable across
    # phases. Confidence gate promotes a strategy to "first pick" only
    # once its rolling EWMA success rate crosses the floor.
    bypass_fingerprint_catalog_enabled: bool = False
    bypass_fingerprint_min_confidence: float = 0.7

    # ── Linux ALSA bypass strategies (Phase 3) ─────────────────────────
    # See docs-internal/plans/linux-alsa-mixer-saturation-fix.md for the
    # full derivation. Covers two orthogonal Linux failure modes that
    # both surface as ``IntegrityVerdict.APO_DEGRADED``: analog mixer
    # saturation (Internal Mic Boost / Capture driven above the ADC's
    # safe range) and session-manager DSP (PulseAudio
    # ``module-echo-cancel`` / PipeWire filter-chain).
    #
    # Strategy #1 — ``LinuxALSAMixerResetBypass`` (linux.alsa_mixer_reset).
    # Reads the card's boost + capture controls via ``amixer``, snapshots
    # them, and reduces the over-driven ones to a safe fraction of max.
    # Fully reverted on teardown — the pipeline never leaves the mixer
    # in an unknown state. Enabled by default because the mutation is
    # 100 % reversible and the subprocess cost is ~50 ms.
    linux_alsa_mixer_reset_enabled: bool = True
    # Fraction of ``max_raw`` to set Boost-class controls to on apply.
    # ``0.0`` = zero out, which is almost always the right answer for
    # laptop-internal mics: a +36 dB ``Internal Mic Boost`` is never
    # correct for a normal-distance speaker — it was shipped at max by
    # default to save Skype calls in the Windows XP era, and the
    # default never changed.
    #
    # **Deprecated since v0.23.0** (Mission §9.1.1, Gap 1) — scheduled
    # for removal in v0.27.0 (Phase 4 — AEC + audio quality), bumped
    # from v0.24.0 per T1.51 because the bypass-coordinator wire-up
    # gating Phase 2 + 3 must land first. Until then this fraction
    # continues to drive the legacy ``LinuxALSAMixerResetBypass``
    # band-aid; a non-default value surfaces a one-time WARN at boot
    # via :func:`sovyx.engine.config.warn_on_deprecated_mixer_overrides`.
    linux_mixer_boost_reset_fraction: float = 0.0
    # Fraction of ``max_raw`` to set Capture-class controls to on apply.
    # ``0.5`` ≈ 0 dB for most codecs with the 0..80 / -40..+30 dB range
    # observed on HDA Intel / Realtek / SN6180 parts. Never ``0.0`` —
    # that would mute the mic and a subsequent probe would classify the
    # endpoint as DRIVER_SILENT rather than HEALTHY.
    #
    # **Deprecated since v0.23.0** (Mission §9.1.1, Gap 1). See
    # ``linux_mixer_boost_reset_fraction`` for the deprecation rationale.
    linux_mixer_capture_reset_fraction: float = 0.5
    # Saturation-detection threshold — a control is at saturation risk
    # when ``current_raw > max_raw * ratio`` AND the control name matches
    # one of the boost / capture patterns. ``0.5`` catches the VAIO case
    # (3/3 = 1.0, 80/80 = 1.0) without false-positiving on reasonable
    # defaults such as ``Capture = 40/80`` (= 0.5 is borderline but not
    # flagged). See ``_linux_mixer_probe._BOOST_PATTERNS``.
    linux_mixer_saturation_ratio_ceiling: float = 0.5
    # Aggregated boost-chain dB above which the card is flagged even
    # when no single control crosses the ratio ceiling. Catches
    # multi-boost cases (``Internal Mic Boost`` + ``Front Mic Boost`` +
    # ``Capture`` each at 60 %, individually under the ratio gate but
    # summing to clipping territory).
    linux_mixer_aggregated_boost_db_ceiling: float = 18.0
    # Fraction targets for the ATTENUATION fix path (`sovyx doctor voice
    # --fix` on attenuated cards — capture+boost both well below VAD
    # operating range). Distinct from `*_reset_fraction` which REDUCES
    # gain on saturated cards.
    #
    # Defaults are deliberately AT the saturation_ratio_ceiling (0.5)
    # for capture and BELOW it for boost — the saturation check uses
    # strict ``>`` so a control at exactly 0.5 ratio does NOT trigger
    # saturation_risk. This keeps the apply path strictly inside the
    # "neither attenuated nor saturated" window and prevents the
    # oscillation observed on the second pilot run (v0.22.3): defaults
    # of 0.75/0.66 lifted attenuated controls past the saturation
    # ceiling, flipping the regime — fix detected attenuation, applied
    # boost, re-probe now reports saturation, deaf-detection still
    # triggered with a CLIPPED signal instead of a SILENT one.
    #
    # Pilot evidence (VAIO VJFE69F11X-B0221H, SN6180 codec):
    # Mic Boost 0/3, Capture 40/80, Internal Mic Boost 1/3 → aggregated
    # -22 dB, Silero max_prob ~ 0. With current defaults:
    # Capture 40 → 40 (at 0.5 ratio = no-op); Mic Boost 0 → 1 (1/3 =
    # +12 dB); Internal Mic Boost 1 → 1 (no-op). Aggregated swing:
    # -22 dB → -10 dB. Above VAD floor, well below saturation ceiling.
    #
    # **Deprecated since v0.23.0** (Mission §9.1.1, Gap 1) — scheduled
    # for removal in v0.27.0 (Phase 4 — AEC + audio quality), bumped
    # from v0.24.0 per T1.51. The attenuation regime is increasingly
    # handled by the in-process AGC2 closed-loop digital gain
    # (Layer 4 of the Linux mixer cascade), which recovers below-VAD-
    # floor signals without mutating the OS-level mixer. The KB
    # preset cascade (Layer 3) covers card-specific overrides for
    # codecs where AGC2 alone is insufficient. A non-default value
    # surfaces a one-time WARN at boot via
    # :func:`sovyx.engine.config.warn_on_deprecated_mixer_overrides`.
    linux_mixer_capture_attenuation_fix_fraction: float = 0.5
    linux_mixer_boost_attenuation_fix_fraction: float = 0.33
    # Strategy #2 — ``LinuxPipeWireDirectBypass`` (linux.pipewire_direct).
    # Rebinds the capture stream to the raw ALSA ``hw:X,Y`` node,
    # bypassing PipeWire / PulseAudio session-manager DSP entirely.
    # Disabled by default: the rebind requires a full stream reopen and
    # on some distros the raw hw: path introduces audible latency
    # spikes. Enable per-stack once validated.
    linux_pipewire_direct_bypass_enabled: bool = False
    # Hard wall-clock cap for each ``amixer`` / ``alsactl`` / ``pactl``
    # invocation originating from the Linux bypass strategies. Mirrors
    # ``_apo_detector_linux._SUBPROCESS_TIMEOUT_S`` — never block the
    # event loop behind a frozen audio tool.
    linux_mixer_subprocess_timeout_s: float = 2.0

    # L2.5 mixer-sanity user-customization thresholds (ADR-voice-mixer-
    # sanity-l2.5-bidirectional §4.I4, V2 Master Plan Part E.5). The
    # 6-signal customization heuristic emits a score in [0, 1]; below
    # the lower threshold L2.5 auto-applies, between the two it defers
    # (dashboard card), above the upper it skips silently to honour the
    # user's intentional tuning (invariant I4 — user customization is
    # sacred).
    linux_mixer_user_customization_threshold_apply: float = 0.5
    linux_mixer_user_customization_threshold_skip: float = 0.75
    # L2.5 KB match threshold — overrides per-profile match_threshold
    # when the caller does not pass ``min_score`` to
    # :meth:`MixerKBLookup.match`. Default per V2 Master Plan §F.2.
    linux_mixer_sanity_kb_match_threshold: float = 0.6
    # Total wall-clock cap for one L2.5 invocation — if the orchestrator
    # exceeds this, it rolls back any in-flight apply and returns
    # ``MixerSanityDecision.ERROR``. V2 Master Plan §C.3.
    linux_mixer_sanity_budget_s: float = 5.0

    # AudioCaptureTask ring buffer — bounded snapshot of the most
    # recent frames delivered by PortAudio. Fed by the capture
    # callback, consumed by :meth:`AudioCaptureTask.tap_recent_frames`
    # so the integrity probe can re-analyse the live signal without
    # opening a second stream (critical on Windows: a concurrent open
    # on an exclusive-held endpoint raises AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED).
    # Sized for ``integrity_probe_duration_s`` + watchdog recheck
    # window. 33 s at 16 kHz mono int16 ≈ 1 MB — bounded, acceptable.
    capture_ring_buffer_seconds: float = 33.0
    # v1.3 §14.E4 — poll interval for
    # :meth:`AudioCaptureTask.tap_frames_since_mark`. The tap spins in
    # a ``while True: check / sleep`` loop so the coordinator can resume
    # as soon as ``min_samples`` post-apply frames accumulate. 50 ms
    # sits comfortably above event-loop jitter (<10 ms on modern hosts)
    # and below the 160 ms PortAudio callback cadence at 16 kHz / 2560
    # samples, so one poll tick per callback is the worst case. Exposed
    # as a tuning knob rather than a module constant because §14
    # mandates every empirical value carry an env override for live
    # adjustment without a rebuild.
    mark_tap_poll_interval_s: float = 0.05

    # Voice device test (setup-wizard meters + TTS test button).
    # Kill-switch + ballistics + rate limiting for the test endpoints.
    device_test_enabled: bool = True
    device_test_frame_rate_hz: int = 30  # WS level frames per second
    device_test_peak_hold_ms: int = 1_500  # peak marker hold duration
    device_test_peak_decay_db_per_sec: float = 20.0  # decay after hold
    device_test_vad_trigger_db: float = -30.0  # shown as marker on meter
    device_test_clipping_db: float = -0.3  # clipping flag threshold
    device_test_reconnect_limit_per_min: int = 10  # per-token budget
    device_test_max_sessions_per_token: int = 1  # singleton per user
    device_test_max_phrase_chars: int = 200  # TTS test phrase cap
    device_test_output_job_ttl_seconds: int = 60  # job cleanup
    # v0.20.2 / Bug B — session lifecycle hard caps. Browser tabs that
    # freeze, get minimised, or have a dead WS peer cannot hold the mic
    # indefinitely; these caps guarantee the capture endpoint is released
    # for the production voice pipeline within a bounded time.
    device_test_max_lifetime_s: float = 300.0  # 5 min absolute session cap
    device_test_peer_alive_timeout_s: float = 10.0  # no-send watchdog
    # stop→close grace window. The SessionRegistry.close_all() pre-enable
    # hook waits this long for each test session to release its PortAudio
    # stream before force-closing. 2 s covers worst-case ALSA PCM drain
    # (~500 ms) plus a 500 ms safety margin; longer would risk the
    # dashboard HTTP timeout (30 s default in the React client) when the
    # user juggles many meter sessions before hitting "Enable voice".
    # See ``voice-linux-cascade-root-fix`` T8 for the handoff contract.
    device_test_force_close_grace_s: float = 2.0

    # ── Voice Windows Paranoid Mission (v0.24.0 → v0.26.0) feature flags.
    # See docs-internal/missions/MISSION-voice-windows-paranoid-2026-04-26.md.
    #
    # Staged adoption: foundation lands in v0.24.0 with every flag default
    # **False** (lenient / disabled). Wire-up in v0.25.0 flips
    # ``probe_cold_strict_validation_enabled`` + ``cascade_host_api_alignment_enabled``;
    # behaviour knobs (``bypass_tier1_raw_enabled``,
    # ``bypass_tier2_host_api_rotate_enabled``, ``mm_notification_listener_enabled``)
    # stay opt-in for early-adopter pilots. v0.26.0 default-flips the
    # remaining three after promotion-gate telemetry validates the wire-up.
    #
    # Master kill switch lives at ``voice_clarity_autofix`` above — do not
    # add a parallel master here (anti-pattern #12: one understood layer
    # beats three mysterious ones).
    probe_cold_strict_validation_enabled: bool = False
    """Furo W-1 — cold-probe stricter signal validation.

    When ``False`` (legacy v0.23.x behaviour) ``_diagnose_cold`` returns
    ``Diagnosis.HEALTHY`` for any combo whose audio callback fires at
    least once, regardless of RMS — which is exactly what lets a
    Microsoft Voice Clarity APO destroy the signal upstream of PortAudio
    yet have the silent combo persist as the winning ComboStore entry,
    replicating the failure deterministically on every boot.

    When ``True``, silent cold probes (``rms_db < probe_rms_db_no_signal``,
    default −70 dBFS) return ``Diagnosis.NO_SIGNAL`` so the cascade
    advances to the next combo and the silent winner never persists.

    Lenient mode (``False``) still emits structured
    ``voice.probe.cold_silence_rejected{mode=lenient_passthrough}``
    events so operators can calibrate the rejection rate before
    enabling. Default-flip planned for v0.25.0."""

    bypass_tier1_raw_enabled: bool = False
    """Furo W-2 Tier 1 — RAW + Communications bypass via
    ``IAudioClient3::SetClientProperties`` (Windows only).

    Cheapest bypass strategy: no exclusive lock, no admin, single
    sub-millisecond COM call. Bypasses the MFX / SFX APO layers via the
    MMDevice property surface, which is orthogonal to the PortAudio
    host-API wrapper — so it covers MME / DirectSound / WDM-KS /
    WASAPI-shared endpoints uniformly when the device reports
    ``RawProcessingSupported=true``.

    Default-flip planned for v0.26.0; opt-in flag for early-adopter
    pilots in v0.25.0. See spec §D2 / Part 1 of mission doc."""

    bypass_tier2_host_api_rotate_enabled: bool = False
    """Furo W-2 Tier 2 — host-API rotate-then-exclusive bypass
    (Windows only).

    For endpoints whose runtime host_api is MME / DirectSound / WDM-KS
    the strategy rotates the capture stream to ``Windows WASAPI`` then
    engages exclusive mode, which bypasses every APO layer
    (MFX / SFX / EFX) on the capture pipeline.

    Requires ``cascade_host_api_alignment_enabled=True`` — the
    cross-validator below rejects the contradictory configuration at
    boot because the rotate path mutates ``self._host_api_name`` and
    relies on the opener honouring it on subsequent device-error
    reopens. Default-flip planned for v0.26.0."""

    mm_notification_listener_enabled: bool = False
    """IMMNotificationClient device-change listener (Windows only).

    When enabled the capture task registers an
    ``IMMNotificationClient`` via
    ``RegisterEndpointNotificationCallback`` so the pipeline auto-
    recovers on default-device changes (USB hot-plug, Sound Settings
    panel flip) without operator intervention.

    The COM callback contract is **non-blocking** (anti-pattern #29
    enforced by ``tools/lint_imm_callbacks.py``): every event posts
    onto the asyncio loop via ``call_soon_threadsafe`` and returns
    ``S_OK`` within ~5 ms. Blocking inside the callback would deadlock
    the entire Windows audio service.

    .. note::

       As of 2026-04-30 this flag is defined but NOT load-bearing —
       no production code reads it to register the listener. The
       runtime wire-up lands via
       ``MISSION-voice-runtime-listener-wireup-2026-04-30.md``
       Phase 2. Until that ships, flipping the flag has no effect.
       Phase 3 Gate 4 (operator backlog) is BLOCKED on the wire-up
       landing first.

    Default-flip planned for v0.26.0. See spec §D5 / Part 3 of mission
    doc."""

    audio_driver_update_listener_enabled: bool = False
    """WMI subscription for Windows audio driver updates (Windows only).

    When enabled the voice pipeline registers a
    ``WindowsDriverUpdateListener`` (foundation shipped in T5.49 /
    `fb815a3`) that subscribes to WMI
    ``__InstanceModificationEvent`` filtered to the audio device
    class GUID. The sink callback emits structured
    ``voice.driver_update.detected`` events when Windows updates a
    driver mid-session — letting operators correlate deaf-signal
    incidents with regressed driver releases instead of blaming
    Sovyx's bypass logic.

    Foundation default is ``False`` per
    ``feedback_staged_adoption`` — operators pilot the listener +
    confirm WMI events fire correctly before the default flips to
    ``True``. The detection is observability-only at the listener
    layer; actual re-cascade decisions go through
    :attr:`audio_driver_update_recascade_enabled` which is gated
    independently.

    Default-flip planned post-pilot once
    ``MISSION-voice-runtime-listener-wireup-2026-04-30.md`` Phase 1
    closes + telemetry confirms WMI events fire on real driver
    updates without false positives."""

    voice_mixer_kb_user_profiles_enabled: bool = False
    """Load operator-contributed mixer KB profiles from ``data_dir/mixer_kb/user/``.

    T5.39 wire-up. The :class:`MixerKBLookup` foundation already
    exposes :meth:`load_shipped_and_user`, but pre-wire-up production
    only called :meth:`load_shipped` — operators who dropped custom
    YAML profiles in ``~/.sovyx/mixer_kb/user/`` were silently
    ignored. With this flag True, the L2.5 mixer-sanity factory
    routes through the user-aware loader: shipped profiles + user
    profiles are merged with the existing dedupe rule
    (user-contributed profile_id wins over shipped same-id) and
    user-contributed matches surface with the
    ``MixerKBMatch.is_user_contributed=True`` provenance flag for
    dashboard / telemetry partitioning.

    Foundation default ``False`` per ``feedback_staged_adoption`` —
    operators opt in once they've authored their first profile or
    pulled from a community KB drop. Pre-flip pilot validates:

    * The user directory is read once at boot (not per cascade).
    * Malformed user YAML is skipped with a structured WARN, not a
      crash.
    * ``MixerKBMatch.is_user_contributed`` propagates to the
      observability surface so accidental priority inversions
      (community profile silently winning over a shipped tested
      profile) are visible.

    Default flip planned post-pilot once telemetry shows non-zero
    user-profile load rate without false-positive cascade
    selections. Linux-only behaviour per L2.5 scope; non-Linux
    platforms ignore the flag.
    """

    combo_store_usb_fingerprint_enabled: bool = False
    """Persist + match :class:`ComboStore` entries by stable USB fingerprint.

    T5.43 + T5.51 wire-up. When enabled the voice factory injects a
    cross-platform resolver into :class:`ComboStore` that maps
    ``endpoint_guid → "usb-VVVV:PPPP[-SERIAL]"`` (Windows via
    :mod:`sovyx.voice.health._endpoint_fingerprint_win` IPropertyStore +
    PKEY_Device_InstanceId; Linux via parsing of the
    ``{linux-usb-VVVV:PPPP-...}`` synthetic guid shape). Side effects:

    * :meth:`ComboStore.record_winning` resolves and persists the
      fingerprint on every newly written entry.
    * :meth:`ComboStore.get` falls back to a fingerprint scan when
      the primary endpoint_guid lookup misses — recovering the
      validated combo across port changes / firmware updates that
      mint a new endpoint_guid for the SAME physical USB device.

    Foundation default is ``False`` per ``feedback_staged_adoption``
    — pre-wire-up entries lack the fingerprint field (back-compat),
    and operators pilot the resolver before defaulting on. The
    fallback-scan log line ``voice.combo_store.usb_fingerprint_recovery_hit``
    surfaces real-world hit rate so the default flip to ``True`` can
    be telemetry-driven.

    Non-USB endpoints (PCI codecs, virtual loopback, Bluetooth A2DP)
    return ``None`` from the resolver and are persisted without a
    fingerprint — the existing endpoint_guid-only lookup is the
    durable contract for those classes.
    """

    audio_driver_update_recascade_enabled: bool = False
    """Trigger graceful re-cascade on detected driver updates (Windows only).

    Gated independently from
    :attr:`audio_driver_update_listener_enabled` per
    ``feedback_staged_adoption`` — detection (the listener +
    structured event emission) is observability and ships first;
    the action (re-cascade on driver swap) is destructive + needs
    its own pilot before defaulting on.

    When the listener flag is True AND this flag is False (lenient
    mode), the handler emits
    ``voice.driver_update.recascade_skipped{reason=flag_disabled}``
    so operators can verify detection without triggering the
    re-cascade path. When this flag is True, the handler emits
    ``voice.driver_update.recascade_would_trigger`` and (in a
    future commit per mission §Part 4.1) invokes the orchestrator's
    actual cascade re-run logic.

    Foundation default is ``False``. Default-flip planned post-
    pilot once detection telemetry confirms low false-positive rate
    + cascade re-run plumbing lands in a future mission."""

    voice_aec_enabled: bool = False
    """Acoustic Echo Cancellation (Phase 4 / T4.1 — foundation).

    Master switch for the in-process AEC stage that lives in
    :mod:`sovyx.voice._aec`. When True the FrameNormalizer runs the
    selected ``voice_aec_engine`` over each incoming capture frame
    using the most recent render PCM (TTS playback) as the echo
    reference; when False AEC is bypassed entirely (NoOp processor).

    Foundation default is ``False`` per ``feedback_staged_adoption`` —
    new validators ship lenient. Operators flip to ``True`` after
    pilot validation confirms ERLE ≥ 25 dB on their hardware.
    Default-flip planned for v0.27.0 once promotion gates close
    (master mission §Phase 4 — AEC ERLE ≥ 30 dB sustained when
    render+capture both active)."""

    voice_aec_engine: Literal["off", "speex"] = "speex"
    """Concrete AEC implementation when ``voice_aec_enabled=True``.

    * ``"off"`` — explicit no-op (also reachable via
      ``voice_aec_enabled=False``); kept as a separate value so the
      operator can pin "AEC engine selected but disabled" semantics.
    * ``"speex"`` — Speex Echo Canceller via the ``pyaec`` package.
      NLMS-based MDF (Multi-Delay block Frequency adaptive filter).
      Ships as a single bundled DLL/SO; works on Windows / Linux /
      macOS without a build environment. Typical ERLE in production:
      20-25 dB. Below the Phase 4 promotion gate of 30 dB but
      sufficient for headphones + most desktop speakers; documented
      tradeoff in the master mission §Phase 4 ADR.

    Future engine ``"webrtc"`` (WebRTC AEC3 via custom ctypes shim)
    is planned for v0.28.0+ once Speex telemetry confirms the ERLE
    gap. The mission's preferred ``webrtc-audio-processing`` PyPI
    binding does not currently ship Windows wheels and fails to
    build from source under MSVC, so v0.27.0 ships with Speex while
    the WebRTC path is researched."""

    voice_aec_filter_length_ms: int = Field(default=128, ge=32, le=512)
    """AEC adaptive filter length in milliseconds.

    Speex AEC convention: filter_length should match the longest
    echo tail expected (room reverb + render-to-capture loop time).
    128 ms covers typical office speakers + headphones. Headphones
    have shorter tails (~32-64 ms); large open rooms with reflective
    surfaces may need 256-512 ms. The implementation rounds to the
    nearest power of two internally."""

    voice_double_talk_detection_enabled: bool = False
    """Double-talk detector (Phase 4 / T4.9 — foundation observability).

    When True the FrameNormalizer's AEC stage runs the
    :class:`~sovyx.voice._double_talk_detector.DoubleTalkDetector`
    on every processed window and emits
    :data:`sovyx.voice.aec.double_talk{state}` so operators can
    measure the rate of user-speaking-during-TTS in their
    deployment. The freeze-the-AEC-filter action is staged for a
    follow-up commit (Speex's ``pyaec`` doesn't expose adaptation
    control) — this foundation phase is observability-only.

    Default ``False`` per ``feedback_staged_adoption``. Operators
    flip after pilot validation confirms the NCC distribution
    matches their hardware (the threshold
    :attr:`voice_double_talk_ncc_threshold` may need tuning per
    deployment)."""

    voice_double_talk_ncc_threshold: float = Field(default=0.5, ge=-1.0, le=1.0)
    """NCC value below which double-talk is declared.

    Standard NLMS-divergence threshold from AEC literature. Tune
    higher (more permissive — fewer freezes) or lower (more
    aggressive). Bounded ``[-1.0, 1.0]`` per the NCC algebraic
    range — values outside reject at config validation."""

    voice_aec_auto_engage_on_exclusive: bool = False
    """Force in-process AEC on when WASAPI exclusive bypasses the
    OS AEC chain (Phase 4 / T4.6 — foundation).

    Background: ``capture_wasapi_exclusive=True`` (or runtime
    auto-fix engaging exclusive on Voice Clarity APO hardware —
    anti-pattern #21) bypasses every endpoint APO including the
    Windows-shipped AEC. Without an in-process echo canceller, TTS
    playback leaks into the capture stream and degrades both VAD
    accuracy and ASR substitution rate.

    When this flag is ``True`` and the boot-time detector observes
    the dangerous combo (``capture_wasapi_exclusive=True`` AND
    ``voice_aec_enabled=False``), the factory force-engages AEC
    with the configured ``voice_aec_engine`` (defaulting to
    ``"speex"`` if the operator pinned ``"off"``) and emits a
    structured ``voice.aec.bypass_combo_auto_engaged`` log so the
    override is auditable.

    Default ``False`` per ``feedback_staged_adoption`` —
    observability-only on day zero. The detector emits
    ``sovyx.voice.aec.bypass_combo{state}`` regardless of this
    flag's value, so operators can measure the dangerous-combo
    rate in their fleet before enabling auto-engage. Default-flip
    planned for v0.28.0 once Speex ERLE pilots close (master
    mission §Phase 4)."""

    voice_noise_suppression_enabled: bool = False
    """Noise suppression master switch (Phase 4 / T4.11 — foundation).

    When True the FrameNormalizer's NS stage runs the configured
    :attr:`voice_noise_suppression_engine` over every emitted
    capture window. Foundation default ``False`` per
    ``feedback_staged_adoption`` — operators flip after pilot
    validation confirms the SNR improvement on their hardware
    (the spectral-gating engine targets ~5-10 dB on stationary
    background noise).

    Default-flip planned for v0.27.0 once NS promotion gate
    closes (master mission §Phase 4 / T4.14)."""

    voice_noise_suppression_engine: Literal["off", "spectral_gating"] = "spectral_gating"
    """Concrete NS implementation when ``voice_noise_suppression_enabled=True``.

    * ``"off"`` — explicit no-op (also reachable via the master
      switch); kept for "engine selected but disabled" semantics.
    * ``"spectral_gating"`` — frequency-domain magnitude gate via
      pure NumPy + scipy. Zero new dependencies. Effective on
      stationary background noise (HVAC, fans), less effective on
      non-stationary noise (keyboard, traffic). Documented quality
      tradeoff vs RNNoise; upgrade path to a custom librnnoise
      ctypes shim is reserved for v0.28.0+ if production telemetry
      shows the SNR gap matters.

    Future ``"rnnoise"`` engine slot is intentionally absent — the
    enum will be widened only when the librnnoise shim ships."""

    voice_noise_suppression_floor_db: float = Field(default=-50.0, ge=-120.0, le=0.0)
    """Per-bin magnitude floor (dBFS) below which the spectral gate attenuates.

    Bins whose magnitude sits below this floor are multiplied by
    :attr:`voice_noise_suppression_attenuation_db`. Higher (closer
    to 0 dBFS) means more aggressive gating — louder bins still
    get attenuated. Lower (more negative) means more permissive —
    only the quietest bins get gated.

    Default -50 dBFS sits between typical room ambient noise
    (~-60 dBFS in a quiet office) and active speech (~-30 dBFS),
    so the gate naturally cuts the noise floor while preserving
    speech harmonics."""

    voice_noise_suppression_attenuation_db: float = Field(default=-20.0, ge=-60.0, le=0.0)
    """Attenuation applied to bins below the magnitude floor (dBFS).

    -20 dB = 10× quieter (linear gain 0.1). 0 dB = passthrough
    (no NS effect). Values outside ``[-60, 0]`` reject at config
    validation: above 0 would amplify noise; below -60 produces
    audible ringing on transient bins."""

    voice_snr_estimation_enabled: bool = False
    """Per-frame SNR estimator master switch (Phase 4 / T4.31).

    When True the FrameNormalizer's SNR stage tracks a sliding-
    window minimum of frame mean-square power as the noise floor
    estimate and emits per-VAD-positive-frame
    :data:`voice.audio.snr_db` histogram samples (T4.33 wire-up).

    Default ``False`` per ``feedback_staged_adoption`` — operators
    flip after pilot validation confirms the noise-window length
    matches their environment (see
    :attr:`voice_snr_noise_window_seconds`)."""

    voice_snr_noise_window_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    """Sliding-window length for the noise-floor minimum tracker.

    The estimator assumes that within any window of this length
    at least one frame is genuine background silence — typical
    speech has 200-500 ms inter-utterance gaps, so 5 s comfortably
    captures multiple silence opportunities. Shorter windows
    (e.g. 1 s) react faster to room changes but risk anchoring
    to a continuously-active speaker; longer windows (30 s+) are
    stale during room transitions (operator walks into a quieter
    room). Bounded ``[0.5, 60.0]`` s."""

    voice_agc2_adaptive_floor_enabled: bool = False
    """AGC2 adaptive noise-floor master switch (Phase 4 / T4.51).

    When True the AGC2 silence gate uses the first-quartile of
    frame RMS over the last
    :attr:`voice_agc2_adaptive_floor_window_seconds` instead of
    the fixed ``silence_floor_dbfs`` (-60 dBFS default). Adapts
    the gate to the actual room noise floor — tighter in quiet
    rooms (less missed transients) and looser in noisy
    environments (less false-positive gating of background talk).

    Default ``False`` per ``feedback_staged_adoption``. Pre-T4.51
    fixed-floor behaviour preserved bit-exactly when off."""

    voice_agc2_adaptive_floor_window_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    """Sliding-window length for the AGC2 first-quartile noise tracker.

    Master mission §Phase 4 / T4.51 default 10 s. Shorter windows
    react faster to room changes (operator walks rooms) but risk
    anchoring to a continuously-active speaker; longer windows
    are stale during transitions. Bounded ``[1.0, 60.0]`` s."""

    voice_dither_enabled: bool = False
    """TPDF dither on int16 quantization (Phase 4 / T4.43).

    When True the FrameNormalizer's float→int16 conversion adds
    triangular-PDF dither before saturation, decorrelating the
    quantization error from the signal. Adds ~+4.77 dB of
    broadband noise to the floor (canonical TPDF "dither
    penalty") in exchange for elimination of quantization
    harmonics on quiet sustained tones.

    Default ``False`` per ``feedback_staged_adoption``. Operators
    flip after pilot validation confirms the audible improvement
    on their hardware (typically inaudible on dense speech;
    audible on near-silence room tone)."""

    voice_wiener_entropy_skip_enabled: bool = False
    """Wiener-entropy signal-destruction detector (Phase 4 / T4.44).

    When True the FrameNormalizer's resample stage computes the
    per-frame Wiener entropy and skips the resample (returns the
    input unchanged) when entropy > ``voice_wiener_entropy_skip_threshold``.
    Empirically: spectra above the threshold are too noise-like
    for downstream DSP to extract useful content from — better
    to skip processing than waste CPU on lost-cause data.

    Default ``False`` per ``feedback_staged_adoption``. Operators
    flip after pilot validation confirms the threshold matches
    their hardware (a too-high threshold misses real destruction;
    too-low gates real speech)."""

    voice_quality_metrics_enabled: bool = False
    """Perceptual voice-quality estimator master switch (Phase 4 / T4.21).

    When True the FrameNormalizer's quality stage runs the
    configured ``voice_quality_engine`` over fixed-size capture
    windows. Foundation default ``False`` per
    ``feedback_staged_adoption``. Operators who want real DNSMOS
    must ALSO install the ``[voice-quality]`` extras::

        pip install sovyx[voice-quality]

    The default voice install ships with the
    :class:`~sovyx.voice._quality_metrics.NoOpQualityEstimator`
    which returns NaN scores — the heavy librosa + numba +
    scikit-learn transitive deps are NOT pulled into every
    daemon. See the engine choice rationale at
    :mod:`sovyx.voice._quality_metrics`."""

    voice_quality_engine: Literal["off", "dnsmos"] = "off"
    """Concrete quality engine when ``voice_quality_metrics_enabled=True``.

    * ``"off"`` — explicit no-op (also reachable via the master
      switch); kept for "engine selected but disabled" semantics.
    * ``"dnsmos"`` — Microsoft DNSMOS via the ``speechmos``
      package. Returns 4 sub-scores: OVRL / SIG / BAK / P808.
      Requires ``[voice-quality]`` extras — raises
      :class:`QualityEstimatorLoadError` at construction
      otherwise.

    Future engine slot ``"pesq"`` will be widened when a
    Windows-shippable PESQ binding becomes available (current
    pesq + pypesq packages fail Windows MSVC build per T4.21
    discovery)."""

    voice_phase_inversion_auto_recovery_enabled: bool = False
    """L-only auto-recovery on phase-inverted stereo input (Phase 4 / T4.46).

    When True the FrameNormalizer's _downmix latches the output to
    L-only after sustained L/R destructive correlation (3 consecutive
    inverted blocks per :data:`_PHASE_RECOVERY_ENGAGE_THRESHOLD`),
    and reverts to mean-downmix after sustained clean signal (50
    consecutive blocks per :data:`_PHASE_RECOVERY_REVERT_THRESHOLD`).

    Promotes the long-standing Band-aid #8 phase-inversion DETECTOR
    (observability-only) to a recovery ACTION. The detector still
    fires its WARN log unchanged regardless of this flag —
    operators who want only observability keep this flag off; those
    who want the daemon to self-heal on broken stereo hardware flip
    it on. Telemetry counter ``voice.audio.phase_inversion_recovery``
    fires on engage / revert transitions for dashboard forensics.

    Default ``False`` per ``feedback_staged_adoption``. Pre-T4.46
    behaviour (mean-downmix always) preserved bit-exactly when off."""

    voice_resample_peak_check_enabled: bool = False
    """Resample peak-clip detector master switch (Phase 4 / T4.45).

    When True the FrameNormalizer's non-passthrough path emits
    :data:`voice.audio.resample_peak_clip{state}` once per push
    based on the post-resample peak amplitude. Distinct from the
    R2 saturation counter — this one isolates overshoot
    introduced by the polyphase Gibbs phenomenon, while R2
    counts the final int16-rail hits (normal for hot inputs).

    Default ``False`` per ``feedback_staged_adoption``."""

    voice_wiener_entropy_skip_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    """Wiener-entropy boundary for the destruction detector.

    Frames with entropy > this value are skipped. Master-mission
    spec default 0.5 — empirically the boundary above which
    resample/AEC/STT extract no useful content. Range:

    * 0.0 → 0.3 — pure-tone / structured speech
    * 0.3 → 0.5 — typical voice with formants
    * 0.5 → 0.7 — degraded / heavy-noise
    * 0.7 → 1.0 — white-noise dominated (signal destroyed)

    Bounded ``[0.0, 1.0]``."""

    voice_dither_amplitude_lsb: float = Field(default=1.0, ge=0.0, le=4.0)
    """Peak-to-peak TPDF dither amplitude in int16 LSBs.

    1.0 = textbook TPDF (Lipshitz 1992 §3). Values < 1 reduce
    decorrelation; values > 1 lift the noise floor visibly.
    Bounded ``[0.0, 4.0]`` — anything beyond 4 LSB is audible
    noise on the quiet path."""

    voice_agc2_adaptive_floor_quantile: float = Field(default=0.25, gt=0.0, lt=1.0)
    """Quantile of the RMS history used as the noise-floor estimate.

    0.25 = first quartile (Q1) — robust under the assumption that
    25%+ of frames are background-only. Operators may tune lower
    (more aggressive — gate engages earlier) or higher (more
    conservative — gate engages later). Strict ``(0.0, 1.0)`` —
    extremes are degenerate (0.0 = min-tracker, 1.0 = max)."""

    voice_snr_silence_floor_db: float = Field(default=-90.0, ge=-120.0, le=-40.0)
    """Mean-square power floor (dBFS) below which a frame is skipped.

    Frames whose power sits below this floor carry no useful SNR
    information AND would pollute the minimum-tracker with
    sub-detection noise (LSB-quantization residuals on int16
    frames). -90 dBFS sits at ~1 LSB² which is the canonical
    "below detection" threshold for 16-bit PCM. Bounded
    ``[-120, -40]`` — values outside reject at config validation:
    above -40 dBFS would skip real ambient room tone; below -120
    dBFS is below the int16 quantization rail."""

    voice_use_os_dsp_when_available: bool = False
    """Defer NS to the OS DSP stack when one is detected (Phase 4 / T4.20).

    Default ``False`` per master-mission §Phase 4 / T4.19 — Sovyx
    prefers in-process NS for predictability (the OS DSP is opaque
    + version-dependent, especially Windows Voice Clarity which
    destroys VAD signal per CLAUDE.md anti-pattern #21). Operators
    explicitly flip to ``True`` to opt out of in-process NS when
    they trust the OS path on their hardware.

    When ``True`` AND
    :attr:`voice_noise_suppression_enabled=True` AND the OS-NS
    detector reports an active stack on the resolved capture
    endpoint, the factory's
    :func:`_build_noise_suppressor` returns ``None`` + logs
    :data:`voice.ns.deferred_to_os_dsp` so the operator sees the
    auto-disable in startup logs.

    Detection sources:

    * Windows: :data:`voice_clarity_active` from the
      :mod:`sovyx.voice._apo_detector` capture-APO report.
    * Linux: :data:`echo_cancel_loaded` from the
      :mod:`sovyx.voice.health._pipewire` report.
    * macOS: :data:`virtual_audio_active` OR
      :data:`audio_enhancement_active` from the
      :mod:`sovyx.voice.health._macos` HAL plug-in report."""

    cascade_host_api_alignment_enabled: bool = False
    """Cascade ↔ runtime host-API alignment (cross-platform).

    Furo W-4 latent-bug fix: ``_stream_opener._device_chain`` currently
    iterates siblings in raw PortAudio enumeration order even when the
    cascade winner picked a specific host_api. This causes the runtime
    opener to drift off the cascade winner on multi-host_api endpoints —
    specifically reproducible on Razer BlackShark V2 Pro under Windows
    Voice Clarity, where the cascade selects DirectSound and the opener
    silently re-picks MME (which inherits the same APO chain).

    When enabled, ``_device_chain`` consumes ``preferred_host_api``
    (cascade winner) and ``capture_fallback_host_apis`` (currently dead
    config) and applies a 3-tier bucket sort: Bucket 0 preferred,
    Bucket 1 ranked fallbacks, Bucket 2 PortAudio enumeration order.

    Pre-requisite for ``bypass_tier2_host_api_rotate_enabled``.
    Default-flip planned for v0.25.0. See spec §D4 / Part 4 of mission
    doc."""

    @model_validator(mode="after")
    def _enforce_settle_ge_probe(self) -> VoiceTuningConfig:
        """v1.3 §4.1 L4-A — guard-rail against probe-window regression.

        The coordinator's post-apply probe window consumes both
        :attr:`integrity_probe_duration_s` (how many seconds of signal
        the probe analyses) and :attr:`bypass_strategy_post_apply_settle_s`
        (how many seconds elapse between apply and probe). The ring
        buffer only contains post-apply frames when ``settle >= probe``;
        any override that breaks the invariant silently reintroduces the
        v0.21.2 probe-window contamination bug (see dossier
        ``SVX-VOICE-LINUX-20260422``).

        Rejecting misconfig at boot is cheaper than a silent regression
        in production. The error message is specific so an operator
        tuning one knob via ``SOVYX_TUNING__VOICE__*`` sees both the
        offending values and which direction to adjust.
        """
        if self.bypass_strategy_post_apply_settle_s < self.integrity_probe_duration_s:
            msg = (
                "Invalid voice tuning: "
                f"bypass_strategy_post_apply_settle_s="
                f"{self.bypass_strategy_post_apply_settle_s} must be >= "
                f"integrity_probe_duration_s={self.integrity_probe_duration_s}. "
                "The post-apply probe window must not reach further back "
                "than the settle window advances forward — otherwise the "
                "probe analyses pre-apply frames and mis-classifies a "
                "successful fix as still degraded. Raise "
                "SOVYX_TUNING__VOICE__BYPASS_STRATEGY_POST_APPLY_SETTLE_S "
                "or lower SOVYX_TUNING__VOICE__INTEGRITY_PROBE_DURATION_S."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _enforce_customization_thresholds_ordered(self) -> VoiceTuningConfig:
        """Paranoid-QA HIGH #9 — L2.5 customization thresholds must be
        ordered ``apply <= skip``.

        The orchestrator branches on ``score`` using two knobs:

        * ``score < linux_mixer_user_customization_threshold_apply``
          → auto-apply the KB preset (low customization).
        * ``score > linux_mixer_user_customization_threshold_skip``
          → respect user intent, skip L2.5 entirely.
        * Between the two → defer for dashboard confirmation.

        If an operator's env override sets ``apply > skip`` (e.g.
        ``APPLY=0.8, SKIP=0.75``) the middle "defer" band inverts —
        there's no score that would land in defer, and the boundary
        between apply and skip becomes ambiguous at exact equality.
        Reject the config at startup instead of shipping surprising
        behaviour at first cascade.
        """
        # Paranoid-QA R2 MEDIUM #2: strict inequality, not <=. When
        # ``apply == skip`` the defer band has width zero — every
        # score lands either in apply-land or skip-land, with a
        # single exact-equality score that's ambiguous (orchestrator
        # uses ``<`` for apply and ``>`` for skip, so ``score ==
        # apply == skip`` triggers neither branch and silently
        # reaches the defer code path for a non-existent band).
        # Requiring strict separation guarantees every score maps to
        # exactly one of the three branches.
        if (
            self.linux_mixer_user_customization_threshold_apply
            >= self.linux_mixer_user_customization_threshold_skip
        ):
            msg = (
                f"linux_mixer_user_customization_threshold_apply="
                f"{self.linux_mixer_user_customization_threshold_apply} must be "
                f"< linux_mixer_user_customization_threshold_skip="
                f"{self.linux_mixer_user_customization_threshold_skip} — "
                "the apply threshold is the FLOOR of the defer band; "
                "skip is the CEILING. Equality eliminates the band "
                "(width zero) and produces an ambiguous boundary at "
                "the exact-equal score."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _enforce_paranoid_mission_dependencies(self) -> VoiceTuningConfig:
        """Voice Windows Paranoid Mission §D4 — Tier 2 host_api_rotate
        depends on cascade ↔ runtime alignment.

        Tier 2 ``WindowsHostApiRotateThenExclusiveBypass`` mutates the
        capture stream's host_api by calling
        ``request_host_api_rotate(target=WASAPI)`` and expects the
        opener to keep honouring that host_api on subsequent device-
        error reopens. Without
        ``cascade_host_api_alignment_enabled=True`` the opener falls
        back to its legacy PortAudio enumeration order on the next
        reopen and silently undoes the rotation — the pipeline reports
        ROTATED_SUCCESS while drifting back to the original (broken)
        host_api on the next hiccup.

        Reject the misconfiguration at boot with a remediation hint;
        cheaper than shipping a strategy that silently undoes itself.
        """
        if (
            self.bypass_tier2_host_api_rotate_enabled
            and not self.cascade_host_api_alignment_enabled
        ):
            msg = (
                "Invalid voice tuning: "
                "bypass_tier2_host_api_rotate_enabled=True requires "
                "cascade_host_api_alignment_enabled=True. The Tier 2 "
                "rotate-then-exclusive strategy mutates the capture "
                "stream's host_api; without the opener alignment fix "
                "(_stream_opener._device_chain bucket-sort) the next "
                "device-error reopen reverts to the legacy enumeration "
                "order and silently undoes the rotation. Set "
                "SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED=true "
                "to enable Tier 2."
            )
            raise ValueError(msg)
        return self


class LLMTuningConfig(BaseSettings):
    """Tunable thresholds for the LLM router complexity classifier.

    Overridable via ``SOVYX_TUNING__LLM__SIMPLE_MAX_LENGTH=300`` etc.
    See :class:`SafetyTuningConfig` for the ``BaseSettings`` rationale.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_TUNING__LLM__", extra="ignore")

    simple_max_length: int = 500
    simple_max_turns: int = 3
    complex_min_length: int = 2000
    complex_min_turns: int = 8


class ObservabilityFeaturesConfig(BaseSettings):
    """Feature flags for granular rollback of the observability subsystem.

    Each flag gates a discrete capability so a regression in one phase can
    be rolled back without disabling the entire stack. Defaults are the
    Phase 1 baseline (async queue + PII redaction + schema validation
    enabled; later-phase features off until their wireup lands).
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_OBSERVABILITY__FEATURES__", extra="ignore")

    async_queue: bool = True
    pii_redaction: bool = True
    saga_propagation: bool = False
    voice_telemetry: bool = True
    startup_cascade: bool = True
    plugin_introspection: bool = True
    anomaly_detection: bool = True
    tamper_chain: bool = False
    schema_validation: bool = True
    # Phase 11 Task 11.6 — opt-in dedicated Prometheus scrape port. Default
    # off because operators commonly run behind a firewall that doesn't
    # expose 9101; the dashboard also serves /metrics on its own port for
    # quick access. Enable in production observability deployments where
    # Prometheus needs an unauthenticated scrape endpoint isolated from
    # the dashboard's API surface.
    metrics_exporter: bool = False


class ObservabilityPIIConfig(BaseSettings):
    """Per-field PII redaction verbosity.

    Each field class can independently choose its mode:

    - ``minimal``: drop the value entirely (replace with ``"[redacted]"``)
    - ``redacted``: pattern-mask known PII inside the value (default)
    - ``hashed``: replace with deterministic SHA-256 prefix
      (``"sha256:abcdef…"``) so logs can correlate without exposing
    - ``full``: emit raw value (dev-only; CI gate forbids in prod profiles)
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_OBSERVABILITY__PII__", extra="ignore")

    user_messages: Literal["minimal", "redacted", "hashed", "full"] = "redacted"
    transcripts: Literal["minimal", "redacted", "hashed", "full"] = "redacted"
    prompts: Literal["minimal", "redacted", "hashed", "full"] = "redacted"
    responses: Literal["minimal", "redacted", "hashed", "full"] = "redacted"
    emails: Literal["minimal", "redacted", "hashed", "full"] = "hashed"
    phones: Literal["minimal", "redacted", "hashed", "full"] = "hashed"


class ObservabilitySamplingConfig(BaseSettings):
    """Sampling rates for high-frequency log streams.

    All ``*_rate`` fields are 1-in-N samplers (``rate=100`` means emit
    one frame, drop ninety-nine). Interval fields (``*_interval_*``)
    are wall-clock periods between snapshots.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_OBSERVABILITY__SAMPLING__", extra="ignore")

    audio_frame_rate: int = 100
    vad_frame_rate: int = 50
    wake_word_score_rate: int = 50
    output_queue_depth_interval_ms: int = 1000
    perf_hotpath_interval_seconds: int = 60


class ObservabilityTuningConfig(BaseSettings):
    """Numeric/policy tuning knobs for the observability subsystem.

    Sister-config to :class:`ObservabilityFeaturesConfig` (booleans):
    everything here is an integer/float threshold that defends a
    runtime budget. Lifted out of the parent so per-knob env overrides
    follow the documented ``SOVYX_OBSERVABILITY__TUNING__*`` namespace
    instead of being mixed into the top-level ``ObservabilityConfig``
    attributes.

    See :class:`SafetyTuningConfig` for the ``BaseSettings`` rationale.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_OBSERVABILITY__TUNING__", extra="ignore")

    max_field_bytes: int = Field(default=8 * 1024, ge=256, le=1024 * 1024)
    max_entry_bytes: int = Field(default=64 * 1024, ge=4 * 1024, le=4 * 1024 * 1024)
    metrics_cardinality_max_total: int = Field(default=10_000, ge=100, le=1_000_000)

    # ── Anomaly detector (P8.1) ──
    anomaly_window_size: int = Field(default=1000, ge=50, le=100_000)
    anomaly_min_samples: int = Field(default=50, ge=10, le=10_000)
    anomaly_latency_factor: float = Field(default=2.0, ge=1.1, le=100.0)
    anomaly_error_rate_window_s: int = Field(default=60, ge=5, le=3600)
    anomaly_error_rate_factor: float = Field(default=3.0, ge=1.1, le=100.0)
    anomaly_memory_growth_window_s: int = Field(default=300, ge=30, le=3600)
    anomaly_memory_growth_pct: float = Field(default=10.0, ge=1.0, le=100.0)
    anomaly_cooldown_s: int = Field(default=60, ge=1, le=3600)

    # ── Synthetic canary (§27.3) ──
    # Period between ``meta.canary.tick`` records. Bounded ``ge=5`` so
    # tests can exercise the loop quickly; ``le=3600`` so a misconfigured
    # daemon can't go a whole hour silent on the gap-detection probe.
    canary_interval_seconds: int = Field(default=60, ge=5, le=3600)


class ObservabilityOtelConfig(BaseSettings):
    """OpenTelemetry OTLP exporter configuration (Phase 11 Task 11.8).

    Default OFF. When ``enabled``, ``observability.otel.OtelExporter``
    installs a real ``TracerProvider`` with an OTLP/gRPC ``SpanExporter``
    targeting ``endpoint`` and the standard resource attributes
    (``service.name``, ``service.version``, ``deployment.environment``,
    ``host.name``, ``process.pid``).

    Optional packages — install via ``pip install sovyx[otel]``:
        - ``opentelemetry-exporter-otlp`` (required when enabled)
        - ``opentelemetry-instrumentation-httpx`` (auto-instrument
          httpx client calls with spans; controlled by
          ``instrument_httpx``)
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_OBSERVABILITY__OTEL__", extra="ignore")

    enabled: bool = False
    endpoint: str = "http://localhost:4317"
    insecure: bool = True
    deployment_environment: str = "dev"
    instrument_httpx: bool = True


class ObservabilityConfig(BaseSettings):
    """Sovyx observability subsystem configuration.

    Single source of truth for the structured-logging pipeline, PII
    policy, sampling, async queue sizing, ring buffer for crash
    forensics, log file rotation, crash dump path, and the FTS5
    sidecar index path.

    Both ``crash_dump_path`` and ``fts_index_path`` default to ``None``
    and are resolved by ``EngineConfig`` against ``data_dir`` so a
    custom ``SOVYX_DATA_DIR`` propagates correctly.
    """

    model_config = SettingsConfigDict(
        env_prefix="SOVYX_OBSERVABILITY__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    features: ObservabilityFeaturesConfig = Field(default_factory=ObservabilityFeaturesConfig)
    pii: ObservabilityPIIConfig = Field(default_factory=ObservabilityPIIConfig)
    sampling: ObservabilitySamplingConfig = Field(default_factory=ObservabilitySamplingConfig)
    tuning: ObservabilityTuningConfig = Field(default_factory=ObservabilityTuningConfig)
    otel: ObservabilityOtelConfig = Field(default_factory=ObservabilityOtelConfig)

    async_queue_size: int = Field(default=65536, ge=1024, le=1048576)
    ring_buffer_size: int = Field(default=2000, ge=100, le=50000)
    file_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024 * 1024)
    file_backup_count: int = Field(default=10, ge=1, le=100)
    crash_dump_path: Path | None = None
    fts_index_path: Path | None = None
    fast_path_file: Path | None = None
    # Phase 11 Task 11.6 — port for the dedicated Prometheus scrape
    # endpoint started by ``observability.prometheus.PrometheusHttpServer``.
    # Only honored when ``features.metrics_exporter`` is on.
    metrics_port: int = Field(default=9101, ge=1, le=65535)

    # Phase 11+ Task 11+.2 — global cardinality budget shared across
    # every counter / histogram / gauge. Once the *total* number of
    # (metric, label-tuple) pairs reaches ``metrics_max_series``, new
    # label combinations are folded into a single ``_overflow=true``
    # series per metric and a one-shot WARNING is emitted with the
    # canonical ``metrics.cardinality.exceeded`` event. The default of
    # 10 000 series matches the §22.7 budget and Prometheus best
    # practice (each series carries ~3 KiB of memory in the scrape
    # backend, so 10 000 ≈ 30 MiB worst-case before the budget kicks).
    metrics_max_series: int = Field(default=10_000, ge=100, le=1_000_000)


class TuningConfig(BaseModel):
    """Aggregate tuning knobs for cognitive / brain / voice / llm subsystems.

    Single source of truth for previously module-level constants. All
    defaults match the historical hardcoded values; subsystems read from
    a ``TuningConfig`` instance built from ``EngineConfig.tuning``.
    """

    safety: SafetyTuningConfig = Field(default_factory=SafetyTuningConfig)
    brain: BrainTuningConfig = Field(default_factory=BrainTuningConfig)
    voice: VoiceTuningConfig = Field(default_factory=VoiceTuningConfig)
    llm: LLMTuningConfig = Field(default_factory=LLMTuningConfig)


class SecurityConfig(BaseModel):
    """Operator-managed security knobs that have boot-time consequences.

    Currently scoped to the secret-rotation hygiene check (§22.4): the
    operator stamps ``secrets_rotated_at`` whenever they rotate any
    ``SOVYX_*`` secret (provider API keys, license JWT, webhook URLs)
    and the daemon emits ``security.secrets.rotation_overdue`` at boot
    when the timestamp is older than ``rotation_warn_days``.

    The check is intentionally a *warning*, not a hard failure — a
    daemon that refuses to start because a secret is 91 days old is
    worse than one that runs and asks the operator to rotate. Hardening
    to "fail closed" is left to operators via custom alerting on the
    emitted event.

    Why a separate model instead of a dedicated env var: rotation
    cadence is a deployment policy, not a secret. Keeping it on the
    config object makes the value visible to ``sovyx doctor``, the
    dashboard config view, and the audit log without leaking through
    process environment.
    """

    secrets_rotated_at: datetime | None = Field(
        default=None,
        description=(
            "ISO-8601 timestamp the operator last rotated SOVYX_* secrets. "
            "When None, the boot-time check skips (fresh install grace). "
            "Set via SOVYX_SECURITY__SECRETS_ROTATED_AT=2026-04-20T00:00:00Z."
        ),
    )
    rotation_warn_days: int = Field(
        default=90,
        ge=1,
        le=3650,
        description=(
            "Age (in days) past which a rotation warning is emitted at "
            "boot. Default 90 mirrors the §22.4 procedure; raise for "
            "low-traffic deployments where rotation is more disruptive."
        ),
    )


class SocketConfig(BaseModel):
    """Unix socket path for daemon RPC.

    Auto-resolves: /run/sovyx/sovyx.sock (systemd) or ~/.sovyx/sovyx.sock (user).
    """

    path: str = ""

    @model_validator(mode="after")
    def resolve_path(self) -> SocketConfig:
        """Auto-resolve socket path based on environment."""
        if not self.path:
            system_path = Path("/run/sovyx")
            if system_path.exists() and os.access(system_path, os.W_OK):
                self.path = "/run/sovyx/sovyx.sock"
            else:
                # #32 — defensive home resolution at the RPC socket
                # path computation; otherwise a missing HOME causes
                # the daemon's RPC layer to fail at startup with a
                # confusing "Could not determine home directory" trace.
                self.path = str(resolve_home_dir() / ".sovyx" / "sovyx.sock")
        return self


class EngineConfig(BaseSettings):
    """Global Sovyx daemon configuration.

    Inherits from BaseSettings (pydantic-settings) for env_prefix support.

    Priority (highest to lowest):
        1. Environment variables (SOVYX_*)
        2. system.yaml
        3. Hardcoded defaults
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_", env_nested_delimiter="__")

    data_dir: Path = Field(
        # #32 — defensive home resolution at the EngineConfig level
        # (mirrors DatabaseConfig.data_dir). Daemon startup must
        # never crash on a missing HOME / sandboxed home; the
        # fallback is a per-user tempdir with structured WARN.
        default_factory=lambda: resolve_home_dir() / ".sovyx",
    )
    log: LoggingConfig = Field(default_factory=LoggingConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    llm: LLMDefaultsConfig = Field(default_factory=LLMDefaultsConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    relay: RelayConfig = Field(default_factory=RelayConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    socket: SocketConfig = Field(default_factory=SocketConfig)
    tuning: TuningConfig = Field(default_factory=TuningConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    @model_validator(mode="after")
    def resolve_log_file(self) -> EngineConfig:
        """Resolve log_file relative to data_dir when not explicitly set.

        Default: ``<data_dir>/logs/sovyx.log``.  This ensures that
        ``SOVYX_DATA_DIR=/data/sovyx`` puts logs at
        ``/data/sovyx/logs/sovyx.log`` instead of the hardcoded
        ``~/.sovyx/logs/sovyx.log``.

        If ``log_file`` is explicitly set (YAML, env, or override),
        the explicit value is preserved unchanged.
        """
        if self.log.log_file is None:
            self.log.log_file = self.data_dir / "logs" / "sovyx.log"
        return self

    @model_validator(mode="after")
    def resolve_observability_paths(self) -> EngineConfig:
        """Resolve observability sidecar paths relative to ``data_dir``.

        - ``crash_dump_path`` → ``<data_dir>/logs/sovyx.crash.jsonl``
        - ``fts_index_path``  → ``<data_dir>/logs/sovyx.log.idx``
        - ``fast_path_file``  → ``<data_dir>/logs/sovyx.crit.jsonl``

        Explicit values (YAML, env, or override) are preserved unchanged.
        """
        logs_dir = self.data_dir / "logs"
        if self.observability.crash_dump_path is None:
            self.observability.crash_dump_path = logs_dir / "sovyx.crash.jsonl"
        if self.observability.fts_index_path is None:
            self.observability.fts_index_path = logs_dir / "sovyx.log.idx"
        if self.observability.fast_path_file is None:
            self.observability.fast_path_file = logs_dir / "sovyx.crit.jsonl"
        return self


def load_engine_config(
    config_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> EngineConfig:
    """Load engine configuration with merge: defaults → yaml → env → overrides.

    Args:
        config_path: Path to system.yaml. If None, uses defaults + env only.
        overrides: Programmatic overrides (highest priority after env).

    Returns:
        Fully resolved EngineConfig.

    Raises:
        ConfigNotFoundError: config_path provided but file does not exist.
        ConfigValidationError: YAML contains invalid fields or values.
    """
    yaml_data: dict[str, Any] = {}

    if config_path is not None:
        if not config_path.exists():
            raise ConfigNotFoundError(
                f"Configuration file not found: {config_path}",
                context={"path": str(config_path)},
            )
        try:
            raw = config_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(raw)
            if parsed is not None:
                if not isinstance(parsed, dict):
                    raise ConfigValidationError(
                        "Configuration file must contain a YAML mapping",
                        context={"path": str(config_path), "type": type(parsed).__name__},
                    )
                yaml_data = parsed
        except yaml.YAMLError as exc:
            raise ConfigValidationError(
                f"Invalid YAML in configuration file: {exc}",
                context={"path": str(config_path)},
            ) from exc

    # Backward compatibility: migrate legacy "format" → "console_format"
    _migrate_legacy_log_format(yaml_data)

    if overrides:
        yaml_data = _deep_merge(yaml_data, overrides)

    try:
        return EngineConfig(**yaml_data)
    except Exception as exc:  # noqa: BLE001
        raise ConfigValidationError(
            f"Configuration validation failed: {exc}",
            context={"fields": str(yaml_data.keys())},
        ) from exc


_DEPRECATED_MIXER_FRACTIONS: tuple[tuple[str, float], ...] = (
    ("linux_mixer_boost_reset_fraction", 0.0),
    ("linux_mixer_capture_reset_fraction", 0.5),
    ("linux_mixer_capture_attenuation_fix_fraction", 0.5),
    ("linux_mixer_boost_attenuation_fix_fraction", 0.33),
)
"""Mission §9.1.1 / Gap 1 deprecation roster — name + default value.

The four hardcoded mixer fractions that drive the legacy
``LinuxALSAMixerResetBypass`` band-aid. Scheduled for removal in
v0.27.0 (Phase 4 — AEC + audio quality), bumped from v0.24.0 per
T1.51, once the L2.5 KB-driven preset cascade (Layer 3) + in-process
AGC2 (Layer 4) replace both the saturation and attenuation regimes
AND the bypass-coordinator wire-up gating Phase 2 + 3 has soaked
through one minor-version cycle.

Until then the fractions remain settable via
``SOVYX_TUNING__VOICE__LINUX_MIXER_*_FRACTION`` env vars per the
migration plan §8 (deprecation warning only, no behaviour change).
``warn_on_deprecated_mixer_overrides`` consults this roster at boot
to fire one structured WARN per non-default override.
"""


def warn_on_deprecated_mixer_overrides(
    tuning: VoiceTuningConfig | None = None,
) -> tuple[str, ...]:
    """Emit one boot-time WARN per non-default deprecated mixer fraction.

    Mission §9.1.1 / Gap 1b — the four ``linux_mixer_*_fraction`` knobs
    are scheduled for removal in v0.27.0 (bumped from v0.24.0 per
    T1.51). Operators who set them via YAML or
    ``SOVYX_TUNING__VOICE__LINUX_MIXER_*_FRACTION`` env vars get a
    structured WARN at boot so they have multiple minor-version
    cycles to migrate to the L2.5 KB-driven preset cascade
    (Layer 3) + in-process AGC2 (Layer 4) replacement path.

    The WARN is opt-in by virtue of the operator having set a
    non-default value — a stock install with no overrides emits
    nothing. The migration plan §8 contract is "deprecation warnings
    only, no behaviour change" until v0.27.0.

    Args:
        tuning: Pre-instantiated :class:`VoiceTuningConfig` (tests
            inject a stub). ``None`` builds a fresh instance, which
            picks up live env overrides via pydantic-settings.

    Returns:
        Tuple of the field names that triggered a WARN. Useful for
        tests + dashboard surfaces that want to render the
        "deprecated knobs in use" badge without re-walking the
        config.
    """
    from sovyx.observability.logging import get_logger

    logger = get_logger(__name__)
    cfg = tuning if tuning is not None else VoiceTuningConfig()
    deprecated_in_use: list[str] = []
    for field_name, default_value in _DEPRECATED_MIXER_FRACTIONS:
        actual = getattr(cfg, field_name, default_value)
        # Use math.isclose so a YAML 0.50 vs python 0.5 round-trip
        # doesn't trigger a false positive — the comparison is for
        # "operator deliberately changed it", not bit-exact identity.
        from math import isclose

        if isclose(float(actual), float(default_value), rel_tol=0.0, abs_tol=1e-9):
            continue
        deprecated_in_use.append(field_name)
        logger.warning(
            "voice.config.deprecated_mixer_fraction_in_use",
            **{
                "voice.config.field": field_name,
                "voice.config.value": float(actual),
                "voice.config.default": float(default_value),
                # T1.51 — removal target bumped from v0.24.0 to v0.27.0
                # (Phase 4) per
                # ``MISSION-voice-final-skype-grade-2026.md``: the
                # bypass-coordinator wire-up gating Phase 2 + 3 must
                # land first; until then the legacy fractions remain
                # functional but emit this WARN. Aligned with the
                # function-level deprecation WARN in
                # ``voice/health/_linux_mixer_apply.py`` and the
                # bypass-strategy WARN in
                # ``voice/health/bypass/_linux_alsa_mixer.py``.
                "voice.config.removal_target": "v0.27.0",
                "voice.action_required": (
                    f"{field_name} is deprecated and scheduled for "
                    "removal in v0.27.0 (Phase 4 — AEC + audio quality). "
                    "The L2.5 KB-driven preset cascade (Layer 3) + "
                    "in-process AGC2 (Layer 4) replace both the "
                    "saturation and attenuation regimes the legacy "
                    "fractions targeted. To silence this warning, unset "
                    "the env override (SOVYX_TUNING__VOICE__"
                    f"{field_name.upper()}) and let the new cascade "
                    "drive remediation. KB profile contribution: see "
                    "docs/contributing/voice-mixer-kb-profiles.md."
                ),
            },
        )
    return tuple(deprecated_in_use)


def _migrate_legacy_log_format(data: dict[str, Any]) -> None:
    """Migrate legacy ``log.format`` to ``log.console_format``.

    Mutates *data* in place.  Emits a deprecation warning (via stdlib
    ``warnings``) so users see it once and know to update their YAML.

    The ``format`` field was renamed to ``console_format`` in v0.5.24
    to clarify that it only controls console output (the file handler
    always writes JSON).

    This migration is idempotent: if both ``format`` and
    ``console_format`` exist, ``console_format`` wins and ``format``
    is silently dropped.
    """
    import warnings

    log_section = data.get("log")
    if not isinstance(log_section, dict):
        return

    if "format" not in log_section:
        return

    legacy_value = log_section.pop("format")

    if "console_format" not in log_section:
        log_section["console_format"] = legacy_value
        warnings.warn(
            "Configuration key 'log.format' is deprecated since v0.5.24. "
            "Use 'log.console_format' instead. "
            f"Migrated automatically: console_format={legacy_value!r}",
            DeprecationWarning,
            stacklevel=2,
        )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dicts. Override values win on conflict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
