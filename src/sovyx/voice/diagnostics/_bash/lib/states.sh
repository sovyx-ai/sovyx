#!/usr/bin/env bash
# lib/states.sh — máquina de estados + lifecycle Sovyx.
#
# Exporta: enter_S_OFF, enter_S_IDLE, enter_S_ACTIVE, enter_S_RESIDUAL_t5,
#          enter_S_RESIDUAL_t30, enter_S_POST_SUSPEND, enter_S_EXTERNAL_GRAB,
#          snapshot_state, generate_state_diffs.
#
# Cada função `enter_X` deixa o sistema no estado X e grava um snapshot
# completo em states/X/.
#
# Snapshot contém (por plano v2 §1.5):
#   timestamp.txt, processes.txt, pactl_*.txt, pw_dump.json, wpctl_status.txt,
#   lsof_snd.txt, fuser_snd.txt, ss_unix.txt, ss_tcp.txt, proc_asound*.txt,
#   sovyx_pid_*.txt, env_snapshot.txt, dbus_user_names.txt, systemd_user_units.txt

# ─────────────────────────────────────────────────────────────────────────
# Helper — transição
# ─────────────────────────────────────────────────────────────────────────

_transition_to() {
    local new_state="$1"
    SOVYX_DIAG_STATE="$new_state"
    log_info "=== STATE: $new_state ==="
}

# Espera até `sovyx_is_running` retornar true/false (parametrizado), com timeout.
# Uso: _wait_sovyx_state <yes|no> <timeout_s>
#
# AUDIT v3: antes usava ``SECONDS`` (wall-clock). Um ``chronyc burst``
# ou step do NTP durante o wait podia acelerar ``SECONDS`` e fazer o
# timeout disparar antes do tempo real — ``sovyx not started`` era
# reportado como falso negativo em redes recém-sincronizadas. Agora
# usa monotonic nanoseconds via ``now_monotonic_ns``, invariante a
# ajustes de relógio.
_wait_sovyx_state() {
    local want="$1" timeout_s="$2"
    local start_mono deadline now
    start_mono=$(now_monotonic_ns) || return 1
    # timeout_s * 1e9 nanoseconds
    deadline=$(awk -v s="$start_mono" -v t="$timeout_s" 'BEGIN{printf "%d", s + t * 1000000000}')
    while :; do
        if [[ "$want" = "yes" ]]; then
            sovyx_is_running && return 0
        else
            sovyx_is_running || return 0
        fi
        now=$(now_monotonic_ns) || return 1
        (( now >= deadline )) && return 1
        sleep 1
    done
}

# Envia POST ao dashboard. Uso: _api_post <path> [body_json] → stdout: body, retcode: http_ok
# Usa run_step_pipe para registrar.
_api_post() {
    local path="$1" body="${2:-{}}"
    local step_id="api_post_$(echo "$path" | tr '/ ' '__' | tr -d '{}:')"
    local out="$SOVYX_DIAG_OUTDIR/_diagnostics/api_calls/${step_id}.json"
    mkdir -p "$(dirname "$out")"

    [[ -z "$SOVYX_DIAG_TOKEN" ]] && return 1

    run_step_pipe "$step_id" "$out" "$SOVYX_DIAG_API_TIMEOUT" \
        curl -sS --max-time "$SOVYX_DIAG_API_TIMEOUT" \
             -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
             -H "Content-Type: application/json" \
             -X POST "http://127.0.0.1:7777$path" \
             -d "$body"
}

# ─────────────────────────────────────────────────────────────────────────
# Snapshot — grava árvore de estado em states/<NAME>/
# ─────────────────────────────────────────────────────────────────────────

