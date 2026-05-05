#!/usr/bin/env bash
# lib/alerts.sh — geração proativa de == ALERTAS == (§9 do plano).
#
# Varre os artefatos coletados e emite alertas em _diagnostics/alerts.jsonl
# que são consolidados no topo do MANIFEST.md.
#
# Chamado por finalize_package() antes da assemblagem do MANIFEST.

_alert_if_file_matches() {
    # Uso: _alert_if_file_matches <severity> <file> <regex> <message>
    local severity="$1" file="$2" regex="$3" msg="$4"
    [[ -r "$file" ]] || return
    if grep -qE "$regex" "$file" 2>/dev/null; then
        alert_append "$severity" "$msg"
    fi
}

_alert_captures_band_limited() {
    # Varre todos os analysis.json e alerta se rolloff_99_hz <= 1500.
    local outdir="$SOVYX_DIAG_OUTDIR"
    python3 - "$outdir" <<'PYEOF' 2>/dev/null
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
hits = []
for p in root.rglob("analysis.json"):
    try:
        d = json.loads(p.read_text())
    except Exception:
        continue
    sp = d.get("spectral") or {}
    roll = sp.get("rolloff_99_hz") or sp.get("rolloff_40db_hz")
    if roll and roll > 0 and roll <= 1500:
        hits.append(f"{p.relative_to(root)} rolloff={roll} rms={d.get('rms_dbfs')}")
if hits:
    print("WARN band_limited_voice across:")
    for h in hits:
        print(f"  - {h}")
PYEOF
}

_alert_filters_in_pw_dump() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local filters_json
    filters_json=$(find "$outdir" -name pw_dump_filters.json 2>/dev/null | head -1)
    [[ -n "$filters_json" && -s "$filters_json" ]] || return
    python3 - "$filters_json" <<'PYEOF' 2>/dev/null
import json, sys, pathlib
try:
    arr = json.loads(pathlib.Path(sys.argv[1]).read_text())
except Exception:
    sys.exit(0)
if arr:
    names = [item.get("name","?") for item in arr]
    print(f"WARN PipeWire DSP filters active: {names}")
PYEOF
}

_alert_destructive_modules_loaded() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local f
    for f in "$outdir"/states/S_*/pactl_modules.txt; do
        [[ -r "$f" ]] || continue
        local matches
        matches=$(grep -oE 'module-(echo-cancel|ladspa|filter-apply|rnnoise|webrtc-audio-processing|noise-cancel|virtual-source|remap-source)' "$f" 2>/dev/null | sort -u | paste -sd ',' -)
        if [[ -n "$matches" ]]; then
            local state
            state=$(basename "$(dirname "$f")")
            alert_append "warn" "destructive pactl modules loaded in $state: $matches"
        fi
    done
}

_alert_multi_sovyx_instances() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    # V4.1 pilot finding: prior regex `python.*sovyx|/sovyx` overcounted
    # because:
    #   - The diag script path (/sovyx-voice-diag.sh) matches `/sovyx`.
    #   - The log follower (`tail -F .../.sovyx/logs/sovyx.log`) matches.
    #   - Guardian followers match (their cmdlines contain `sovyx`).
    # Result on pilot: S_OFF reported count=14 when real daemon count was
    # 2-3. Fix: match ONLY actual Sovyx runtimes (pipx venv python or
    # sovyx binary entry), and EXCLUDE the script + follower artifacts.
    for f in "$outdir"/states/S_*/processes.txt; do
        [[ -r "$f" ]] || continue
        local count
        # INCLUDE: pipx venv python running sovyx, or the sovyx binary.
        # EXCLUDE: our own diag script, tail follower, guardian followers,
        # journalctl/dmesg followers, grep/awk/sed inside the script.
        count=$(grep -E 'pipx/venvs/sovyx/bin/python|/bin/sovyx[[:space:]]|/usr/local/bin/sovyx|/usr/bin/sovyx' "$f" 2>/dev/null \
                 | grep -vE 'sovyx-voice-diag|tail.*\.sovyx/logs|dmesg|journalctl|udevadm|inotifywait|pw-dump|grep|awk|sed' \
                 | wc -l)
        count="${count//[^0-9]/}"
        count="${count:-0}"
        if (( count > 1 )); then
            local state
            state=$(basename "$(dirname "$f")")
            alert_append "warn" "multiple Sovyx daemon instances in $state: count=$count (F6 — possible dual-launch)"
        fi
    done
}

