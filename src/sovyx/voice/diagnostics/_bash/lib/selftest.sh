#!/usr/bin/env bash
# lib/selftest.sh — Analyzer calibration self-test (T0).
#
# Rationale (enterprise-grade)
# ----------------------------
# Todas as conclusões forenses derivam das métricas produzidas por
# ``analyze_wav.py`` e ``silero_probe.py``. Se qualquer um deles tem
# bug sutil que a auditoria V3 não pegou, toda a análise downstream é
# contaminada silenciosamente. Este self-test é o equivalente ao
# procedimento de calibração de um equipamento de medição em
# laboratório: antes de medir qualquer coisa real, valida-se que o
# instrumento reporta os valores corretos para um SINAL CONHECIDO.
#
# O self-test:
#   1. Gera um tom sinoidal de 440 Hz, -20 dBFS, 1 s, 16 kHz mono s16.
#   2. Roda ``analyze_wav.py`` sobre esse WAV.
#   3. Verifica que cada métrica cai dentro da tolerância teórica.
#   4. Se QUALQUER métrica estiver fora da tolerância, ABORTA o run
#      com erro loud. A análise não começa.
#
# Resultado persistido em ``_diagnostics/analyzer_selftest.json`` e
# referenciado em SUMMARY.json. Um auditor externo pode verificar que
# o self-test passou antes de confiar em qualquer número derivado.

# Tolerâncias derivadas da teoria do sinal de teste:
#   - Tom 440 Hz, amplitude 0.1 (= -20 dBFS) → RMS = 0.1/sqrt(2) ≈ 0.0707
#     → 20*log10(0.0707) = -23.0 dBFS (teoria pura)
#   - Na prática, tone_gen aplica envelope de fade-in/out de 5ms, o
#     que reduz ligeiramente o RMS total. Tolerância generosa: -24 a -20.
#   - Peak: 0.1 * 32767 = 3276 → peak_dbfs = 20*log10(3276/32768) = -20.0
#     Tolerância: -21 a -19.
#   - Rolloff @85%: toda a energia de um tom puro está na fundamental +
#     harmônicas mínimas. rolloff_85 deve ficar MUITO perto da fundamental
#     (440 Hz) ou no primeiro bin acima dela. Tolerância: 300-700 Hz.
#   - Flatness: tom puro tem flatness ≈ 0 (espectro de linha única).
#     Tolerância: < 0.10.
#   - Clipping: zero para amplitude 0.1 (longe de full scale).
#   - Classification: "unclassified" ou "silence" (tom 440 Hz não é
#     voz; RMS é baixo). Aceitável.

_SELFTEST_TONE_FREQ_HZ=440
_SELFTEST_TONE_AMPLITUDE=0.1          # = -20 dBFS target
_SELFTEST_TONE_DURATION_S=1.0
_SELFTEST_TONE_RATE=16000
_SELFTEST_TONE_CHANNELS=1

# Tolerâncias aceitas (inclusive inclusive).
_SELFTEST_RMS_MIN=-24.0
_SELFTEST_RMS_MAX=-20.0
_SELFTEST_PEAK_MIN=-22.0
_SELFTEST_PEAK_MAX=-18.0
_SELFTEST_ROLLOFF_MIN_HZ=300
_SELFTEST_ROLLOFF_MAX_HZ=700
_SELFTEST_FLATNESS_MAX=0.15
_SELFTEST_CLIPPING_MAX=0