snapshot_state() {
    local name="$1"
    local dir="$SOVYX_DIAG_OUTDIR/states/$name"
    mkdir -p "$dir"
    log_info "snapshot: $name"

    # Timestamp pair.
    now_pair_json > "$dir/timestamp.json"

    # Processos + Sovyx PID.
    run_step "snap_${name}_processes" "$dir/processes.txt" 10 \
        bash -c 'ps -ef; echo "---sovyx---"; pgrep -af sovyx || true'

    # Filtra pelo /proc/exe — nunca casa com o próprio script bash (anti-suicide).
    #
    # AUDIT v3 — critical fixes:
    #   1. Hypothesis F6 is "multiple Sovyx daemons"; the old
    #      ``head -1`` silently picked ONE arbitrary PID and reported
    #      all proc_status/fd_count for it. If there WERE multiple,
    #      the wrong one was documented. Now enumerate all.
    #   2. ``$sovyx_pid`` is interpolated into downstream ``bash -c
    #      "... /proc/$sovyx_pid/status ..."`` constructs. Without a
    #      strict ``^[0-9]+$`` validator, any upstream bug that leaks
    #      non-numeric content would enable command injection. Now
    #      validated before use.
    local sovyx_pids_list
    sovyx_pids_list=$(_sovyx_daemon_pids || true)
    echo "$sovyx_pids_list" > "$dir/sovyx_pids_all.txt"
    local sovyx_pid_count
    sovyx_pid_count=$(printf '%s\n' "$sovyx_pids_list" | grep -c '^[0-9]')
    echo "$sovyx_pid_count" > "$dir/sovyx_pid_count.txt"

    # Pick the LOWEST PID (typically the parent daemon) — deterministic
    # across runs instead of order-sensitive ``head -1``.
    local sovyx_pid=""
    sovyx_pid=$(printf '%s\n' "$sovyx_pids_list" | grep -E '^[0-9]+$' | sort -n | head -1)
    # Strict validation — defense in depth against any future caller
    # bug that might leak non-numeric content.
    if [[ -z "$sovyx_pid" ]] || ! [[ "$sovyx_pid" =~ ^[0-9]+$ ]]; then
        sovyx_pid=""
    fi
    echo "$sovyx_pid" > "$dir/sovyx_pid.txt"

    if (( sovyx_pid_count > 1 )); then
        alert_append "warn" \
            "Multiple Sovyx daemons detected during $name: $sovyx_pid_count processes (F6 hypothesis). First PID used for detailed snapshots: $sovyx_pid"
    fi

    # PipeWire — usa pactl/pw-dump/wpctl onde disponíveis.
    if command -v pactl >/dev/null 2>&1; then
        run_step "snap_${name}_pactl_info"           "$dir/pactl_info.txt"           10 pactl info
        run_step "snap_${name}_pactl_sources"        "$dir/pactl_sources.txt"        10 pactl list sources
        run_step "snap_${name}_pactl_sinks"          "$dir/pactl_sinks.txt"          10 pactl list sinks
        run_step "snap_${name}_pactl_source_outputs" "$dir/pactl_source-outputs.txt" 10 pactl list source-outputs
        run_step "snap_${name}_pactl_sink_inputs"    "$dir/pactl_sink-inputs.txt"    10 pactl list sink-inputs
        run_step "snap_${name}_pactl_modules"        "$dir/pactl_modules.txt"        10 pactl list modules
        run_step "snap_${name}_pactl_clients"        "$dir/pactl_clients.txt"        10 pactl list clients
    fi

    if command -v pw-dump >/dev/null 2>&1; then
        run_step_pipe "snap_${name}_pw_dump"  "$dir/pw_dump.json"  15 pw-dump --no-colors
    fi
    if command -v pw-top >/dev/null 2>&1; then
        run_step "snap_${name}_pw_top" "$dir/pw_top.txt" 10 pw-top -b -n 3
    fi
    if command -v pw-metadata >/dev/null 2>&1; then
        run_step "snap_${name}_pw_metadata"          "$dir/pw_metadata_full.txt"     10 pw-metadata 0
        run_step "snap_${name}_pw_metadata_settings" "$dir/pw_metadata_settings.txt" 10 pw-metadata -n settings 0
    fi

    if command -v wpctl >/dev/null 2>&1; then
        run_step "snap_${name}_wpctl_status" "$dir/wpctl_status.txt" 10 wpctl status
        # Inspect default source/sink se IDs resolvidos.
        if [[ -n "$SOVYX_DIAG_DEFAULT_SOURCE_ID" ]]; then
            run_step "snap_${name}_wpctl_inspect_src" "$dir/wpctl_inspect_default_source.txt" 10 \
                wpctl inspect "$SOVYX_DIAG_DEFAULT_SOURCE_ID"
        fi
        if [[ -n "$SOVYX_DIAG_DEFAULT_SINK_ID" ]]; then
            run_step "snap_${name}_wpctl_inspect_sink" "$dir/wpctl_inspect_default_sink.txt" 10 \
                wpctl inspect "$SOVYX_DIAG_DEFAULT_SINK_ID"
        fi
    fi

    # /dev/snd open handles. ORDEM: lsof/fuser ANTES de qualquer arecord no mesmo estado.
    run_step "snap_${name}_lsof_snd"  "$dir/lsof_snd.txt"  10 bash -c 'lsof /dev/snd/* 2>/dev/null || true'
    run_step "snap_${name}_fuser_snd" "$dir/fuser_snd.txt" 10 bash -c 'fuser -v /dev/snd/* 2>&1 || true'

    # Sockets.
    run_step "snap_${name}_ss_unix" "$dir/ss_unix.txt" 10 bash -c 'ss -lnpx 2>/dev/null | grep -E "pipewire|pulse|sovyx" || true'
    run_step "snap_${name}_ss_tcp"  "$dir/ss_tcp.txt"  10 bash -c 'ss -lntp 2>/dev/null | grep -E "7777|sovyx" || true'

    # /proc/asound.
    run_step "snap_${name}_proc_asound_cards" "$dir/proc_asound_cards.txt" 5 cat /proc/asound/cards
    run_step "snap_${name}_proc_asound_devices" "$dir/proc_asound_devices.txt" 5 cat /proc/asound/devices
    # PCM status é legível se stream ativo.
    for status_file in /proc/asound/card*/pcm*c/sub0/status; do
        [[ -r "$status_file" ]] || continue
        local tag
        tag=$(echo "$status_file" | tr '/' '_' | sed 's/^_//')
        run_step "snap_${name}_${tag}" "$dir/${tag}.txt" 5 cat "$status_file"
    done

    # Sovyx PID — se existir, grava fd count e detalhes.
    if [[ -n "$sovyx_pid" && -d "/proc/$sovyx_pid" ]]; then
        local fd_count
        fd_count=$(ls "/proc/$sovyx_pid/fd" 2>/dev/null | wc -l || echo 0)
        echo "$fd_count" > "$dir/sovyx_pid_fd_count.txt"

        run_step "snap_${name}_sovyx_status" "$dir/sovyx_proc_status.txt" 5 \
            bash -c "cat /proc/$sovyx_pid/status 2>/dev/null || true"
        run_step "snap_${name}_sovyx_limits" "$dir/sovyx_proc_limits.txt" 5 \
            bash -c "cat /proc/$sovyx_pid/limits 2>/dev/null || true"
        run_step "snap_${name}_sovyx_cgroup" "$dir/sovyx_proc_cgroup.txt" 5 \
            bash -c "cat /proc/$sovyx_pid/cgroup 2>/dev/null || true"
        run_step "snap_${name}_sovyx_ns" "$dir/sovyx_proc_namespaces.txt" 5 \
            bash -c "ls -la /proc/$sovyx_pid/ns/ 2>/dev/null || true"
        run_step "snap_${name}_sovyx_maps_libs" "$dir/sovyx_proc_maps_libs.txt" 10 \
            bash -c "awk '{print \$6}' /proc/$sovyx_pid/maps 2>/dev/null | sort -u || true"
        run_step "snap_${name}_sovyx_threads" "$dir/sovyx_threads.txt" 5 \
            bash -c "ps -L -p $sovyx_pid 2>/dev/null || true"
        run_step "snap_${name}_sovyx_fd_list" "$dir/sovyx_proc_fd_list.txt" 10 \
            bash -c "ls -la /proc/$sovyx_pid/fd 2>/dev/null | head -100 || true"
    else
        echo "0" > "$dir/sovyx_pid_fd_count.txt"
    fi

    # Env redigido.
    env | redact_stream > "$dir/env_snapshot.txt"
    header_write "$dir/env_snapshot.txt" "snap_${name}_env" "env | redact" 0 0

    # D-Bus.
    if command -v busctl >/dev/null 2>&1; then
        run_step "snap_${name}_busctl_user" "$dir/dbus_user_names.txt" 10 busctl --user list --no-pager
    fi
    if command -v dbus-send >/dev/null 2>&1; then
        run_step "snap_${name}_dbus_listnames" "$dir/dbus_session_names.txt" 10 \
            dbus-send --session --print-reply \
                --dest=org.freedesktop.DBus /org/freedesktop/DBus \
                org.freedesktop.DBus.ListNames
    fi

    # systemd --user units.
    run_step "snap_${name}_systemd_user_units" "$dir/systemd_user_units.txt" 10 \
        bash -c 'systemctl --user list-units --state=running,failed --no-pager 2>&1 || true'

    manifest_append "snap_${name}" "states/$name/" \
        "Snapshot completo do estado $name — eixo 1 do plano (§1.5)." \
        "Base para diffs inter-estado."
}

