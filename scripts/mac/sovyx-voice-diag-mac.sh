#!/usr/bin/env bash
# sovyx-voice-diag-mac — bateria forense de voz/audio Sovyx em macOS.
#
# Espelha o layout do toolkit Linux (sovyx-voice-diag) com 15 camadas
# (A-O), reusando os mesmos analyzers Python (analyze_wav.py +
# silero_probe.py + tone_gen.py + wav_header.py).
#
# Uso:
#   bash sovyx-voice-diag-mac.sh [--with-sysdiagnose] [--skip-captures]
#                                 [--non-interactive] [--yes]
#                                 [--outdir DIR]
#
# Output: ~/sovyx-diag-mac-<host>-<ts>-<uuid>.tar.gz

set -uo pipefail
shopt -s nullglob

# ────────────────────────────────────────────────────────────────────
# Args + paths (parsed BEFORE OS check so --help works on any OS)
# ────────────────────────────────────────────────────────────────────

OUTDIR=""
WITH_SYSDIAGNOSE=0
SKIP_CAPTURES=0
NON_INTERACTIVE=0
YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --outdir)            OUTDIR="$2"; shift 2 ;;
        --with-sysdiagnose)  WITH_SYSDIAGNOSE=1; shift ;;
        --skip-captures)     SKIP_CAPTURES=1; shift ;;
        --non-interactive)   NON_INTERACTIVE=1; shift ;;
        --yes)               YES=1; shift ;;
        -h|--help)
            grep -E '^#( |$)' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown flag: $1" >&2; exit 2 ;;
    esac
done

# OS check AFTER args so --help is reachable on any platform.
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "sovyx-voice-diag-mac só roda em macOS. Detectado: $(uname -s)" >&2
    exit 2
fi

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS=$(date -u +%Y%m%dT%H%M%SZ)
HOST=$(hostname -s 2>/dev/null || echo unknown)
UUID=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])' 2>/dev/null || echo 00000000)
[[ -z "$OUTDIR" ]] && OUTDIR="$HOME/sovyx-diag-mac-${HOST}-${TS}-${UUID}"
mkdir -p "$OUTDIR"

TOOL_VERSION="1.0"
RUNLOG="$OUTDIR/RUNLOG.txt"
SUMMARY="$OUTDIR/SUMMARY.json"
MANIFEST="$OUTDIR/MANIFEST.md"