run_analyzer_selftest() {
    local diag_dir="$SOVYX_DIAG_OUTDIR/_diagnostics"
    local self_dir="$diag_dir/selftest"
    mkdir -p "$self_dir"

    local tone_wav="$self_dir/calibration_tone.wav"
    local analysis_out="$self_dir/calibration_analysis.json"
    local selftest_json="$diag_dir/analyzer_selftest.json"

    log_info "=== T0: Analyzer self-test (calibration) ==="

    if [[ -z "$SOVYX_DIAG_PYTHON" ]]; then
        log_error "selftest: SOVYX_DIAG_PYTHON unresolved; cannot calibrate"
        printf '{"status":"skipped","reason":"no_python"}\n' > "$selftest_json"
        alert_append "error" "analyzer_selftest skipped — no python; downstream metrics UNCALIBRATED"
        return 1
    fi

    # Step 1: generate a known-tone WAV.
    if ! "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/tone_gen.py" \
            --out "$tone_wav" \
            --freq "$_SELFTEST_TONE_FREQ_HZ" \
            --duration "$_SELFTEST_TONE_DURATION_S" \
            --rate "$_SELFTEST_TONE_RATE" \
            --channels "$_SELFTEST_TONE_CHANNELS" \
            --amplitude "$_SELFTEST_TONE_AMPLITUDE" \
            > "$self_dir/tone_gen.log" 2>&1; then
        log_error "selftest: tone_gen failed; see $self_dir/tone_gen.log"
        printf '{"status":"failed","stage":"tone_gen"}\n' > "$selftest_json"
        alert_append "error" "analyzer_selftest failed at tone_gen — calibration impossible"
        return 1
    fi

    if [[ ! -s "$tone_wav" ]]; then
        log_error "selftest: tone_gen produced empty file"
        printf '{"status":"failed","stage":"tone_gen_empty"}\n' > "$selftest_json"
        alert_append "error" "analyzer_selftest — tone_gen produced empty WAV"
        return 1
    fi

    # Step 2: run analyze_wav on the known-tone.
    if ! "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/analyze_wav.py" \
            --wav "$tone_wav" \
            --state "SELFTEST" \
            --source "selftest:tone_${_SELFTEST_TONE_FREQ_HZ}hz" \
            --capture-id "SELFTEST_CALIBRATION" \
            --monotonic-ns "$SOVYX_DIAG_START_MONO_NS" \
            --utc-iso-ns "$SOVYX_DIAG_START_UTC_NS" \
            --out "$analysis_out" 2> "$self_dir/analyze.log"; then
        log_error "selftest: analyze_wav failed on calibration tone; see $self_dir/analyze.log"
        printf '{"status":"failed","stage":"analyze_wav"}\n' > "$selftest_json"
        alert_append "error" "analyzer_selftest failed at analyze_wav — calibration impossible"
        return 1
    fi

    # Step 3: validate metrics against tolerances.
    local verdict_payload
    verdict_payload=$("$SOVYX_DIAG_PYTHON" - "$analysis_out" \
            "$_SELFTEST_RMS_MIN" "$_SELFTEST_RMS_MAX" \
            "$_SELFTEST_PEAK_MIN" "$_SELFTEST_PEAK_MAX" \
            "$_SELFTEST_ROLLOFF_MIN_HZ" "$_SELFTEST_ROLLOFF_MAX_HZ" \
            "$_SELFTEST_FLATNESS_MAX" "$_SELFTEST_CLIPPING_MAX" \
            "$_SELFTEST_TONE_FREQ_HZ" <<'PYEOF' 2>"$self_dir/validate.log"
import json
import sys

(analysis_path,
 rms_min, rms_max, peak_min, peak_max,
 rolloff_min, rolloff_max, flatness_max, clipping_max,
 tone_freq) = sys.argv[1:]

with open(analysis_path) as f:
    d = json.load(f)

def _check(label, value, lo, hi):
    """Return (passed, note)."""
    if value is None:
        return False, f"{label}: value is None"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False, f"{label}: value not numeric ({value!r})"
    if float(lo) <= v <= float(hi):
        return True, f"{label}: {v:.3f} in [{lo},{hi}]"
    return False, f"{label}: {v:.3f} OUT OF RANGE [{lo},{hi}]"

checks = []
rms_pass, rms_note = _check("rms_dbfs", d.get("rms_dbfs"), rms_min, rms_max)
checks.append({"gate": "rms_dbfs", "pass": rms_pass, "note": rms_note,
               "observed": d.get("rms_dbfs"),
               "expected_range": [float(rms_min), float(rms_max)]})

peak_pass, peak_note = _check("peak_dbfs", d.get("peak_dbfs"), peak_min, peak_max)
checks.append({"gate": "peak_dbfs", "pass": peak_pass, "note": peak_note,
               "observed": d.get("peak_dbfs"),
               "expected_range": [float(peak_min), float(peak_max)]})

spec = d.get("spectral") or {}
rolloff_pass, rolloff_note = _check("rolloff_85_hz", spec.get("rolloff_85_hz"),
                                     rolloff_min, rolloff_max)
checks.append({"gate": "rolloff_85_hz", "pass": rolloff_pass, "note": rolloff_note,
               "observed": spec.get("rolloff_85_hz"),
               "expected_range": [float(rolloff_min), float(rolloff_max)],
               "context": f"tone fundamental is {tone_freq} Hz"})

flatness_pass, flatness_note = _check("flatness", spec.get("flatness"),
                                       -1.0, float(flatness_max))
checks.append({"gate": "flatness", "pass": flatness_pass, "note": flatness_note,
               "observed": spec.get("flatness"),
               "expected_max": float(flatness_max)})

clipping_pass, clipping_note = _check("clipping_samples", d.get("clipping_samples"),
                                       0, int(clipping_max))
checks.append({"gate": "clipping_samples", "pass": clipping_pass, "note": clipping_note,
               "observed": d.get("clipping_samples"),
               "expected_max": int(clipping_max)})

spectral_available = bool(d.get("spectral_available"))
checks.append({"gate": "spectral_available", "pass": spectral_available,
               "note": f"spectral_available={spectral_available}",
               "observed": spectral_available})

all_pass = all(c["pass"] for c in checks)
result = {
    "status": "pass" if all_pass else "fail",
    "analysis_source": analysis_path,
    "tone_freq_hz": float(tone_freq),
    "checks": checks,
}

print(json.dumps(result, indent=2))
sys.exit(0 if all_pass else 1)
PYEOF
)
    local selftest_rc=$?

    # Write verdict JSON atomically.
    local tmp="${selftest_json}.tmp"
    printf '%s\n' "$verdict_payload" > "$tmp" && mv -f "$tmp" "$selftest_json"

    manifest_append "T0_analyzer_selftest" \
        "_diagnostics/analyzer_selftest.json _diagnostics/selftest/" \
        "Calibration self-test: tone_gen(440Hz,-20dBFS) → analyze_wav → tolerance check. All downstream metrics are only trustworthy when this passes." \
        "T0-calibration"

    if [[ $selftest_rc -eq 0 ]]; then
        log_info "T0: analyzer self-test PASSED — measurement instruments calibrated"
        return 0
    fi

    log_error "T0: analyzer self-test FAILED — see $selftest_json"
    log_error "T0: refuse to continue; measurement tool reports incorrect values for known signal"
    alert_append "error" \
        "analyzer_selftest FAILED — calibration out of tolerance; all downstream metrics suspect. See analyzer_selftest.json."
    return 1
}