# ─────────────────────────────────────────────────────────────────────────
# Transições de estado
# ─────────────────────────────────────────────────────────────────────────

# S_OFF: Sovyx parado, socket ausente.
enter_S_OFF() {
    _transition_to "S_OFF"

    if sovyx_is_running; then
        log_info "stopping sovyx (may take up to 30s for shutdown)..."
        sovyx stop >/dev/null 2>&1 || true
        if ! _wait_sovyx_state no 30; then
            log_warn "sovyx stop timed out — sending SIGTERM to daemon PIDs only"
            _kill_sovyx_daemon TERM
            sleep 5
        fi
        if sovyx_is_running; then
            log_warn "sovyx still alive — SIGKILL to daemon PIDs only"
            _kill_sovyx_daemon KILL
            sleep 2
        fi
    fi

    # Resolve PW defaults AGORA (antes da primeira captura).
    resolve_pw_defaults || true

    snapshot_state "S_OFF"
}

# S_IDLE: Sovyx rodando, voice desabilitado.
enter_S_IDLE() {
    _transition_to "S_IDLE"

    if ! sovyx_is_running; then
        log_info "starting sovyx daemon..."
        # Lança daemon em background (sovyx start pode ser bloqueante dependendo da versão).
        ( sovyx start >/dev/null 2>&1 & )
        if ! _wait_sovyx_state yes 30; then
            log_error "sovyx failed to start within 30s"
            # Não aborta — capturas posteriores vão falhar graciosamente.
        fi
    fi

    # Recarrega token caso tenha sido gerado agora.
    [[ -z "$SOVYX_DIAG_TOKEN" ]] && resolve_dashboard_token || true

    # Garante voice off.
    if [[ -n "$SOVYX_DIAG_TOKEN" ]]; then
        _api_post "/api/voice/disable" "{}" || true
        sleep 2
    fi

    resolve_pw_defaults || true
    snapshot_state "S_IDLE"
}

