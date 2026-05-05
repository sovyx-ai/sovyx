#!/usr/bin/env bash
# lib/T_guardian.sh — Camada T: Temporal Guardian (background monitor).
#
# Objetivo (enterprise-grade)
# ---------------------------
# Bugs de voice em Linux frequentemente são INTERMITENTES:
#   - PipeWire module load/unload mid-run
#   - Kernel audio driver hot-reload (snd_hda_intel replace)
#   - USB hub glitch (device hotplug)
#   - Bluetooth profile switch (A2DP ↔ HSP/HFP)
#   - Suspend/resume signals
#   - dmesg ERROR/OOPS
#   - journalctl audit denials
#   - thermal throttle
#   - audiosrv/session-manager restart
#
# As camadas A-K tiram snapshots em momentos discretos. Se um evento
# raro acontece ENTRE snapshots, ele é **invisível**. O Guardian roda
# em background do T0 ao T_end, capturando streams contínuos que
# permitem reconstituir o que aconteceu momento-a-momento.
#
# Cada follower é um processo background que escreve para um arquivo
# com timestamp ISO-ns por linha. Cleanup on trap garante que
# processes são terminated antes de finalize_package.
#
# Os arquivos produzidos vão para _diagnostics/guardian/ e são
# primeira-classe forensic artifacts (checksummed, manifest-listed).

# Global registry of guardian PIDs so stop_guardian can terminate them.
declare -g SOVYX_DIAG_GUARDIAN_PIDS=()
declare -g SOVYX_DIAG_GUARDIAN_DIR=""

_guardian_mark_line() {
    # Helper para prepend ISO-ns + monotonic_ns a cada linha lida.
    # Lê stdin, escreve stdout com marker por linha.
    awk 'BEGIN{ OFS="\t" } { print strftime("%Y-%m-%dT%H:%M:%SZ", systime()), $0; fflush() }'
}

_guardian_start_dmesg_watch() {
    # AUDIT v3+ — dmesg -w segue o kernel ring buffer em tempo real.
    # Filtro: apenas linhas com significado para audio/usb/erros.
    # Stderr preserved em .err sibling.
    local out="$SOVYX_DIAG_GUARDIAN_DIR/dmesg_watch.log"
    local err="$SOVYX_DIAG_GUARDIAN_DIR/dmesg_watch.err"
    {
        printf '# guardian.dmesg_watch started at %s (monotonic_ns=%s)\n' \
            "$(now_utc_ns)" "$(now_monotonic_ns)" >> "$out"
        dmesg -Tw 2>"$err" \
            | stdbuf -oL grep --line-buffered -iE \
                'audio|snd_hda|pipewire|alsa|usb.*disconn|usb.*connect|oom|panic|oops|taint|thermal|throttl|iommu.*fault|firmware|xhci|bluetooth' \
            | stdbuf -oL awk '{ print strftime("%Y-%m-%dT%H:%M:%SZ"), $0; fflush() }' \
            >> "$out"
    } &
    SOVYX_DIAG_GUARDIAN_PIDS+=($!)
}

_guardian_start_journal_watch() {
    # journalctl -f filtered para session/audio/auth events.
    local out="$SOVYX_DIAG_GUARDIAN_DIR/journal_watch.log"
    local err="$SOVYX_DIAG_GUARDIAN_DIR/journal_watch.err"
    if ! command -v journalctl >/dev/null 2>&1; then
        echo "journalctl unavailable on this system" > "$out"
        return
    fi
    {
        printf '# guardian.journal_watch started at %s (monotonic_ns=%s)\n' \
            "$(now_utc_ns)" "$(now_monotonic_ns)" >> "$out"
        journalctl --follow --since "now" --output=short-iso-precise --no-pager 2>"$err" \
            | stdbuf -oL grep --line-buffered -iE \
                'pipewire|wireplumber|pulseaudio|audio|alsa|sound|systemd-logind|dbus|polkit|denied|apparmor|selinux|suspend|resume|sleep|wake|sovyx' \
            >> "$out"
    } &
    SOVYX_DIAG_GUARDIAN_PIDS+=($!)
}

_guardian_start_udev_watch() {
    # udevadm monitor captura kernel + udev events em real-time.
    # Detecta USB hotplug, ALSA card add/remove, Bluetooth pairing.
    local out="$SOVYX_DIAG_GUARDIAN_DIR/udev_watch.log"
    local err="$SOVYX_DIAG_GUARDIAN_DIR/udev_watch.err"
    if ! command -v udevadm >/dev/null 2>&1; then
        echo "udevadm unavailable on this system" > "$out"
        return
    fi
    {
        printf '# guardian.udev_watch started at %s (monotonic_ns=%s)\n' \
            "$(now_utc_ns)" "$(now_monotonic_ns)" >> "$out"
        udevadm monitor --udev --env --subsystem-match=sound \
            --subsystem-match=usb --subsystem-match=bluetooth 2>"$err" \
            | stdbuf -oL awk '{ print strftime("%Y-%m-%dT%H:%M:%SZ"), $0; fflush() }' \
            >> "$out"
    } &
    SOVYX_DIAG_GUARDIAN_PIDS+=($!)
}

