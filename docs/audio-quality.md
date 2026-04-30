# Audio Quality

Sovyx ships a measured DSP pipeline. This page publishes the
quality numbers the project guarantees, the bounds the CI gate
enforces, and the methodology behind every measurement so
operators can verify the claims independently.

> **TL;DR** — the resampler clears the master-mission §Phase 4
> promotion gate (alias ≤ -60 dBFS) by 100 dB of headroom. THD
> and spectrum SNR clear by 18-20 dB. No upgrade to a custom
> sinc / higher-order polyphase resampler is required for the
> Phase 4 release.

## Resampler quality (44.1 → 16 kHz path)

The capture-side
[`FrameNormalizer`](../src/sovyx/voice/_frame_normalizer.py)
resamples PortAudio's negotiated capture rate to the pipeline's
16 kHz invariant via `scipy.signal.resample_poly` (default
Kaiser β=5.0 window). The 44.1 → 16 kHz path is the most common
deployment combination (most consumer mics deliver 44.1 kHz);
its measurements are the worst-case data point published below.

### Headline numbers (measured 2026-04-29)

| Test                 | Measured | CI gate    | Headroom |
|----------------------|----------|-----------|----------|
| Pure-tone THD        | -83 dB   | ≤ -65 dB  | 18 dB    |
| White-noise alias    | -200 dB  | ≤ -100 dB | 100 dB   |
| Chirp spectrum SNR   | +60 dB   | ≥ +40 dB  | 20 dB    |

The CI gate
([`tests/unit/voice/test_resampler_quality.py`](../tests/unit/voice/test_resampler_quality.py))
runs on every commit. A scipy version drift that regresses any
metric below its gate fails CI before reaching production. The
measured numbers are well above broadcast-grade thresholds —
the headroom exists so a future scipy upgrade has visible margin
before triggering the master-mission §Phase 4 / T4.42 escalation
("upgrade to higher-order polyphase / sinc resampler").

### Methodology

Each test drives a synthetic signal through the same
`resample_poly` flow the FrameNormalizer uses, then measures one
spectral property of the output. All FFTs use a Hann window
(suppresses leakage from non-integer-bin fundamentals); all SNR
computations are spectrum-based (delay-invariant) since the
resampler introduces 5-10 ms of group delay.

#### 1. Pure-tone THD

* **Input** — 1 kHz sine at amplitude 0.5, 8192 samples @ 44.1 kHz.
* **Output** — resampled to 16 kHz (~2973 samples).
* **Measure** — `compute_thd_db` with `tolerance_bins=4` (Hann
  leakage spans ~3 bins at ±1 of fundamental; 4 covers the
  envelope).
* **Result** — total non-fundamental energy is **83 dB** below the
  fundamental. -60 dB ≈ "broadcast clean"; this resampler is
  ~24 dB cleaner.

#### 2. White-noise alias energy

* **Input** — Gaussian noise at amplitude 0.3 (clipped to ±1.0),
  8192 samples @ 44.1 kHz.
* **Reference** — same noise low-passed by an 8th-order Butterworth
  at the new Nyquist - 1 kHz guard, then resampled. This is the
  "ideal in-band" output the resampler should match.
* **Measure** — `compute_alias_energy_db` — power at output bins
  where the reference had near-zero energy (<1% of in-band RMS).
* **Result** — alias energy is at the helper's -200 dB floor:
  scipy's polyphase filter rejects out-of-band content below
  measurement precision.

#### 3. Chirp spectrum SNR

* **Input** — linear chirp 200 Hz → 4 kHz at amplitude 0.5,
  8192 samples @ 44.1 kHz.
* **Reference** — same chirp generated directly at 16 kHz.
* **Measure** — `compute_spectrum_snr_db` over Hann-windowed
  magnitude spectra (delay-invariant; sample-by-sample SNR
  would mis-attribute the resampler's group delay as distortion
  and produce ~+10 dB on a perfect resampler).
* **Result** — spectra match within **+60 dB** SNR. The
  difference is dominated by Hann-window edge attenuation, not
  resampler artefacts.

### Reproducing locally

```bash
uv run python -m pytest tests/unit/voice/test_resampler_quality.py -v -s
```

The `-s` flag preserves the `print()` statements so the live
measured numbers appear in the test log alongside the assertion
verdicts. Output looks like:

```
[T4.41/THD]   1 kHz tone @ 44.1→16 kHz: -83.01 dB
[T4.41/ALIAS] white noise @ 44.1→16 kHz: -200.00 dB
[T4.41/SNR ]  chirp 200-4k @ 44.1→16 kHz: 59.69 dB
```

### Known limitations

* **Single rate combination** — the published numbers are for
  44.1 → 16 kHz only. Other rates (48 → 16 kHz, 96 → 16 kHz)
  use the same `resample_poly` flow with proportional filter
  lengths; spot-checks at boot show similar ranges but are not
  pinned in CI yet.
