#!/usr/bin/env bash
# lib/K_output.sh — Camada K: cadeia de saída (TTS → PipeWire → ALSA → alto-falantes).
#
# Objetivo: o sintoma "sem resposta sonora" pode ser (a) entrada morta
# [A-F], (b) processamento morto [G-H], ou (c) SAÍDA morta. Esta camada
# isola (c) com evidência independente.
#
# Roda em S_IDLE (voice test endpoint recusa se pipeline ativo — R3 do ADR).
#
# Hipóteses (§2 do plano):
#   K1 — Sink default mudo / volume 0 / role suspenso.
#   K2 — PipeWire roteando playback para HDMI/Bluetooth sem alto-falantes.
#   K3 — Kokoro gerou .wav interno mas playback não solicitado.
#   K4 — Sink bloqueado por cliente exclusivo anterior.

_playback_test() {
    # Uso: _playback_test <cid> <tool> <device_flag> <wav_path>
    # Faz o playback + prompt did_hear + registra em playback_results.
    local cid="$1" tool="$2" device="$3" wav="$4"
    local dir="$SOVYX_DIAG_OUTDIR/K_output"
    local results_file="$dir/playback_results.json"
    local log="$dir/${cid}_playback.log"

    if [[ ! -f "$wav" ]]; then
        log_warn "$cid: wav missing: $wav"
        return
    fi

    local start_utc start_mono end_utc end_mono duration_ms rc cmd
    start_utc=$(now_utc_ns)
    start_mono=$(now_monotonic_ns)

    printf '\n\033[1;36m>>> [%s] Vou tocar um som (~1s) no dispositivo: %s\033[0m\n' "$cid" "$device" >&2

    # AUDIT v3 — acoustic verification via concurrent monitor-source
    # capture. Previously, the ONLY evidence of playback was operator's
    # "y/n" — can't distinguish "played silence" from "sink muted" from
    # "speakers unplugged" from subjective misperception. Now: if
    # pactl is available, capture 3 s of the sink's .monitor source
    # during playback and emit an RMS measurement alongside the prompt.
    # A non-zero RMS on .monitor proves signal reached the sink; the
    # prompt then asks about the physical chain (speakers).
    local monitor_wav=""
    local monitor_rms_dbfs=""
    local monitor_pid=""
    local default_sink=""
    if [[ "$tool" != "unknown_tool: "* ]] && tool_has parecord >/dev/null && tool_has pactl >/dev/null; then
        default_sink=$(pactl get-default-sink 2>/dev/null || echo "")
        if [[ -n "$default_sink" ]]; then
            monitor_wav="$dir/${cid}_monitor_capture.wav"
            # parecord starts ~instantly; run it in background and
            # kill after the playback + 0.5 s tail to capture any
            # tail-fade samples.
            ( parecord --device="${default_sink}.monitor" \
                --rate=48000 --channels=1 --format=s16le --file-format=wav \
                "$monitor_wav" 2>/dev/null ) &
            monitor_pid=$!
        fi
    fi

    case "$tool" in
        aplay)
            cmd="aplay -D $device $wav"
            timeout --preserve-status --kill-after=3 10 \
                aplay -D "$device" "$wav" > "$log" 2>&1
            rc=$?
            ;;
        paplay)
            cmd="paplay --device=$device $wav"
            timeout --preserve-status --kill-after=3 10 \
                paplay --device="$device" "$wav" > "$log" 2>&1
            rc=$?
            ;;
        pw-play)
            cmd="pw-play --target=$device $wav"
            timeout --preserve-status --kill-after=3 10 \
                pw-play --target="$device" "$wav" > "$log" 2>&1
            rc=$?
            ;;
        *)
            rc=99
            cmd="unknown_tool: $tool"
            echo "unknown tool" > "$log"
            ;;
    esac

    end_utc=$(now_utc_ns)
    end_mono=$(now_monotonic_ns)
    duration_ms=$(( (end_mono - start_mono) / 1000000 ))

    # Terminate monitor capture and compute its RMS — objective evidence.
    if [[ -n "$monitor_pid" ]]; then
        sleep 0.5
        kill "$monitor_pid" 2>/dev/null || true
        wait "$monitor_pid" 2>/dev/null || true
        if [[ -s "$monitor_wav" && -n "$SOVYX_DIAG_PYTHON" ]]; then
            monitor_rms_dbfs=$("$SOVYX_DIAG_PYTHON" \
                "$SOVYX_DIAG_LIB_DIR/py/analyze_wav.py" \
                --wav "$monitor_wav" --state "$SOVYX_DIAG_STATE" \
                --source "monitor:${default_sink}" --capture-id "${cid}_monitor" \
                --monotonic-ns "$start_mono" --utc-iso-ns "$start_utc" \
                2>/dev/null \
                | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("rms_dbfs",""))' \
                2>/dev/null || echo "")
        fi
    fi

    local heard
    heard=$(prompt_did_hear "$cid: ouviu o som? (monitor RMS: ${monitor_rms_dbfs:-n/a} dBFS)")

    # AUDIT v3 — ATOMIC append to playback_results.json via tempfile
    # + os.replace. Previous version did read-modify-write with a
    # plain ``write_text``; crash mid-write corrupted the file AND
    # the read-side fallback ``except: arr = []`` silently discarded
    # ALL prior playback records. For a forensic append-log that is
    # catastrophic. Also: stderr from python is now captured to
    # ``.py.err`` so malformed writes surface.
    python3 - "$cid" "$tool" "$device" "$wav" "$rc" "$heard" \
            "$start_utc" "$start_mono" "$end_utc" "$end_mono" "$duration_ms" \
            "$results_file" "${monitor_rms_dbfs:-}" "${monitor_wav:-}" \
            2>"$dir/${cid}_playback_append.err" <<'PYEOF'
