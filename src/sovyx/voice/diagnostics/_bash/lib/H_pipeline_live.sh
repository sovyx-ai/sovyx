#!/usr/bin/env bash
# lib/H_pipeline_live.sh — Camada H: teste end-to-end do pipeline Sovyx (oráculo real).
#
# Roda em S_ACTIVE (voice on). Usuário fala a frase-canário DUAS vezes:
#   1. Durante o pipeline produtivo corrente (voice ligado).
#   2. Se --intrusive-restart-audio: reinicia pipewire/wireplumber, reabilita voice, fala de novo.
#
# Coleta:
#   - Tail de sovyx.log em torno da janela de fala.
#   - T0 / T_fala_start / T_fala_end em ISO-ns + monotonic_ns.
#   - Diff antes/depois no GET /api/voice/status.
#
# Hipóteses (§2 do plano):
#   H1 — /api/voice/test/input (meter) vivo enquanto pipeline produtivo morto → divergência.
#   H2 — Race na inicialização (Sovyx antes do PipeWire pronto).

_mark_timestamp() {
    local label="$1" outfile="$2"
    {
        printf 'label: %s\n' "$label"
        printf 'utc_iso_ns: %s\n' "$(now_utc_ns)"
        printf 'monotonic_ns: %s\n' "$(now_monotonic_ns)"
        printf 'state: %s\n' "$SOVYX_DIAG_STATE"
    } >> "$outfile"
}

_tail_sovyx_log_between() {
    # Copia linhas do sovyx.log dentro da janela [start_s, end_s].
    # Uso: _tail_sovyx_log_between <start_epoch_s> <end_epoch_s> <out_file>
    #
    # AUDIT v3 — três fixes:
    #   (1) stderr do python NÃO é swallowed — vai para out.err sibling
    #       file, so parse failures surface forensic-detectable.
    #   (2) Empty-output-vs-crash discriminator: marker line written
    #       upfront so downstream can tell "extraction found nothing"
    #       from "extraction crashed".
    #   (3) Timezone-aware ISO parsing — assume UTC when no tz.
    local start_s="$1" end_s="$2" out="$3"
    local err="${out%.*}.err"
    local sovyx_log="$HOME/.sovyx/logs/sovyx.log"
    if [[ ! -r "$sovyx_log" ]]; then
        {
            echo "# extractor_status: log_unreadable"
            echo "# sovyx_log: $sovyx_log"
        } > "$out"
        : > "$err"
        return
    fi
    # Write marker header so consumers distinguish "extracted 0 lines"
    # from "extractor crashed before writing anything".
    printf '# extractor_status: started\n# window_start_epoch: %s\n# window_end_epoch: %s\n# source: %s\n' \
        "$start_s" "$end_s" "$sovyx_log" > "$out"

    python3 - "$sovyx_log" "$start_s" "$end_s" >> "$out" 2>"$err" <<'PYEOF'
import json, sys, pathlib
import datetime as _dt

path = pathlib.Path(sys.argv[1])
start = float(sys.argv[2])
end = float(sys.argv[3])
matched = 0
parsed = 0
try:
    with path.open("r", errors="replace") as f:
        for line in f:
            parsed += 1
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = rec.get("timestamp") or rec.get("ts") or rec.get("time")
            if ts is None:
                continue
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                # ISO string; normalize to UTC if no tz info.
                try:
                    s = str(ts).replace("Z", "+00:00")
                    dt = _dt.datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_dt.timezone.utc)
                    ts = dt.timestamp()
                except Exception:
                    continue
            if start <= ts <= end:
                sys.stdout.write(line)
                matched += 1
except Exception as exc:
    sys.stderr.write(f"extractor_failed: {type(exc).__name__}: {exc}\n")
    sys.exit(1)

sys.stderr.write(f"parsed_lines={parsed} matched_lines={matched}\n")
PYEOF
    local py_rc=$?
    if [[ $py_rc -ne 0 ]]; then
        {
            printf '# extractor_status: crashed rc=%s (see %s)\n' "$py_rc" "$(basename "$err")"
        } >> "$out"
    else
        printf '# extractor_status: complete\n' >> "$out"
    fi
}