* **No real-mic data** — synthetic test signals only. Real
  microphones add their own noise floor, frequency response and
  THD; the resampler's -83 dB THD is meaningless on a mic with
  -50 dB self-noise. Use this page to verify *Sovyx isn't
  introducing distortion*, not to characterise the mic itself.
* **scipy `resample_poly` version dependency** — measurements
  taken against scipy 1.x. A future scipy 2.x with a different
  default filter window would change the absolute numbers; the
  CI gate's purpose is to catch this.

## Other Phase 4 quality gates

The full Phase 4 DSP stack publishes additional measurements
through OpenTelemetry counters and histograms. See
[Observability](observability.md) for the canonical metric
catalogue. Highlights for audio quality:

| Metric                                | Promotion gate                  |
|---------------------------------------|---------------------------------|
| `voice.aec.erle_db` (p50)             | ≥ 35 dB sustained               |
| `voice.aec.erle_db` (p95)             | ≥ 30 dB sustained               |
| `voice.ns.suppression_db` (p50)       | ≥ 5 dB on stationary noise      |
| `voice.audio.snr_db` (p50)            | ≥ 9 dB (Moonshine STT threshold)|
| `voice.audio.signal_destroyed{state}` | < 5% destroyed sustained        |
| `voice.audio.resample_peak_clip{state}` | < 1% clip sustained          |

These are pulled directly from the live pipeline; the dashboard's
voice-quality panel (Phase 4 / T4.37) plots them per session.

## SNR ranges per use case (Phase 4 / T4.40)

The pipeline measures per-window signal-to-noise ratio via
[`SnrEstimator`](../src/sovyx/voice/_snr_estimator.py) and emits
the result as `sovyx.voice.audio.snr_db` (histogram) +
`snr_p50_db` / `snr_p95_db` on every `voice_pipeline_heartbeat`.
Per-session SNR p50 falls into the following ranges; the Phase 4
/ T4.35 alert (`voice_pipeline_snr_low_alert`) fires when the
de-flap threshold is crossed.

| Range          | Verdict   | What it means                                        | STT / VAD impact                                 |
|----------------|-----------|------------------------------------------------------|--------------------------------------------------|
| **≥ 17 dB**    | Excellent | Studio mic in a treated room, or a headset close-mic | Moonshine + Silero operate at calibrated rates   |
| **9 to 17 dB** | Good      | Typical office desk mic, HVAC running                | <1% STT substitution rate degradation            |
| **3 to 9 dB**  | Degraded  | Loud open-plan office, fan close to mic              | STT substitution rate climbs ~3-5× per dB lost   |
| **< 3 dB**     | Poor      | Mic in noisy environment (cafe, vehicle)             | STT becomes unreliable; VAD onset latency climbs |

**The 9 dB floor is the configured Moonshine STT degradation
threshold per master mission §Phase 4 / T4.35.** Sovyx defaults
`voice_snr_low_alert_threshold_db = 9.0` to fire at this floor;
operators using a different STT engine should retune the floor
to match their model's noise-robustness profile (Whisper-tiny
typically degrades at higher SNR than Moonshine, fine-tuned
models tolerant of room noise can run lower). Validate per-engine
via the `voice.audio.snr_db` histogram correlated with STT
substitution-rate telemetry over a representative sample.

The recommended remediation order when SNR p50 sits in the
"degraded" range:

1. **Move the mic** — even 30 cm closer adds 3-6 dB of speech
   energy without affecting noise.
2. **Enable in-process noise suppression** — flip
   `voice_noise_suppression_enabled = True` (foundation default
   off per `feedback_staged_adoption`). The spectral-gating
   engine targets ~5-10 dB SNR improvement on stationary noise
   (HVAC, fans).
3. **Check OS DSP detection** — when
   `voice_use_os_dsp_when_available` is True the factory logs
   `voice.ns.deferred_to_os_dsp` at boot if the Phase 4 / T4.18
   detector finds an active OS NS chain. If OS NS is already
   active and removing transient noise, in-process NS would
   double-suppress; see the trade-off section below.
4. **Lower the STT confidence threshold** — last resort, accepts
   higher substitution rate in exchange for lower
   "unintelligible" rejections.

## OS DSP vs in-process NS trade-off (Phase 4 / T4.19)

The `voice_use_os_dsp_when_available` tuning flag (foundation
default `False`) lets operators defer noise suppression to the
operating-system DSP stack instead of running Sovyx's
in-process spectral-gating engine. The choice has real trade-offs
that depend on the deployment.

**Sovyx's default policy: prefer in-process NS over OS NS for
predictability.** The rationale below is the durable record of
why; operators flipping the flag should understand what they're
opting into.

### Why in-process NS is the default