import json, os, pathlib, sys, tempfile

(cid, tool, device, wav, rc, heard,
 s_utc, s_mono, e_utc, e_mono, dur, out_path, monitor_rms, monitor_wav) = sys.argv[1:]
path = pathlib.Path(out_path)

# AUDIT v3: reading failures used to silently reset the array,
# losing all prior records. Now: on parse failure, rename the
# corrupted file to ``.corrupt.N`` so the analyst can inspect it,
# and start a fresh array.
arr = []
if path.exists() and path.stat().st_size:
    try:
        arr = json.loads(path.read_text())
        if not isinstance(arr, list):
            raise ValueError("not a list")
    except Exception as exc:
        # Preserve the corrupted file for forensic inspection.
        for i in range(100):
            bak = path.with_suffix(path.suffix + f".corrupt.{i}")
            if not bak.exists():
                path.rename(bak)
                sys.stderr.write(f"preserved_corrupt: {bak}\n")
                break
        sys.stderr.write(f"prior_results_unparseable: {type(exc).__name__}: {exc}\n")
        arr = []

arr.append({
    "cid": cid,
    "tool": tool,
    "device": device,
    "wav": wav,
    "retcode": int(rc),
    "did_hear": heard,
    "start_utc_ns": s_utc,
    "start_monotonic_ns": int(s_mono),
    "end_utc_ns": e_utc,
    "end_monotonic_ns": int(e_mono),
    "duration_ms": int(dur),
    "monitor_rms_dbfs": monitor_rms or None,
    "monitor_wav": monitor_wav or None,
})

# Atomic write via tempfile + os.replace.
path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(
    mode="w",
    delete=False,
    dir=str(path.parent),
    prefix=f".{path.name}.",
    suffix=".tmp",
    encoding="utf-8",
) as tmp:
    tmp.write(json.dumps(arr, indent=2))
    tmp.flush()
    os.fsync(tmp.fileno())
    tmp_path = tmp.name