_alert_clock_drift() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local drift_file="$outdir/I_network/clock_drift.txt"
    [[ -r "$drift_file" ]] || return
    local drift_line
    drift_line=$(grep -E '^drift_seconds=' "$drift_file" 2>/dev/null || echo "")
    [[ -z "$drift_line" ]] && return
    local drift
    drift=$(echo "$drift_line" | cut -d= -f2)

    # AUDIT v3 — the previous version used bash ``[[ ${drift#-} -gt 2 ]]``
    # which CRASHES SILENTLY for fractional values (drift=0.523 → "integer
    # expression expected", alert never emitted). NTP drift is routinely
    # reported as fractional seconds. Now: use awk for fractional-safe
    # absolute-value comparison, and also surface the raw drift in the
    # alert so the analyst can recompute.
    local abs_pass
    abs_pass=$(awk -v d="$drift" 'BEGIN{ if (d+0 < 0) d = -d; exit (d > 2 ? 0 : 1) }'; echo $?)
    if [[ "$abs_pass" = "0" ]]; then
        alert_append "warn" "system clock drifted by ${drift}s (|drift|>2s) — may break TLS and ntp-gated features"
    fi
}

_alert_kernel_tainted() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local tfile="$outdir/B_kernel/tainted.txt"
    [[ -r "$tfile" ]] || return
    local tv
    tv=$(grep -v '^#' "$tfile" 2>/dev/null | head -1)
    if [[ "$tv" =~ ^[0-9]+$ ]] && (( tv != 0 )); then
        alert_append "warn" "kernel tainted (flags=$tv) — may indicate broken DKMS/out-of-tree module"
    fi
}

_alert_recent_audio_updates() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local hist="$outdir/B_kernel/apt_history_recent.txt"
    [[ -r "$hist" ]] || return
    python3 - "$hist" <<'PYEOF' 2>/dev/null
import pathlib, re, sys, datetime as dt
txt = pathlib.Path(sys.argv[1]).read_text(errors="replace")
# apt history blocks are separated by blank lines; each has a Start-Date line.
events = re.split(r"\n(?=Start-Date:)", txt)
now = dt.datetime.utcnow()
critical_re = re.compile(r"(linux-image|pipewire|wireplumber|alsa|libasound|portaudio)")
for ev in events:
    m = re.search(r"Start-Date:\s*(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})", ev)
    if not m:
        continue
    try:
        ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        continue
    age_days = (now - ts).days
    if age_days <= 7 and critical_re.search(ev):
        # Emit a single alert line; main loop dedupes via message.
        pkgs = ",".join(set(critical_re.findall(ev)))
        print(f"audio-relevant apt upgrade {age_days}d ago ({pkgs})")
PYEOF
}

_alert_autosuspend_active() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local pfile="$outdir/B_kernel/power_states.txt"
    [[ -r "$pfile" ]] || return
    if grep -qE '^\s*control=auto' "$pfile" 2>/dev/null; then
        alert_append "info" "codec runtime PM in auto mode — may cause autosuspend (B2/B4)"
    fi
}

# V4.3 — Alerta cross-camada: se TODAS as capturas voice (não-silence)
# em E_portaudio/captures/W*/ tem rms_dbfs < -85 dBFS E silero max_prob
# < 0.01 → mic está morto/mute/destruído por APO upstream. Esta é a
# inferência mais acionável do toolkit. Sem ela, analyst tem que ler
# 5+ analysis.json + 5+ silero.json e correlacionar manualmente.
_alert_silence_across_layers() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    python3 - "$outdir" <<'PYEOF' 2>/dev/null
import json, pathlib, sys

root = pathlib.Path(sys.argv[1])
e_dir = root / "E_portaudio" / "captures"
if not e_dir.is_dir():
    sys.exit(0)

silence_threshold_dbfs = -85.0
vad_silent_threshold = 0.01