# S_ACTIVE: Sovyx rodando, voice habilitado.
enter_S_ACTIVE() {
    _transition_to "S_ACTIVE"

    # Garante que estamos vindo de S_IDLE (daemon up).
    if ! sovyx_is_running; then
        log_warn "S_ACTIVE: sovyx not running; attempting start"
        ( sovyx start >/dev/null 2>&1 & )
        _wait_sovyx_state yes 30 || log_error "sovyx start failed"
    fi

    [[ -z "$SOVYX_DIAG_TOKEN" ]] && resolve_dashboard_token || true

    if [[ -n "$SOVYX_DIAG_TOKEN" ]]; then
        # Enable voice. Body vazio → usa mind.yaml persisted config.
        _api_post "/api/voice/enable" "{}" || true
        # Warm-up — aguarda pipeline estabilizar antes do snapshot.
        sleep 10
    else
        log_warn "S_ACTIVE: no token — voice enable skipped"
    fi

    resolve_pw_defaults || true
    snapshot_state "S_ACTIVE"
}

# Transição S_ACTIVE → S_IDLE dedicada a K. Não re-snapshota (já temos
# S_IDLE do phase 2). Garante voice disabled antes de voice/test/output
# ser chamado.
transition_to_S_IDLE_for_K() {
    _transition_to "S_IDLE"
    log_info "transitioning back to S_IDLE for layer K (disable voice pipeline)"
    if [[ -n "$SOVYX_DIAG_TOKEN" ]] && sovyx_is_running; then
        _api_post "/api/voice/disable" "{}" || true
        sleep 3
    fi
}