os.replace(tmp_path, str(path))
PYEOF

    local note="retcode=$rc; heard=$heard; monitor_rms=${monitor_rms_dbfs:-n/a}"
    if [[ $rc -eq 0 ]]; then (( SOVYX_DIAG_STEPS_OK++ )) || true
    elif [[ $rc -eq 124 ]]; then (( SOVYX_DIAG_STEPS_TIMEOUT++ )) || true
    elif [[ $rc -eq 143 || $rc -eq 137 ]]; then
        # AUDIT v3: `--preserve-status` makes timeout return the signal
        # rc, not 124. Count signal-kills as timeouts for consistency
        # with the STEPS_TIMEOUT semantics.
        (( SOVYX_DIAG_STEPS_TIMEOUT++ )) || true
        note="$note; timed-out-via-signal"
    else
        (( SOVYX_DIAG_STEPS_FAIL++ )) || true
    fi
    (( SOVYX_DIAG_STEPS_TOTAL++ )) || true
    timeline_append "K_${cid}" "$SOVYX_DIAG_STATE" "$start_utc" "$start_mono" \
                    "$end_utc" "$end_mono" "$duration_ms" "$cmd" "$rc" "$log" "$note"
    printf '[%s] step=K_%s state=%s retcode=%s duration_ms=%s notes=%s\n' \
        "$start_utc" "$cid" "$SOVYX_DIAG_STATE" "$rc" "$duration_ms" "$note" \
        >> "$SOVYX_DIAG_RUNLOG"

    manifest_append "K_${cid}" "K_output/${cid}_playback.log" \
        "Playback via $tool em device='$device'. heard=$heard (usuário). rc=$rc." \
        "K1-K4"

    sleep 1
}


