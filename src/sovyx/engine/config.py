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

    data_dir: Path = Field(default_factory=lambda: Path.home() / ".sovyx")
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

    transcribe_timeout_seconds: float = 10.0
    streaming_drain_seconds: float = 0.5
    cloud_stt_timeout_seconds: float = 30.0
    cloud_stt_max_audio_seconds: float = 120.0
    auto_select_min_gpu_vram_mb: int = 4_000
    auto_select_high_ram_threshold_mb: int = 16_000
    auto_select_low_ram_threshold_mb: int = 2_048
    capture_reconnect_delay_seconds: float = 2.0
    capture_queue_maxsize: int = 256

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
    pipeline_heartbeat_interval_seconds: float = 5.0
    # Deaf-pipeline heuristic: if the orchestrator has processed at
    # least ``pipeline_deaf_min_frames`` frames in the current heartbeat
    # window and the max observed VAD probability never crossed
    # ``pipeline_deaf_vad_max_threshold``, emit
    # ``voice_pipeline_deaf_warning``. This surfaces the class of bug
    # where audio is captured (``audio_capture_heartbeat`` shows real
    # RMS) but VAD silently rejects every frame — typically because the
    # frames reaching :meth:`VoicePipeline.feed_frame` are not 16 kHz
    # mono (FrameNormalizer misconfigured / bypassed).
    pipeline_deaf_min_frames: int = 150  # ~4.8 s at 32 ms/frame
    pipeline_deaf_vad_max_threshold: float = 0.05
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
    deaf_warnings_before_exclusive_retry: int = 2
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

    # CaptureIntegrityCoordinator — iterates registered platform
    # strategies in ``cost_rank`` order. Hard cap per session guards
    # against pathological oscillation (strategy A applies, reverts,
    # strategy B applies, reverts, ad infinitum). The post-apply
    # settle window is the driver-side debounce before re-probing.
    bypass_strategy_max_attempts: int = 3
    bypass_strategy_post_apply_settle_s: float = 1.5

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
    linux_mixer_boost_reset_fraction: float = 0.0
    # Fraction of ``max_raw`` to set Capture-class controls to on apply.
    # ``0.5`` ≈ 0 dB for most codecs with the 0..80 / -40..+30 dB range
    # observed on HDA Intel / Realtek / SN6180 parts. Never ``0.0`` —
    # that would mute the mic and a subsequent probe would classify the
    # endpoint as DRIVER_SILENT rather than HEALTHY.
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

    # AudioCaptureTask ring buffer — bounded snapshot of the most
    # recent frames delivered by PortAudio. Fed by the capture
    # callback, consumed by :meth:`AudioCaptureTask.tap_recent_frames`
    # so the integrity probe can re-analyse the live signal without
    # opening a second stream (critical on Windows: a concurrent open
    # on an exclusive-held endpoint raises AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED).
    # Sized for ``integrity_probe_duration_s`` + watchdog recheck
    # window. 33 s at 16 kHz mono int16 ≈ 1 MB — bounded, acceptable.
    capture_ring_buffer_seconds: float = 33.0

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
                self.path = str(Path.home() / ".sovyx" / "sovyx.sock")
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

    data_dir: Path = Field(default_factory=lambda: Path.home() / ".sovyx")
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