_guardian_start_pw_dump_poll() {
    # pw-dump periódico (cada 10s) para detectar module load/unload.
    # Cada dump salvo com timestamp no nome; diff é computado post-run.
    if ! command -v pw-dump >/dev/null 2>&1; then
        echo "pw-dump unavailable on this system" > "$SOVYX_DIAG_GUARDIAN_DIR/pw_dump_poll.log"
        return
    fi
    local poll_dir="$SOVYX_DIAG_GUARDIAN_DIR/pw_dump_poll"
    mkdir -p "$poll_dir"
    {
        local tick=0
        while :; do
            local ts
            ts=$(date -u +%Y%m%dT%H%M%SZ)
            local mono
            mono=$(now_monotonic_ns 2>/dev/null || echo "NA")
            pw-dump --no-colors > "$poll_dir/pw_dump_${ts}_${mono}.json" 2>/dev/null || true
            tick=$((tick + 1))
            sleep 10
        done
    } &
    SOVYX_DIAG_GUARDIAN_PIDS+=($!)
}

_guardian_start_suspend_watch() {
    # Detecta entrada em suspend via /sys/power/state modification
    # (systemd emite PrepareForSleep via dbus mas não é trivial de
    # monitorar sem dbus-monitor; inotify em /run/systemd/suspend é
    # mais portável).
    local out="$SOVYX_DIAG_GUARDIAN_DIR/suspend_watch.log"
    if ! command -v inotifywait >/dev/null 2>&1; then
        echo "inotifywait unavailable — suspend/resume detection via guardian skipped; suspend will still be inferred from monotonic_ns gaps" \
            > "$out"
        return
    fi
    {
        printf '# guardian.suspend_watch started at %s (monotonic_ns=%s)\n' \
            "$(now_utc_ns)" "$(now_monotonic_ns)" >> "$out"
        # Monitor /run/systemd/ for suspend-related unit activity.
        inotifywait -m -q --format '%T %w%f %e' --timefmt '%Y-%m-%dT%H:%M:%SZ' \
            /run/systemd/ /sys/power/ 2>/dev/null \
            >> "$out"
    } &
    SOVYX_DIAG_GUARDIAN_PIDS+=($!)
}

_guardian_periodic_pulse() {
    # Emite pulso a cada 30s: monotonic_ns, load, fd_count of Sovyx,
    # pipewire client count. Permite detectar hangs silenciosos (se
    # pulsos param de aparecer, algo está travado).
    local out="$SOVYX_DIAG_GUARDIAN_DIR/periodic_pulse.log"
    {
        printf '# guardian.periodic_pulse started at %s\n' "$(now_utc_ns)" >> "$out"
        printf 'ts_iso_ns\tmonotonic_ns\tloadavg_1m\tsovyx_fd_count\tpipewire_client_count\n' >> "$out"
        while :; do
            local ts mono load fds pw_count
            ts=$(now_utc_ns 2>/dev/null || echo "NA")
            mono=$(now_monotonic_ns 2>/dev/null || echo "NA")
            load=$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo "NA")
            local sovyx_pid
            sovyx_pid=$(_sovyx_daemon_pids 2>/dev/null | head -1)
            if [[ -n "$sovyx_pid" && -d "/proc/$sovyx_pid/fd" ]]; then
                fds=$(ls "/proc/$sovyx_pid/fd" 2>/dev/null | wc -l)
            else
                fds="NA"
            fi
            # V4.3 fix: `cmd | grep -c X || echo "NA"` produz 2 linhas
            # ("0\nNA") quando cmd falha — grep -c emite "0" rc=1 E o
            # fallback dispara, corrompendo a TSV do periodic_pulse a
            # cada heartbeat. Capturar pactl rc separadamente.
            local pactl_out pactl_rc
            pactl_out=$(pactl list clients 2>/dev/null); pactl_rc=$?
            if [[ $pactl_rc -eq 0 ]]; then
                pw_count=$(printf '%s\n' "$pactl_out" | grep -c '^Client')
            else
                pw_count="NA"
            fi
            printf '%s\t%s\t%s\t%s\t%s\n' "$ts" "$mono" "$load" "$fds" "$pw_count" >> "$out"
            sleep 30
        done
    } &
    SOVYX_DIAG_GUARDIAN_PIDS+=($!)
}