results = []
for cap_dir in sorted(e_dir.iterdir()):
    if not cap_dir.is_dir():
        continue
    cid = cap_dir.name
    # Excluir capturas de silêncio intencional (W14/W14c/etc).
    if "silence" in cid.lower():
        continue
    analysis = cap_dir / "analysis.json"
    silero = cap_dir / "silero.json"
    rms = None
    vad_max = None
    if analysis.is_file() and analysis.stat().st_size > 0:
        try:
            d = json.loads(analysis.read_text())
            rms = d.get("rms_dbfs")
        except Exception:
            pass
    if silero.is_file() and silero.stat().st_size > 0:
        try:
            d = json.loads(silero.read_text())
            if d.get("available", False):
                vad_max = d.get("max_prob")
        except Exception:
            pass
    results.append({"cid": cid, "rms": rms, "vad": vad_max})

if not results:
    sys.exit(0)

# Captura é "silenciosa" se ambos rms E vad indicam silêncio.
# Se VAD não disponível (None), exigir só rms.
silent_caps = []
for r in results:
    rms_silent = r["rms"] is not None and r["rms"] < silence_threshold_dbfs
    vad_silent = r["vad"] is None or (r["vad"] is not None and r["vad"] < vad_silent_threshold)
    if rms_silent and vad_silent:
        silent_caps.append(r)

if len(silent_caps) >= len(results) and len(results) >= 3:
    # Todas as capturas voice silenciosas — high-confidence "mic dead".
    cids = ",".join(r["cid"] for r in silent_caps)
    rms_summary = ",".join(f"{r['rms']:.1f}" if r['rms'] is not None else "?" for r in silent_caps)
    vad_summary = ",".join(f"{r['vad']:.3f}" if r['vad'] is not None else "?" for r in silent_caps)
    print(f"silence_across_default_source_captures: ALL {len(silent_caps)} voice captures silent (rms_dbfs={rms_summary}, vad_max_prob={vad_summary}, cids={cids}) — mic dead/muted/APO-destroyed (anti-pattern #21 Voice Clarity OR W11/W12/W13 source mismatch). Cross-check C_alsa W1-W4 to isolate ALSA layer.")
PYEOF
}

_alert_sovyx_fd_leak_residual() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    for state in S_RESIDUAL_t5 S_RESIDUAL_t30; do
        local f="$outdir/states/$state/sovyx_pid_fd_count.txt"
        [[ -r "$f" ]] || continue
        local count
        count=$(cat "$f" 2>/dev/null || echo 0)
        if [[ "$count" =~ ^[0-9]+$ ]] && (( count > 0 )); then
            alert_append "warn" "$state: Sovyx has $count fds open despite stop — possible leak"
        fi
    done
}

_alert_cascade_healthy_vs_pipeline_deaf() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    # Lê api_voice_capture_diagnostics.json (tem veredito do probe).
    local diag="$outdir/G_sovyx/api_voice_capture_diagnostics.json"
    [[ -r "$diag" ]] || return
    # E17 flag discrepância — se voice_clarity_active=true no diag MAS
    # dashboard Capture Health mostra "healthy" (combo_store), é discrepância.
    python3 - "$diag" "$outdir/G_sovyx/api_voice_health.json" <<'PYEOF' 2>/dev/null
import json, pathlib, sys
try:
    d = json.loads(pathlib.Path(sys.argv[1]).read_text())
except Exception:
    sys.exit(0)
if d.get("voice_clarity_active") or d.get("any_voice_clarity_active"):
    print("capture-APO active but pipeline may still report healthy (E17 discrepância)")
try:
    h = json.loads(pathlib.Path(sys.argv[2]).read_text())
except Exception:
    sys.exit(0)
for entry in h.get("combo_store", []):
    diag_last = entry.get("last_boot_diagnosis", "")
    if diag_last.lower() == "healthy" and entry.get("vad_max_prob_at_validation", 0) < 0.05:
        print(f"combo entry {entry.get('device_friendly_name')} healthy but vad_max={entry.get('vad_max_prob_at_validation')}")
PYEOF
}