* **VAD accuracy**. OS NS engines (Windows Voice Clarity, Linux
  `module-echo-cancel`, macOS Voice Isolation, Krisp) target
  human listener perception, not VAD model input. They aggressively
  suppress signal characteristics that VAD depends on
  (sub-band energy at 80-300 Hz, harmonic structure during voiced
  consonants). On Voice Clarity-affected hardware (CLAUDE.md
  anti-pattern #21) the *speech itself* gets destroyed — the
  underlying root cause behind the cold-probe signal-validation
  fix (anti-pattern #28).
* **Predictability across deployments**. The same Sovyx version
  on Windows, Linux, macOS produces the same DSP behaviour when
  in-process NS owns the chain. Defer to OS DSP and the
  spectral envelope reaching VAD depends on which OS, which
  version, which audio driver, and which third-party DSP is
  installed (Krisp, NoiseGator, NVIDIA RTX Voice). Reproducing
  customer issues requires matching all of these, not just
  Sovyx's version.
* **Auditability**. In-process NS emits per-window
  `voice.ns.suppression_db` telemetry (Phase 4 / T4.16). Every
  attenuation decision is observable. OS NS is opaque — the
  signal that reaches Sovyx has already been processed by a
  closed-source pipeline; "what changed" between two heartbeats
  is unanswerable.
* **Bypass-mode compatibility**. When WASAPI exclusive mode
  engages (statically via `capture_wasapi_exclusive` or via
  the Voice Clarity APO auto-fix — anti-pattern #21), the OS
  AEC + NS chain is bypassed entirely. In-process NS keeps
  working in exclusive mode; OS NS does not. The Phase 4 /
  T4.6 AEC bypass-combo detector
  (`sovyx.voice.aec.bypass_combo`) already alerts on this combo
  for AEC; the same logic applies to NS.

### When OS DSP is the better choice

Some deployments benefit from `voice_use_os_dsp_when_available =
True`:

* **Aggressive non-stationary noise** that the spectral-gating
  engine doesn't address. Krisp / RTX Voice handle keyboard
  clicks, dog barks, and traffic better than spectral gating
  because they're DNN-based on much larger training corpora.
  Until the optional librnnoise ctypes shim ships (deferred to
  v0.28.0+, mission §Phase 4 / T4.15), in-process NS is
  spectral-gating only.
* **Operator already paid for premium DSP**. Krisp on
  enterprise plans costs $5-10/seat/month — a Sovyx deployment
  on top of a Krisp-enabled fleet shouldn't double-suppress.
  Flipping the flag to `True` lets the OS chain own NS while
  Sovyx focuses on AEC + signal validation.
* **Headphone-only deployments**. Headset mics with noise
  cancellation (Sennheiser ANC, Bose, AirPods Pro) ship their
  own DSP that performs well. The Sovyx in-process layer adds
  negligible value on a -50 dBFS noise floor; bypassing saves
  CPU for STT inference.

### How to flip safely

1. Check the boot logs for `voice.ns.deferred_to_os_dsp` after
   flipping the flag — the entry confirms the T4.18 detector
   found an active OS NS chain. Without an active OS NS the
   flag is a no-op (and worse: with `voice_noise_suppression_
   enabled = False` it leaves no NS in the chain).
2. Disable in-process NS: `voice_noise_suppression_enabled =
   False`. Setting only `voice_use_os_dsp_when_available = True`
   does not disable the in-process engine — both run, doubling
   suppression and over-attenuating speech.
3. Validate via `voice.audio.snr_db` histogram for ≥1 day.
   The post-flip SNR should match or exceed the in-process
   baseline; if it drops by more than 3 dB, the OS DSP is
   either inactive or destroying signal — revert.
4. Watch `voice_pipeline_snr_low_alert{state=warned}` over the
   first 7 days. The alert clears at 9 dB by default; sustained
   alerts on the OS-DSP path indicate the trade-off didn't pay
   off for that hardware.

The Phase 4 / T4.18 detector + `voice_use_os_dsp_when_available`
flag together implement the trade-off; the call site in
[`voice/factory/__init__.py`](../src/sovyx/voice/factory/__init__.py)
already short-circuits in-process NS construction when both
conditions are met, so the runtime cost of the wrong choice is
"slightly different DSP behaviour", not "broken pipeline".

## Cross-references

* Master mission §Phase 4 promotion gates —
  [`docs-internal/missions/MISSION-voice-final-skype-grade-2026.md`](https://github.com/sovyx-ai/sovyx)
  (internal).
* CI gate source —
  [`tests/unit/voice/test_resampler_quality.py`](../tests/unit/voice/test_resampler_quality.py).
* DSP helpers —
  [`src/sovyx/voice/_resampler_quality.py`](../src/sovyx/voice/_resampler_quality.py).
* Observability metrics catalogue —
  [`docs/observability.md`](observability.md).
* SNR estimator implementation —
  [`src/sovyx/voice/_snr_estimator.py`](../src/sovyx/voice/_snr_estimator.py).
* Noise suppression engine —
  [`src/sovyx/voice/_noise_suppression.py`](../src/sovyx/voice/_noise_suppression.py).