run_layer_K() {
    local dir="$SOVYX_DIAG_OUTDIR/K_output"
    mkdir -p "$dir"
    log_info "=== Layer K: output chain (S_IDLE) ==="

    if [[ "$SOVYX_DIAG_FLAG_SKIP_CAPTURES" = "1" ]]; then
        log_info "skipping K (--skip-captures includes playback)"
        manifest_append "K_layer" "K_output/" "Camada K pulada (--skip-captures)." "K1-K4"
        return
    fi

    # ── Inventário sink ──────────────────────────────────────────────────
    if tool_has pactl >/dev/null; then
        run_step "K_sink_info" "$dir/sink_info.txt" 10 pactl list sinks
        run_step "K_default_sink" "$dir/default_sink.txt" 5 pactl get-default-sink
        run_step "K_sink_volume" "$dir/volumes_mute.txt" 5 \
            bash -c 'pactl get-sink-mute @DEFAULT_SINK@; pactl get-sink-volume @DEFAULT_SINK@'
    fi
    if tool_has wpctl >/dev/null && [[ -n "$SOVYX_DIAG_DEFAULT_SINK_ID" ]]; then
        run_step "K_wpctl_sink" "$dir/wpctl_inspect_sink.txt" 10 \
            wpctl inspect "$SOVYX_DIAG_DEFAULT_SINK_ID"
    fi
    if tool_has amixer >/dev/null; then
        local target_card
        target_card=$(cat "$SOVYX_DIAG_OUTDIR/C_alsa/target_card.txt" 2>/dev/null || echo "1")
        run_step "K_amixer_master"    "$dir/amixer_master.txt"    10 \
            bash -c "amixer -c $target_card sget Master 2>&1 || amixer -c $target_card sget Speaker 2>&1 || amixer -c $target_card sget PCM 2>&1 || echo 'no master-class control'"
        run_step "K_amixer_headphone" "$dir/amixer_headphone.txt" 10 \
            bash -c "amixer -c $target_card sget Headphone 2>&1 || echo 'no Headphone control'"
    fi

    # AUDIT F1 gap-fix — K4 hypothesis ("Sink bloqueado por cliente
    # exclusivo anterior") requires evidence of who holds the playback
    # PCM devices (pcm*p). Previous version only had capture-side
    # lsof/fuser (pcm*c) in C_alsa. Add playback-side pre-inventory.
    run_step "K_lsof_pcm_playback_pre" "$dir/lsof_pcm_playback_pre.txt" 10 \
        bash -c 'lsof /dev/snd/pcmC*D*p 2>/dev/null || echo "no open playback handles"'
    run_step "K_fuser_pcm_playback_pre" "$dir/fuser_pcm_playback_pre.txt" 10 \
        bash -c 'fuser -v /dev/snd/pcmC*D*p 2>&1 || echo "no playback handles"'
    manifest_append "K_pcm_playback_pre" "K_output/lsof_pcm_playback_pre.txt K_output/fuser_pcm_playback_pre.txt" \
        "Abre handles de playback PCM antes dos testes — sustenta K4 (sink exclusive-locked)." "K4"

    # ── Gera tom + toca em cada caminho ──────────────────────────────────
    if [[ -z "$SOVYX_DIAG_PYTHON" ]]; then
        log_warn "no python — can't generate tone; K playback tests skipped"
        manifest_append "K_layer" "K_output/" "Camada K parcial — sem python p/ tom." "K1-K4"
        return
    fi

    local tone="$dir/K_tone.wav"
    "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/tone_gen.py" \
        --out "$tone" --freq 440 --duration 1.0 --rate 48000 --channels 2 --amplitude 0.3 \
        > "$dir/tone_gen.log" 2>&1 || log_warn "tone_gen failed"

    if [[ ! -f "$tone" ]]; then
        log_warn "K_tone.wav not generated; skipping playback tests"
        return
    fi

    prompt_user "Próximos testes: vou tocar um tom de 440 Hz (~1s) em cada device (hw direto, default, paplay, pw-play). Após cada um, responda 'y' ou 'n'. ENTER para começar." 60 || true

    # K1 — direto ao hw (bypass completo do servidor)
    if tool_has aplay >/dev/null; then
        local target_card
        target_card=$(cat "$SOVYX_DIAG_OUTDIR/C_alsa/target_card.txt" 2>/dev/null || echo "1")
        _playback_test "K1_tone_hw_direct" aplay "hw:${target_card},0" "$tone"
        # K2 — aplay default (alsa-pa shim)
        _playback_test "K2_tone_default" aplay "default" "$tone"
    fi
    # K3 — paplay (API PulseAudio)
    if tool_has paplay >/dev/null; then
        local sink_name
        sink_name="${SOVYX_DIAG_DEFAULT_SINK_NAME:-@DEFAULT_SINK@}"
        _playback_test "K3_tone_paplay" paplay "$sink_name" "$tone"
    fi
    # K4 — pw-play (API PipeWire nativa)
    if tool_has pw-play >/dev/null && [[ -n "$SOVYX_DIAG_DEFAULT_SINK_ID" ]]; then
        _playback_test "K4_tone_pwplay" pw-play "$SOVYX_DIAG_DEFAULT_SINK_ID" "$tone"
    fi

    # ── Kokoro TTS — via API test/output (toca) + Python direto (só wav) ─
    if [[ -n "$SOVYX_DIAG_TOKEN" ]]; then
        log_info "Kokoro test via /api/voice/test/output..."
        run_step_pipe "K_api_test_output_post" "$dir/api_voice_test_output_job.json" 15 \
            curl -sS --max-time 15 \
                 -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
                 -H "Content-Type: application/json" \
                 -X POST "http://127.0.0.1:7777/api/voice/test/output" \
                 -d '{"phrase_key":"default","language":"pt-br","voice":"pf_dora"}'

        # Extrai job_id e faz polling.
        local job_id
        job_id=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("job_id",""))' \
                 < "$dir/api_voice_test_output_job.json" 2>/dev/null || echo "")
        if [[ -n "$job_id" ]]; then
            log_info "polling job_id=$job_id..."
            local i=0
            while (( i < 30 )); do
                sleep 0.5
                run_step_pipe "K_api_test_output_poll_${i}" \
                    "$dir/api_voice_test_output_result.json" 8 \
                    curl -sS --max-time 8 \
                         -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
                         "http://127.0.0.1:7777/api/voice/test/output/$job_id"
                local status
                status=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status",""))' \
                         < "$dir/api_voice_test_output_result.json" 2>/dev/null || echo "")
                if [[ "$status" = "done" || "$status" = "error" ]]; then
                    break
                fi
                i=$((i + 1))
            done
            heard=$(prompt_did_hear "K_kokoro_api: você ouviu o Kokoro tocando a frase default em pt-br?")
            python3 - "$heard" "$dir/api_voice_test_output_result.json" "$dir/kokoro_api_result.json" <<'PYEOF' 2>/dev/null