# S_RESIDUAL_t5: 5s após sovyx stop a partir de S_ACTIVE (ou S_IDLE pós-K).
enter_S_RESIDUAL_t5() {
    _transition_to "S_RESIDUAL_t5"

    if [[ -n "$SOVYX_DIAG_TOKEN" ]] && sovyx_is_running; then
        _api_post "/api/voice/disable" "{}" || true
        sleep 2
    fi

    # V4.3: marca timestamp ANTES do stop pra capturar janela completa
    # de logs durante residual. Sem isso, _snapshot_residual_window não
    # tem ponto de partida e coleta logs do boot inteiro.
    SOVYX_DIAG_RESIDUAL_START_UTC=$(date -u +%Y-%m-%dT%H:%M:%S)
    SOVYX_DIAG_RESIDUAL_START_MONO=$(now_monotonic_ns)
    # Captura PID Sovyx ANTES do stop pra inspecionar /proc/$pid/fd
    # depois (pode existir 5s pós-SIGTERM em zombie state).
    SOVYX_DIAG_RESIDUAL_LAST_SOVYX_PID=$(_sovyx_daemon_pids 2>/dev/null | head -1 || true)

    log_info "stopping sovyx for residual sampling..."
    sovyx stop >/dev/null 2>&1 || true
    _wait_sovyx_state no 30 || _kill_sovyx_daemon TERM
    sleep 5

    snapshot_state "S_RESIDUAL_t5"
    _snapshot_residual_window "S_RESIDUAL_t5" "$SOVYX_DIAG_RESIDUAL_START_UTC" \
        "$SOVYX_DIAG_RESIDUAL_LAST_SOVYX_PID"
}

# S_RESIDUAL_t30: 30s depois — crítico para detecção de vazamento.
enter_S_RESIDUAL_t30() {
    _transition_to "S_RESIDUAL_t30"
    log_info "waiting 25s for t30 residual window..."
    sleep 25
    snapshot_state "S_RESIDUAL_t30"
    _snapshot_residual_window "S_RESIDUAL_t30" \
        "${SOVYX_DIAG_RESIDUAL_START_UTC:-}" \
        "${SOVYX_DIAG_RESIDUAL_LAST_SOVYX_PID:-}"
}