_alert_frame_drops_in_live_log() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    for slice in "$outdir"/H_pipeline_live/live_pipeline_log_slice_round*.txt; do
        [[ -r "$slice" ]] || continue
        if grep -qE '"gap_ms"\s*:\s*[1-9][0-9]{2,}|voice\.gap_ms=[0-9]{3,}' "$slice" 2>/dev/null; then
            alert_append "warn" "frame-drops recurrent in $(basename "$slice") — jitter/scheduling (E12)"
        fi
    done
}

_alert_sink_muted() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local mute="$outdir/K_output/volumes_mute.txt"
    [[ -r "$mute" ]] || return
    if grep -qi "mute: yes" "$mute" 2>/dev/null; then
        alert_append "error" "default sink is MUTED — playback silent regardless of chain"
    fi
}

_alert_hdmi_as_default_sink() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local sink="$outdir/K_output/default_sink.txt"
    [[ -r "$sink" ]] || return

    # AUDIT v3 — the previous regex ``hdmi|digital`` false-positived
    # on "digital input" / "Digital In" mic devices, flagging perfectly
    # normal setups as having HDMI default sink. Tighten to HDMI-specific
    # patterns only, anchor to a sink-name line (pactl output format
    # `Name: <sink>`), and exclude known benign substrings.
    if grep -qE '^Name:.*(hdmi|HDMI|spdif|S/PDIF|DisplayPort|DP)' "$sink" 2>/dev/null; then
        alert_append "warn" "default sink is HDMI/SPDIF/DisplayPort — speakers likely silent if not connected"
    fi
}

_alert_bluetooth_source_default() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    for f in "$outdir"/states/S_*/pactl_sources.txt; do
        [[ -r "$f" ]] || continue
        if grep -qiE 'bluez_source|a2dp_source|hfp_source' "$f" 2>/dev/null; then
            alert_append "warn" "Bluetooth source available — may be selected as default in error"
        fi
    done
}

_alert_dkms_broken() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local dkms="$outdir/B_kernel/dkms_status.txt"
    [[ -r "$dkms" ]] || return
    if grep -qiE 'failed|broken|error' "$dkms" 2>/dev/null; then
        alert_append "warn" "DKMS module in failed/broken state — kernel taint source"
    fi
}

_alert_cascade_env_overrides() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local env="$outdir/G_sovyx/env_SOVYX_redacted.txt"
    [[ -r "$env" ]] || return
    # V4 Track B smoke finding: `grep -c ... 2>/dev/null || echo 0` produces
    # "0\n0" when grep returns rc=1 (no matches), because grep emits "0" AND
    # the fallback echoes "0". `(( count > 0 ))` on a 2-line value errors.
    # Fix: discard the grep rc entirely; grep -c always emits a count.
    local count
    count=$(grep -cE '^SOVYX_TUNING__VOICE' "$env" 2>/dev/null)
    count="${count:-0}"
    # Strip any trailing whitespace/newlines defensively.
    count="${count//[^0-9]/}"
    if (( count > 0 )); then
        alert_append "info" "$count SOVYX_TUNING__VOICE__* env overrides present — may mask defaults (G1)"
    fi
}

# Função pública chamada por finalize_package.
generate_alerts() {
    log_info "generating proactive alerts (§9 of plan)..."

    _alert_filters_in_pw_dump
    _alert_destructive_modules_loaded
    _alert_multi_sovyx_instances
    _alert_clock_drift
    _alert_kernel_tainted

    # Pipe em formato "linha por alerta" — cada linha vira um alert_append warn.
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        alert_append "warn" "$line"
    done < <(_alert_recent_audio_updates)

    _alert_autosuspend_active
    _alert_sovyx_fd_leak_residual

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        alert_append "warn" "$line"
    done < <(_alert_cascade_healthy_vs_pipeline_deaf)

    _alert_frame_drops_in_live_log
    _alert_sink_muted
    _alert_hdmi_as_default_sink
    _alert_bluetooth_source_default
    _alert_dkms_broken
    _alert_cascade_env_overrides

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        alert_append "warn" "$line"
    done < <(_alert_captures_band_limited)

    # V4.3 — silence cross W10-W14b é high-confidence error (mic dead/
    # APO-destroyed). Severity error porque triggera leitura imediata
    # do analyst (vs warns que se acumulam).
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        alert_append "error" "$line"
    done < <(_alert_silence_across_layers)
}