import json, sys, pathlib
heard, src, out = sys.argv[1:]
try:
    src_data = json.loads(pathlib.Path(src).read_text())
except Exception:
    src_data = {}
src_data["user_did_hear"] = heard
pathlib.Path(out).write_text(json.dumps(src_data, indent=2))
PYEOF
        else
            log_warn "no job_id from /api/voice/test/output — API may be disabled or pipeline active"
        fi
    else
        echo "no token — Kokoro API test skipped" > "$dir/kokoro_api_SKIPPED.txt"
    fi

    # Kokoro direto via Python — para capturar wav sem playback.
    if [[ -n "$SOVYX_DIAG_PYTHON" ]]; then
        log_info "Kokoro synth direto (sem playback)..."
        # V4 Track H: capture rc so a Kokoro synth failure is not masked.
        "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/kokoro_synth.py" \
            --text "Teste de áudio do Sovyx." \
            --voice "pf_dora" --language "pt-br" \
            --out "$dir/K_kokoro.wav" \
            > "$dir/kokoro_synth_result.json" 2>"$dir/kokoro_synth.log"
        local kokoro_rc=$?
        if [[ $kokoro_rc -ne 0 ]]; then
            alert_append "warn" "kokoro_synth rc=$kokoro_rc; K_kokoro.wav may be absent or invalid (K3 verdict blocked)"
        fi

        if [[ -f "$dir/K_kokoro.wav" ]] && [[ -s "$dir/K_kokoro.wav" ]]; then
            # Analisa o .wav gerado (sem tocar).
            "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/analyze_wav.py" \
                --wav "$dir/K_kokoro.wav" \
                --state "$SOVYX_DIAG_STATE" \
                --source "kokoro_synth_direct" \
                --capture-id "K_kokoro_direct" \
                --monotonic-ns "$(now_monotonic_ns)" --utc-iso-ns "$(now_utc_ns)" \
                --out "$dir/K_kokoro_analysis.json" 2>>"$dir/kokoro_synth.log"
            local kokoro_analyze_rc=$?
            if [[ $kokoro_analyze_rc -ne 0 ]] || [[ ! -s "$dir/K_kokoro_analysis.json" ]]; then
                printf '{"error":"analyze_wav_failed","rc":%d,"source":"kokoro_synth_direct"}\n' \
                    "$kokoro_analyze_rc" > "$dir/K_kokoro_analysis.json"
                alert_append "warn" "K_kokoro analyze_wav failed rc=$kokoro_analyze_rc; K_kokoro_analysis.json contains error marker"
            fi
            manifest_append "K_kokoro_direct" "K_output/K_kokoro.wav" \
                "Kokoro TTS gerado localmente (sem playback). Análise espectral separada em K_kokoro_analysis.json. Se RMS=0 → bug Kokoro runtime (K3)." \
                "K3"
        else
            alert_append "error" "K_kokoro.wav ausente ou vazio após kokoro_synth — K3 verdict INCONCLUSIVE; see kokoro_synth.log"
        fi
    fi

    manifest_append "K_layer" "K_output/" \
        "Camada K — saída: sink info, volumes, tom 440Hz em 4 caminhos + Kokoro via API e via Python direto." \
        "K1-K4"
}