# V4.3 — Captura forense específica da janela residual:
#   1. journalctl --since <residual_start> sovyx + audio
#   2. dmesg --since <residual_start>
#   3. /proc/$last_sovyx_pid/* se ainda existe (zombie inspection)
#   4. ps -o pid,ppid,stat,etime,command pra detectar processos
#      remanescentes (Z = zombie, D = uninterruptible sleep)
#   5. lsof | grep sovyx — handles ainda abertos por nome
#
# Uso: _snapshot_residual_window <state_name> <start_utc> <last_sovyx_pid>
_snapshot_residual_window() {
    local state="$1" start_utc="${2:-}" last_pid="${3:-}"
    local dir="$SOVYX_DIAG_OUTDIR/states/$state/residual_window"
    mkdir -p "$dir"

    # journalctl filtrado por sovyx + audio desde residual start.
    if command -v journalctl >/dev/null 2>&1 && [[ -n "$start_utc" ]]; then
        run_step "${state}_residual_journal_sovyx" \
            "$dir/journal_sovyx_since_residual.txt" 15 \
            bash -c "journalctl --since '$start_utc' --no-pager 2>&1 | grep -iE 'sovyx|pipewire|wireplumber|alsa|pulseaudio|portaudio' || echo '(no relevant entries since $start_utc)'"
    fi

    # dmesg desde residual start (kernel pode emitir liberação de driver).
    if command -v dmesg >/dev/null 2>&1; then
        run_step "${state}_residual_dmesg" \
            "$dir/dmesg_since_residual.txt" 10 \
            bash -c "dmesg --time-format=iso 2>/dev/null | awk -v s='$start_utc' '\$0 > s' || dmesg | tail -100"
    fi

    # ps com etime — processos sovyx-related ainda vivos.
    run_step "${state}_residual_ps_sovyx" "$dir/ps_sovyx_remaining.txt" 10 \
        bash -c "ps -eo pid,ppid,stat,etime,user,command --sort=etime 2>/dev/null | grep -iE 'sovyx|pipewire|wireplumber' | grep -v 'voice-diag' || echo '(no sovyx-related processes)'"

    # Zombie/D-state processes (atenção especial — podem indicar leak).
    run_step "${state}_residual_zombie_dstate" "$dir/zombie_dstate_processes.txt" 5 \
        bash -c "ps -eo pid,ppid,stat,command 2>/dev/null | awk 'NR==1 || \$3 ~ /^[ZD]/' || echo '(no Z/D state processes)'"

    # /proc/$last_pid/* se ainda existe (zombie inspection).
    if [[ -n "$last_pid" ]] && [[ "$last_pid" =~ ^[0-9]+$ ]] \
            && [[ -d "/proc/$last_pid" ]]; then
        {
            echo "=== /proc/$last_pid/status ==="
            cat "/proc/$last_pid/status" 2>&1 || echo "(unreadable)"
            echo ""
            echo "=== /proc/$last_pid/stat ==="
            cat "/proc/$last_pid/stat" 2>&1 || echo "(unreadable)"
            echo ""
            echo "=== ls /proc/$last_pid/fd/ ==="
            ls -la "/proc/$last_pid/fd/" 2>&1 || echo "(unreadable)"
        } > "$dir/last_sovyx_pid_${last_pid}_proc.txt" 2>&1
        header_write "$dir/last_sovyx_pid_${last_pid}_proc.txt" \
            "${state}_last_sovyx_proc" "/proc inspection" 0 0
    fi

    # lsof | grep sovyx — qualquer process ainda segurando arquivo
    # com 'sovyx' no path.
    if command -v lsof >/dev/null 2>&1; then
        run_step "${state}_residual_lsof_sovyx" "$dir/lsof_grep_sovyx.txt" 15 \
            bash -c "lsof 2>/dev/null | grep -i sovyx || echo '(no open files match sovyx)'"
    fi

    manifest_append "${state}_residual_window" \
        "states/$state/residual_window/" \
        "Janela forense do estado residual: journalctl/dmesg desde $start_utc, ps sobrevivos, zombies/D-state, /proc do último PID Sovyx, lsof. Detecta memory/fd/thread leak + driver zumbi." \
        "F6/G2 (multi-instance/leak)"
}

