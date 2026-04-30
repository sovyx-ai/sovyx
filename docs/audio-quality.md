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