run_layer_H() {
    local dir="$SOVYX_DIAG_OUTDIR/H_pipeline_live"
    mkdir -p "$dir"
    local marks="$dir/timestamps_marks.txt"
    : > "$marks"
    log_info "=== Layer H: pipeline live test ==="

    if [[ -z "$SOVYX_DIAG_TOKEN" ]]; then
        echo "no dashboard token — H layer cannot exercise APIs" > "$dir/LAYER_SKIPPED.txt"
        header_write "$dir/LAYER_SKIPPED.txt" "H_skip" "no_token" 1 0
        manifest_append "H_layer" "H_pipeline_live/LAYER_SKIPPED.txt" \
            "Camada H parcial — sem token dashboard." ""
        return
    fi

    # Pré-estado.
    _mark_timestamp "T0_pre" "$marks"

    run_step_pipe "H_voice_status_pre" "$dir/voice_status_pre.json" 10 \
        curl -sS --max-time 10 \
             -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
             "http://127.0.0.1:7777/api/voice/status"

    # ── Rodada 1: pipeline produtivo corrente ───────────────────────────
    prompt_user "Frase-canário com pipeline produtivo. Vou marcar T_start → fale 'Sovyx, me ouça agora: um, dois, três, quatro, cinco.' → aguarde 8s → T_end." 60 || true

    _mark_timestamp "T_speak_start_round1" "$marks"
    local speak_start
    speak_start=$(date -u +%s)
    log_info "fale agora; aguardo 10s..."
    sleep 10
    _mark_timestamp "T_speak_end_round1" "$marks"

    # Snapshot da status API depois da fala.
    run_step_pipe "H_voice_status_post_r1" "$dir/voice_status_post_round1.json" 10 \
        curl -sS --max-time 10 \
             -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
             "http://127.0.0.1:7777/api/voice/status"
    run_step_pipe "H_capture_diag_r1" "$dir/capture_diagnostics_round1.json" 15 \
        curl -sS --max-time 15 \
             -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
             "http://127.0.0.1:7777/api/voice/capture-diagnostics"

    # Extrai log entre speak_start e now.
    _tail_sovyx_log_between "$speak_start" "$(date -u +%s)" \
        "$dir/live_pipeline_log_slice_round1.txt"

    # ── Rodada 2: intrusive restart (opt-in) ────────────────────────────
    if [[ "${SOVYX_DIAG_FLAG_INTRUSIVE_RESTART_AUDIO:-0}" = "1" ]]; then
        log_warn "INTRUSIVE: restarting pipewire/wireplumber; will restore on trap"
        # AUDIT v3 — ``RESTART_PENDING=1`` sinaliza ao trap de EXIT que
        # ainda devemos restaurar os serviços. Antes, essa variável era
        # zerada INCONDICIONALMENTE no fim do bloco (linha 201) mesmo
        # em falha intermediária — o trap não faria restoration, user
        # ficaria com audio half-restarted. Agora só zeramos no caminho
        # de sucesso, e validamos cada restart.
        SOVYX_DIAG_AUDIO_RESTART_PENDING=1

        _mark_timestamp "T_intrusive_restart_start" "$marks"
        run_step "H_stop_pw" "$dir/pw_stop.txt" 15 \
            bash -c 'systemctl --user stop wireplumber pipewire-pulse pipewire 2>&1'
        local stop_rc=$?
        sleep 3
        run_step "H_start_pw" "$dir/pw_start.txt" 15 \
            bash -c 'systemctl --user start pipewire pipewire-pulse wireplumber 2>&1'
        local start_rc=$?
        sleep 5

        # Verificar que pipewire/wireplumber estão ativos pós-start.
        # ``is-active`` returns 0 iff ALL given units are active.
        local svc_active=0
        systemctl --user is-active pipewire wireplumber >/dev/null 2>&1 && svc_active=1
        echo "stop_rc=$stop_rc start_rc=$start_rc svc_active=$svc_active" \
            > "$dir/restart_verdict.txt"

        if [[ $svc_active -ne 1 ]]; then
            log_error "pipewire/wireplumber not active after restart — leaving RESTART_PENDING=1"
            alert_append "error" "pipewire restart failed; audio stack in degraded state; trap will attempt restoration"
            # Intentionally do NOT zero RESTART_PENDING — the trap must
            # try to restore. Skip the voice-re-enable + round 2 below
            # (no point if the audio stack is broken).
            manifest_append "H_intrusive_abort" "H_pipeline_live/restart_verdict.txt" \
                "Intrusive restart aborted; audio stack degraded. Trap will restore." "H1/H2"
            return
        fi

        # Re-enable voice no novo graph.
        run_step_pipe "H_voice_disable_post_restart" "$dir/voice_disable_post_restart.json" 15 \
            curl -sS --max-time 15 \
                 -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
                 -X POST "http://127.0.0.1:7777/api/voice/disable" \
                 -H "Content-Type: application/json" -d '{}'
        sleep 2
        run_step_pipe "H_voice_enable_post_restart" "$dir/voice_enable_post_restart.json" 30 \
            curl -sS --max-time 30 \
                 -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
                 -X POST "http://127.0.0.1:7777/api/voice/enable" \
                 -H "Content-Type: application/json" -d '{}'
        sleep 10

        prompt_user "Rodada 2: fale a mesma frase novamente. Aguardo 10s." 60 || true

        _mark_timestamp "T_speak_start_round2" "$marks"
        local speak_start2=$(date -u +%s)
        sleep 10
        _mark_timestamp "T_speak_end_round2" "$marks"

        run_step_pipe "H_voice_status_post_r2" "$dir/voice_status_post_round2.json" 10 \
            curl -sS --max-time 10 \
                 -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
                 "http://127.0.0.1:7777/api/voice/status"
        run_step_pipe "H_capture_diag_r2" "$dir/capture_diagnostics_round2.json" 15 \
            curl -sS --max-time 15 \
                 -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
                 "http://127.0.0.1:7777/api/voice/capture-diagnostics"

        _tail_sovyx_log_between "$speak_start2" "$(date -u +%s)" \
            "$dir/live_pipeline_log_slice_round2.txt"

        # Only NOW, after the intrusive sequence completed end-to-end,
        # clear the pending flag. A bash error mid-sequence (pipefail)
        # would have returned earlier and left RESTART_PENDING=1, so
        # the EXIT trap would still attempt restoration.
        SOVYX_DIAG_AUDIO_RESTART_PENDING=0
    fi

    manifest_append "H_layer" "H_pipeline_live/" \
        "Camada H — teste live do pipeline Sovyx. Marks, status/diag pré+pós, log slice, intrusive restart opt-in." \
        "H1/H2"
}