# S_POST_SUSPEND (opt-in): ciclo suspend/resume manual.
enter_S_POST_SUSPEND() {
    _transition_to "S_POST_SUSPEND"

    if ! prompt_yn "Suspend + resume: sistema vai hibernar. Após resumir, o script continua automaticamente. Prosseguir?"; then
        log_warn "S_POST_SUSPEND cancelled"
        return
    fi
    log_info "issuing systemctl suspend"
    # Dispara em background para o script não morrer no suspend.
    ( sleep 3 && systemctl suspend ) &
    # Aguarda o sistema retornar. Detecta via uptime/monotonic jump.
    local before_mono after_mono
    before_mono=$(now_monotonic_ns)
    log_info "sleeping 60s wall-clock (script pausa efetiva durante suspend)..."
    sleep 60
    after_mono=$(now_monotonic_ns)
    local skipped_ns=$(( after_mono - before_mono ))
    log_info "resumed; monotonic delta ${skipped_ns}ns"

    sleep "$SOVYX_DIAG_SUSPEND_RESUME_WAIT"
    snapshot_state "S_POST_SUSPEND"
}

# S_EXTERNAL_GRAB (opt-in): usuário abre app externa que segura o mic.
enter_S_EXTERNAL_GRAB() {
    _transition_to "S_EXTERNAL_GRAB"

    if ! prompt_user "Abra Firefox ou Chromium em uma página que peça microfone (ex.: https://webcammictest.com) e conceda permissão. Volte ao terminal e pressione ENTER." 180; then
        log_warn "S_EXTERNAL_GRAB skipped"
        return
    fi
    snapshot_state "S_EXTERNAL_GRAB"
    prompt_user "Pode fechar a aba do Firefox/Chromium. Pressione ENTER para continuar." 60 || true
}

# ─────────────────────────────────────────────────────────────────────────
# Diffs inter-estado
# ─────────────────────────────────────────────────────────────────────────