start_guardian() {
    # Chamado pelo orchestrator DEPOIS de _init_common e ANTES da
    # primeira camada. Levanta os 6 followers em background.
    SOVYX_DIAG_GUARDIAN_DIR="$SOVYX_DIAG_OUTDIR/_diagnostics/guardian"
    mkdir -p "$SOVYX_DIAG_GUARDIAN_DIR"

    if [[ "${SOVYX_DIAG_FLAG_SKIP_GUARDIAN:-0}" = "1" ]]; then
        log_info "T_guardian: skipped via --skip-guardian flag"
        printf '{"status":"skipped","reason":"flag_skip_guardian"}\n' \
            > "$SOVYX_DIAG_GUARDIAN_DIR/guardian_status.json"
        return 0
    fi

    log_info "=== T0.5: Temporal Guardian — starting background monitors ==="

    _guardian_start_dmesg_watch
    _guardian_start_journal_watch
    _guardian_start_udev_watch
    _guardian_start_pw_dump_poll
    _guardian_start_suspend_watch
    _guardian_periodic_pulse

    # Record the PIDs and start-time for audit.
    {
        printf '{\n'
        printf '  "status": "started",\n'
        printf '  "started_utc_ns": "%s",\n' "$(now_utc_ns)"
        printf '  "started_monotonic_ns": %s,\n' "$(now_monotonic_ns 2>/dev/null || echo 0)"
        printf '  "followers": [\n'
        printf '    {"name":"dmesg_watch",      "pid": %s},\n' "${SOVYX_DIAG_GUARDIAN_PIDS[0]:-0}"
        printf '    {"name":"journal_watch",    "pid": %s},\n' "${SOVYX_DIAG_GUARDIAN_PIDS[1]:-0}"
        printf '    {"name":"udev_watch",       "pid": %s},\n' "${SOVYX_DIAG_GUARDIAN_PIDS[2]:-0}"
        printf '    {"name":"pw_dump_poll",     "pid": %s},\n' "${SOVYX_DIAG_GUARDIAN_PIDS[3]:-0}"
        printf '    {"name":"suspend_watch",    "pid": %s},\n' "${SOVYX_DIAG_GUARDIAN_PIDS[4]:-0}"
        printf '    {"name":"periodic_pulse",   "pid": %s}\n'  "${SOVYX_DIAG_GUARDIAN_PIDS[5]:-0}"
        printf '  ]\n'
        printf '}\n'
    } > "$SOVYX_DIAG_GUARDIAN_DIR/guardian_status.json"

    log_info "T_guardian: 6 followers armed (pids: ${SOVYX_DIAG_GUARDIAN_PIDS[*]})"

    manifest_append "T_guardian" "_diagnostics/guardian/" \
        "Temporal Guardian: 6 background followers capturing intermittent events (dmesg, journal, udev, pw-dump diff, suspend, periodic pulse). Critical for debugging bugs that strike BETWEEN discrete snapshots." \
        "temporal/intermittent"
}

stop_guardian() {
    # Chamado pelo trap EXIT (common.sh::_cleanup) ANTES de finalize.
    if [[ -z "$SOVYX_DIAG_GUARDIAN_DIR" ]]; then
        return 0
    fi
    if [[ ${#SOVYX_DIAG_GUARDIAN_PIDS[@]} -eq 0 ]]; then
        return 0
    fi

    log_info "T_guardian: stopping followers (${#SOVYX_DIAG_GUARDIAN_PIDS[@]} pids)"

    # Send SIGTERM, wait up to 3s each, then SIGKILL.
    for pid in "${SOVYX_DIAG_GUARDIAN_PIDS[@]}"; do
        [[ -z "$pid" || "$pid" = "0" ]] && continue
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    sleep 1
    for pid in "${SOVYX_DIAG_GUARDIAN_PIDS[@]}"; do
        [[ -z "$pid" || "$pid" = "0" ]] && continue
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done

    # Wait for zombies to be reaped.
    for pid in "${SOVYX_DIAG_GUARDIAN_PIDS[@]}"; do
        [[ -z "$pid" || "$pid" = "0" ]] && continue
        wait "$pid" 2>/dev/null || true
    done

    # Update status with stop info.
    local stopped_utc stopped_mono
    stopped_utc=$(now_utc_ns)
    stopped_mono=$(now_monotonic_ns 2>/dev/null || echo 0)
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$SOVYX_DIAG_GUARDIAN_DIR/guardian_status.json" "$stopped_utc" "$stopped_mono" \
            <<'PYEOF' 2>/dev/null || true
import json, os, pathlib, sys, tempfile
path, stopped_utc, stopped_mono = sys.argv[1:]
p = pathlib.Path(path)
try:
    d = json.loads(p.read_text())
except Exception:
    d = {}
d["stopped_utc_ns"] = stopped_utc
d["stopped_monotonic_ns"] = int(stopped_mono)
d["status"] = "stopped"
with tempfile.NamedTemporaryFile("w", delete=False, dir=str(p.parent),
                                  prefix=f".{p.name}.", suffix=".tmp") as t:
    t.write(json.dumps(d, indent=2))
    t.flush()
    os.fsync(t.fileno())
    tmp_path = t.name
os.replace(tmp_path, str(p))
PYEOF
    fi

    log_info "T_guardian: all followers stopped"
    SOVYX_DIAG_GUARDIAN_PIDS=()
}