log_info()  { printf '[%s] [INFO]  %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$RUNLOG" >&2; }
log_warn()  { printf '[%s] [WARN]  %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$RUNLOG" >&2; }
log_error() { printf '[%s] [ERROR] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$RUNLOG" >&2; }

run_step() {
    # run_step <step_id> <out_path> <timeout_s> <cmd...>
    local step_id="$1" out_path="$2" timeout_s="$3"
    shift 3
    mkdir -p "$(dirname "$out_path")"
    local start end
    start=$(date -u +%s.%N)
    # macOS lacks GNU `timeout` by default; use `gtimeout` if installed
    # via coreutils, fallback to Perl-based timeout.
    if command -v gtimeout >/dev/null 2>&1; then
        gtimeout --preserve-status --kill-after=5 "$timeout_s" "$@" \
            > "$out_path" 2>&1
    elif command -v timeout >/dev/null 2>&1; then
        timeout --preserve-status --kill-after=5 "$timeout_s" "$@" \
            > "$out_path" 2>&1
    else
        perl -e "alarm shift @ARGV; exec @ARGV" "$timeout_s" "$@" \
            > "$out_path" 2>&1
    fi
    local rc=$?
    end=$(date -u +%s.%N)
    local elapsed
    elapsed=$(awk -v s="$start" -v e="$end" 'BEGIN{printf "%.3f", e-s}')
    printf '[%s] step=%s rc=%s elapsed=%ss out=%s\n' \
        "$(date -u +%H:%M:%S)" "$step_id" "$rc" "$elapsed" "$out_path" \
        >> "$RUNLOG"
    return "$rc"
}

manifest_append() {
    local step="$1" path="$2" purpose="$3"
    {
        printf -- '- **%s** — `%s`\n' "$step" "$path"
        printf '    %s\n' "$purpose"
    } >> "$MANIFEST"
}

# Find Python in Sovyx venv (pipx, pip user, dev install).
find_sovyx_python() {
    local candidates=(
        "$HOME/.local/pipx/venvs/sovyx/bin/python"
        "$HOME/.local/pipx/venvs/sovyx/bin/python3"
        "$HOME/.local/share/pipx/venvs/sovyx/bin/python"
        "$HOME/Library/pipx/venvs/sovyx/bin/python"
    )
    for p in "${candidates[@]}"; do
        [[ -x "$p" ]] && { echo "$p"; return 0; }
    done
    # Fallback to system python3.
    command -v python3 || true
}
SOVYX_PYTHON=$(find_sovyx_python)

# ────────────────────────────────────────────────────────────────────
# Consent prompt
# ────────────────────────────────────────────────────────────────────

if [[ $YES -ne 1 ]] && [[ $NON_INTERACTIVE -ne 1 ]]; then
    cat <<EOF >&2
This will collect ~10-50 MB of diagnostic data about your Mac's audio
stack into:
    $OUTDIR

Includes: system_profiler dump, IOReg audio devices, CoreAudio engine
state, HAL plug-ins, TCC microphone consents (if Full Disk Access),
sounddevice probe, sovyx config, and live mic capture (5s).

NO secrets are uploaded; no settings are changed.

Press ENTER to continue, Ctrl+C to abort.
EOF
    read -r _
fi

# Init MANIFEST + SUMMARY + RUNLOG.
{
    echo "# sovyx-voice-diag-mac MANIFEST"
    echo ""
    echo "Tool version: $TOOL_VERSION"
    echo "Started:      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Host:         $HOST"
    echo "macOS:        $(sw_vers -productVersion 2>/dev/null || echo unknown)"
    echo "Outdir:       $OUTDIR"
    echo ""
    echo "## Artifacts"
    echo ""
} > "$MANIFEST"
: > "$RUNLOG"

# ────────────────────────────────────────────────────────────────────
# A — Hardware
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer A: hardware ==="
DIR_A="$OUTDIR/A_hardware"
mkdir -p "$DIR_A"

run_step "A_system_profiler_audio" "$DIR_A/system_profiler_audio.json" 30 \
    system_profiler -json SPAudioDataType
run_step "A_system_profiler_hw" "$DIR_A/system_profiler_hardware.json" 30 \
    system_profiler -json SPHardwareDataType
run_step "A_system_profiler_usb" "$DIR_A/system_profiler_usb.json" 30 \
    system_profiler -json SPUSBDataType
run_step "A_system_profiler_bluetooth" "$DIR_A/system_profiler_bluetooth.json" 30 \
    system_profiler -json SPBluetoothDataType
run_step "A_ioreg_audiodevice" "$DIR_A/ioreg_audiodevice.txt" 30 \
    bash -c 'ioreg -lw0 -r -c IOAudioDevice 2>&1 || echo "(ioreg failed)"'
run_step "A_ioreg_audioengine" "$DIR_A/ioreg_audioengine.txt" 30 \
    bash -c 'ioreg -lw0 -r -c IOAudioEngine 2>&1 || echo "(ioreg failed)"'
manifest_append "A_layer" "A_hardware/" \
    "Hardware enumeration: system_profiler (audio/hw/usb/bluetooth) + ioreg audio classes."

# ────────────────────────────────────────────────────────────────────
# B — Kernel/OS
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer B: kernel/OS ==="
DIR_B="$OUTDIR/B_kernel"
mkdir -p "$DIR_B"

run_step "B_sw_vers" "$DIR_B/sw_vers.txt" 5 sw_vers
run_step "B_uname" "$DIR_B/uname.txt" 5 uname -a
run_step "B_softwareupdate_history" "$DIR_B/softwareupdate_history.txt" 30 \
    bash -c 'softwareupdate --history --all 2>&1 || echo "(softwareupdate failed)"'
run_step "B_kmutil_loaded" "$DIR_B/kmutil_loaded.txt" 30 \
    bash -c 'kmutil showloaded --collection auxiliary --collection system 2>&1 || echo "(kmutil failed)"'
run_step "B_systemextensionsctl_list" "$DIR_B/systemextensionsctl_list.txt" 15 \
    bash -c 'systemextensionsctl list 2>&1 || echo "(systemextensionsctl failed)"'
run_step "B_csrutil_status" "$DIR_B/csrutil_status.txt" 5 \
    bash -c 'csrutil status 2>&1 || echo "(csrutil failed)"'
run_step "B_nvram_boot_args" "$DIR_B/nvram_boot_args.txt" 5 \
    bash -c 'nvram boot-args 2>&1 || echo "(nvram failed)"'
run_step "B_pkgutil_audio_pkgs" "$DIR_B/pkgutil_audio_pkgs.txt" 30 \
    bash -c 'pkgutil --pkgs 2>/dev/null | grep -iE "audio|coreaudio|driverkit" || echo "(no audio pkgs detected)"'
manifest_append "B_layer" "B_kernel/" \
    "OS/kernel: sw_vers + softwareupdate history (correlate audio breakage with macOS update) + kmutil + system extensions."

# ────────────────────────────────────────────────────────────────────
# C — CoreAudio engine (via Swift dumper)
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer C: CoreAudio engine ==="
DIR_C="$OUTDIR/C_coreaudio"
mkdir -p "$DIR_C"

if command -v swift >/dev/null 2>&1 && [[ -r "$SCRIPT_DIR/coreaudio-dump.swift" ]]; then
    run_step "C_coreaudio_dump" "$DIR_C/coreaudio_dump.json" 60 \
        swift "$SCRIPT_DIR/coreaudio-dump.swift"
    manifest_append "C_coreaudio_dump" "C_coreaudio/coreaudio_dump.json" \
        "Full CoreAudio device + stream + format enumeration via Swift CLI. Equivalente do pw-dump no Linux."
else
    echo "Swift unavailable or coreaudio-dump.swift missing" \
        > "$DIR_C/coreaudio_dump_SKIPPED.txt"
    log_warn "C_coreaudio: swift or dump script missing"
fi

# Default device names via SwitchAudioSource (homebrew) if available.
if command -v SwitchAudioSource >/dev/null 2>&1; then
    run_step "C_default_input"  "$DIR_C/default_input.txt"  10 \
        SwitchAudioSource -t input -c
    run_step "C_default_output" "$DIR_C/default_output.txt" 10 \
        SwitchAudioSource -t output -c
fi

# ────────────────────────────────────────────────────────────────────
# D — coreaudiod daemon + HAL plug-ins
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer D: coreaudiod + HAL plug-ins ==="
DIR_D="$OUTDIR/D_coreaudiod"
mkdir -p "$DIR_D"

run_step "D_launchctl_print" "$DIR_D/launchctl_coreaudiod.txt" 15 \
    bash -c 'launchctl print system/com.apple.audio.coreaudiod 2>&1 || launchctl list com.apple.audio.coreaudiod 2>&1'
run_step "D_ps_audio" "$DIR_D/ps_audio_processes.txt" 10 \
    bash -c 'ps -o pid,rss,%cpu,stat,etime,command -ax | grep -iE "coreaudiod|audio" | grep -v grep'

# HAL plug-ins via classifier script.
if [[ -r "$SCRIPT_DIR/hal-plugin-classifier.sh" ]]; then
    run_step "D_hal_classifier" "$DIR_D/hal_classifier.json" 60 \
        bash "$SCRIPT_DIR/hal-plugin-classifier.sh"
    manifest_append "D_hal_classifier" "D_coreaudiod/hal_classifier.json" \
        "HAL plug-ins + AU components + system extensions classified into known vendors (BlackHole, Loopback, Krisp, etc.). MACOS ANALOG do APO catalog Windows."
fi

# AU pluginkit listing.
run_step "D_pluginkit_au" "$DIR_D/pluginkit_audiounit.txt" 30 \
    bash -c 'pluginkit -mAvvv -p com.apple.audio.unit.effect 2>&1 || echo "(pluginkit failed)"'

# ────────────────────────────────────────────────────────────────────
# E — PortAudio / sounddevice
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer E: PortAudio / sounddevice ==="
DIR_E="$OUTDIR/E_portaudio"
mkdir -p "$DIR_E"

if [[ -n "$SOVYX_PYTHON" ]]; then
    run_step "E_sounddevice_query" "$DIR_E/sounddevice_query.json" 20 \
        "$SOVYX_PYTHON" -c '
import json, sys
try:
    import sounddevice as sd
    out = {
        "ok": True,
        "version": sd.__version__,
        "lib_name": sd._lib._name if hasattr(sd, "_lib") else None,
        "host_apis": list(sd.query_hostapis()),
        "devices": list(sd.query_devices()),
        "default_input_index": sd.default.device[0] if sd.default.device else None,
        "default_output_index": sd.default.device[1] if sd.default.device else None,
    }
    print(json.dumps(out, indent=2, default=str))
except Exception as e:
    print(json.dumps({"ok": False, "error": repr(e)}))
'
    run_step "E_pip_list" "$DIR_E/pip_list.json" 30 \
        "$SOVYX_PYTHON" -m pip list --format=json
else
    log_warn "E layer: SOVYX_PYTHON unresolved"
fi

# ────────────────────────────────────────────────────────────────────
# F — Session / TCC / signing
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer F: TCC + entitlements ==="
DIR_F="$OUTDIR/F_session"
mkdir -p "$DIR_F"

# TCC.db reader (FDA-aware).
if [[ -r "$SCRIPT_DIR/tcc-mic-reader.py" ]]; then
    run_step "F_tcc_mic" "$DIR_F/tcc_mic_consents.json" 15 \
        python3 "$SCRIPT_DIR/tcc-mic-reader.py"
fi

# codesign + entitlements de Python + sovyx binary.
if [[ -n "$SOVYX_PYTHON" ]]; then
    run_step "F_codesign_python" "$DIR_F/codesign_python.txt" 10 \
        bash -c "codesign -dv --verbose=4 '$SOVYX_PYTHON' 2>&1"
    run_step "F_entitlements_python" "$DIR_F/entitlements_python.xml" 10 \
        bash -c "codesign -d --entitlements :- '$SOVYX_PYTHON' 2>&1"
fi
SOVYX_BIN=$(command -v sovyx 2>/dev/null || echo "")
if [[ -n "$SOVYX_BIN" ]]; then
    run_step "F_codesign_sovyx" "$DIR_F/codesign_sovyx.txt" 10 \
        bash -c "codesign -dv --verbose=4 '$SOVYX_BIN' 2>&1"
fi

# AudioMIDISetup prefs.
run_step "F_audiomidisetup_prefs" "$DIR_F/audiomidisetup_prefs.json" 5 \
    bash -c 'plutil -convert json -o - ~/Library/Preferences/com.apple.audio.AudioMIDISetup.plist 2>/dev/null || echo "{}"'

# Mic input volume.
run_step "F_mic_volume" "$DIR_F/mic_volume.txt" 5 \
    bash -c 'osascript -e "input volume of (get volume settings)" 2>&1'

# ────────────────────────────────────────────────────────────────────
# G — Sovyx runtime
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer G: Sovyx runtime ==="
DIR_G="$OUTDIR/G_sovyx"
mkdir -p "$DIR_G"

run_step "G_sovyx_version" "$DIR_G/version.txt" 15 \
    bash -c 'sovyx --version 2>&1; echo ""; which sovyx; readlink -f "$(which sovyx)" 2>/dev/null'
run_step "G_doctor_voice" "$DIR_G/doctor_voice.txt" 60 \
    bash -c 'sovyx doctor voice --json 2>&1 || sovyx doctor voice 2>&1'

# Sovyx data dir snapshot.
SOVYX_DATA="$HOME/.sovyx"
if [[ -d "$SOVYX_DATA" ]]; then
    for f in voice/capture_combos.json voice/capture_overrides.json \
              voice/endpoint_quarantine.json system.yaml; do
        if [[ -r "$SOVYX_DATA/$f" ]]; then
            cp "$SOVYX_DATA/$f" "$DIR_G/$(basename "$f")"
        fi
    done
fi
if [[ -r "$SOVYX_DATA/logs/sovyx.log" ]]; then
    tail -n 5000 "$SOVYX_DATA/logs/sovyx.log" > "$DIR_G/sovyx_log_tail.txt"
fi

# ────────────────────────────────────────────────────────────────────
# H/W — Live capture (skipped if --skip-captures)
# ────────────────────────────────────────────────────────────────────

if [[ $SKIP_CAPTURES -eq 0 ]] && [[ -n "$SOVYX_PYTHON" ]]; then
    log_info "=== Layer W: live capture (5s) ==="
    DIR_W="$OUTDIR/W_capture"
    mkdir -p "$DIR_W"

    if [[ $NON_INTERACTIVE -eq 0 ]]; then
        printf '\n>>> Speak naturally for ~5s: "Sovyx, me ouça agora: um, dois, três, quatro, cinco."\n' >&2
        printf '    Press ENTER when ready (timeout 30s)\n' >&2
        read -t 30 -r _ || true
    fi

    # Capture via sounddevice (mirrors prod).
    run_step "W_capture_sounddevice" "$DIR_W/capture.wav.log" 20 \
        "$SOVYX_PYTHON" - <<'PYEOF'
import sounddevice as sd
import wave, sys, time
RATE = 16000
DUR = 5
print(f"recording {DUR}s @ {RATE}Hz mono...", file=sys.stderr)
rec = sd.rec(int(DUR * RATE), samplerate=RATE, channels=1, dtype='int16')
sd.wait()
import os
wav_path = os.path.join(os.environ.get("DIR_W", "."), "capture.wav")
with wave.open(wav_path, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
    w.writeframes(rec.tobytes())
print(f"wrote {wav_path}, {len(rec)} samples", file=sys.stderr)
PYEOF

    # Analyze (analyze_wav.py + silero_probe.py).
    if [[ -r "$DIR_W/capture.wav" ]]; then
        if [[ -r "$SCRIPT_DIR/analyze_wav.py" ]]; then
            run_step "W_analyze_wav" "$DIR_W/analysis.json" 30 \
                "$SOVYX_PYTHON" "$SCRIPT_DIR/analyze_wav.py" \
                    --wav "$DIR_W/capture.wav" --state "S_ACTIVE" \
                    --source "coreaudio_default" --capture-id "W_mac_default" \
                    --monotonic-ns "$(python3 -c 'import time; print(time.monotonic_ns())')" \
                    --utc-iso-ns "$(date -u +%Y-%m-%dT%H:%M:%S.000000000Z)" \
                    --out "$DIR_W/analysis.json"
        fi
        if [[ -r "$SCRIPT_DIR/silero_probe.py" ]]; then
            run_step "W_silero_probe" "$DIR_W/silero.json" 30 \
                "$SOVYX_PYTHON" "$SCRIPT_DIR/silero_probe.py" \
                    --wav "$DIR_W/capture.wav" --out "$DIR_W/silero.json"
        fi
    fi
fi

# ────────────────────────────────────────────────────────────────────
# I — Network (LLM endpoints)
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer I: network ==="
DIR_I="$OUTDIR/I_network"
mkdir -p "$DIR_I"

{
    for host in api.anthropic.com api.openai.com generativelanguage.googleapis.com api.deepgram.com api.elevenlabs.io; do
        echo "=== $host ==="
        dig +short "$host" 2>&1 | head -5
        nc -zv -w 3 "$host" 443 2>&1
        echo ""
    done
} > "$DIR_I/llm_endpoints.txt" 2>&1

# Application Firewall.
run_step "I_app_firewall" "$DIR_I/app_firewall.txt" 10 \
    bash -c '/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>&1'

# ────────────────────────────────────────────────────────────────────
# K — Unified logs
# ────────────────────────────────────────────────────────────────────

log_info "=== Layer K: unified logs ==="
DIR_K="$OUTDIR/K_logs"
mkdir -p "$DIR_K"

run_step "K_log_coreaudio" "$DIR_K/log_coreaudio.txt" 60 \
    bash -c 'log show --last 15m --predicate "subsystem == \"com.apple.coreaudio\"" --style compact 2>&1 | head -2000'
run_step "K_log_audiotools" "$DIR_K/log_audiotools.txt" 60 \
    bash -c 'log show --last 15m --predicate "process == \"coreaudiod\" OR sender == \"AudioToolbox\"" --style compact 2>&1 | head -2000'

# Crash reports.
run_step "K_crashreports_user" "$DIR_K/crashreports_user.txt" 10 \
    bash -c 'ls -la ~/Library/Logs/DiagnosticReports/ 2>/dev/null | grep -iE "coreaudiod|sovyx|python" || echo "(none)"'
run_step "K_crashreports_system" "$DIR_K/crashreports_system.txt" 10 \
    bash -c 'ls -la /Library/Logs/DiagnosticReports/ 2>/dev/null | grep -iE "coreaudiod|sovyx|python" || echo "(none)"'

# ────────────────────────────────────────────────────────────────────
# O — sysdiagnose (opt-in, 200MB)
# ────────────────────────────────────────────────────────────────────

if [[ $WITH_SYSDIAGNOSE -eq 1 ]]; then
    log_info "=== Layer O: sysdiagnose (HEAVY ~200MB) ==="
    DIR_O="$OUTDIR/O_sysdiagnose"
    mkdir -p "$DIR_O"
    log_warn "Running sudo sysdiagnose -- you'll be prompted for your password."
    sudo sysdiagnose -f "$DIR_O" -A sysdiagnose -V . -b -u 2>&1 \
        | tee "$DIR_O/sysdiagnose.log" || true
fi

# ────────────────────────────────────────────────────────────────────
# Finalize: SUMMARY + tarball
# ────────────────────────────────────────────────────────────────────

log_info "=== Finalize ==="

cat > "$SUMMARY" <<EOF
{
  "schema_version": 1,
  "tool": "sovyx-voice-diag-mac",
  "tool_version": "$TOOL_VERSION",
  "captured_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": "$HOST",
  "macos_version": "$(sw_vers -productVersion 2>/dev/null || echo unknown)",
  "outdir": "$OUTDIR",
  "with_sysdiagnose": $WITH_SYSDIAGNOSE,
  "skip_captures": $SKIP_CAPTURES
}
EOF

# Checksum + tarball.
( cd "$OUTDIR" && find . -type f ! -name 'CHECKSUMS.sha256' ! -name '*.tar.gz' \
    -exec shasum -a 256 {} \; > CHECKSUMS.sha256 2>/dev/null )

TAR_PATH="${OUTDIR}.tar.gz"
( cd "$(dirname "$OUTDIR")" && tar czf "$TAR_PATH" "$(basename "$OUTDIR")" )
shasum -a 256 "$TAR_PATH" > "${TAR_PATH}.sha256"

log_info "Done. Tarball: $TAR_PATH"
log_info "SHA256:        $(cat "${TAR_PATH}.sha256" | awk '{print $1}')"

echo "$TAR_PATH"