generate_state_diffs() {
    _transition_to "DIFF_GEN"
    local diff_dir="$SOVYX_DIAG_OUTDIR/states/_diffs"
    mkdir -p "$diff_dir"

    local -a transitions=(
        "S_OFF:S_IDLE"
        "S_IDLE:S_ACTIVE"
        "S_ACTIVE:S_RESIDUAL_t5"
        "S_RESIDUAL_t5:S_RESIDUAL_t30"
    )
    [[ -d "$SOVYX_DIAG_OUTDIR/states/S_POST_SUSPEND" ]] && \
        transitions+=("S_RESIDUAL_t30:S_POST_SUSPEND")
    [[ -d "$SOVYX_DIAG_OUTDIR/states/S_EXTERNAL_GRAB" ]] && \
        transitions+=("S_RESIDUAL_t30:S_EXTERNAL_GRAB")

    local summary="$diff_dir/summary.md"
    {
        echo "# Inter-state diffs — resumo automático"
        echo ""
        echo "Gerado em $(now_utc_ns)."
        echo ""
    } > "$summary"

    local trans from to from_dir to_dir out_file
    for trans in "${transitions[@]}"; do
        from="${trans%:*}"
        to="${trans#*:}"
        from_dir="$SOVYX_DIAG_OUTDIR/states/$from"
        to_dir="$SOVYX_DIAG_OUTDIR/states/$to"
        [[ -d "$from_dir" && -d "$to_dir" ]] || continue

        out_file="$diff_dir/diff_${from}_to_${to}.txt"
        log_info "diff: $from -> $to"

        {
            echo "# diff $from -> $to"
            echo "# generated: $(now_utc_ns)"
            echo "# ---"
            # Only diff text files — skip JSON (too noisy) and binaries.
            local f name
            for f in "$from_dir"/*.txt; do
                [[ -r "$f" ]] || continue
                name=$(basename "$f")
                [[ -r "$to_dir/$name" ]] || continue
                echo ""
                echo "=== $name ==="
                diff -u "$f" "$to_dir/$name" 2>&1 || true
            done
        } > "$out_file"

        # Heurísticas de vazamento/novos sockets para summary.md.
        _diff_heuristics "$from" "$to" >> "$summary"
    done

    manifest_append "state_diffs" "states/_diffs/" \
        "Diffs inter-estado — eixo 1. summary.md resume achados automáticos."
}

# Heurísticas que detectam vazamento de recursos entre estados.
# Saída em markdown, anexada ao summary.md.
_diff_heuristics() {
    local from="$1" to="$2"
    local from_dir="$SOVYX_DIAG_OUTDIR/states/$from"
    local to_dir="$SOVYX_DIAG_OUTDIR/states/$to"

    echo ""
    echo "## $from → $to"
    echo ""

    # Sovyx fd count delta.
    if [[ -r "$from_dir/sovyx_pid_fd_count.txt" && -r "$to_dir/sovyx_pid_fd_count.txt" ]]; then
        local from_fd to_fd
        from_fd=$(cat "$from_dir/sovyx_pid_fd_count.txt" 2>/dev/null || echo 0)
        to_fd=$(cat "$to_dir/sovyx_pid_fd_count.txt" 2>/dev/null || echo 0)
        if [[ "$from_fd" != "$to_fd" ]]; then
            echo "- Sovyx fd count: $from_fd → $to_fd"
            # Flag vazamento severo em RESIDUAL transitions.
            if [[ "$from" = "S_ACTIVE" && "$to" = "S_RESIDUAL_t5" && "$to_fd" -gt 0 ]]; then
                echo "  - ⚠ VAZAMENTO: Sovyx deveria estar parado em S_RESIDUAL"
                alert_append "warn" "fd leak in $from -> $to: $to_fd fds still open"
            fi
        fi
    fi

    # pactl modules — novos em to que não estavam em from.
    if [[ -r "$from_dir/pactl_modules.txt" && -r "$to_dir/pactl_modules.txt" ]]; then
        local new_modules
        new_modules=$(comm -13 \
            <(grep -oE 'module-[a-z0-9-]+' "$from_dir/pactl_modules.txt" 2>/dev/null | sort -u) \
            <(grep -oE 'module-[a-z0-9-]+' "$to_dir/pactl_modules.txt" 2>/dev/null | sort -u) \
            | paste -sd ',' -)
        if [[ -n "$new_modules" ]]; then
            echo "- Novos pactl modules: $new_modules"
            # Flag filtros potencialmente destrutivos.
            if grep -qE 'echo-cancel|ladspa|filter-apply|rnnoise|webrtc|noise-cancel' <<<"$new_modules"; then
                echo "  - ⚠ Filtro potencialmente destrutivo ativado"
                alert_append "warn" "potentially destructive filter loaded in $to: $new_modules"
            fi
        fi
    fi

    # Source-outputs novos (algum cliente abriu o mic).
    if [[ -r "$from_dir/pactl_source-outputs.txt" && -r "$to_dir/pactl_source-outputs.txt" ]]; then
        local from_count to_count
        from_count=$(grep -c '^Source Output' "$from_dir/pactl_source-outputs.txt" 2>/dev/null)
        from_count="${from_count//[^0-9]/}"; from_count="${from_count:-0}"
        to_count=$(grep -c '^Source Output' "$to_dir/pactl_source-outputs.txt" 2>/dev/null)
        to_count="${to_count//[^0-9]/}"; to_count="${to_count:-0}"
        if [[ "$from_count" != "$to_count" ]]; then
            echo "- source-outputs count: $from_count → $to_count"
        fi
    fi

    # /dev/snd open handles.
    if [[ -r "$from_dir/lsof_snd.txt" && -r "$to_dir/lsof_snd.txt" ]]; then
        local from_lsof to_lsof
        from_lsof=$(wc -l < "$from_dir/lsof_snd.txt" 2>/dev/null || echo 0)
        to_lsof=$(wc -l < "$to_dir/lsof_snd.txt" 2>/dev/null || echo 0)
        if [[ "$from_lsof" != "$to_lsof" ]]; then
            echo "- /dev/snd open handles (lsof lines): $from_lsof → $to_lsof"
        fi
    fi
}
