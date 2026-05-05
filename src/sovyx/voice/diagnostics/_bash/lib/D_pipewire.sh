#!/usr/bin/env bash
# lib/D_pipewire.sh — Camada D: PipeWire / WirePlumber / PulseAudio shim.
#
# Objetivo: identificar reroutings, filter chains (echo-cancel, rnnoise),
# DSP do server, política WirePlumber, estado dos nodes, metadata.
# Roda em S_IDLE (Sovyx up, voice off — sem concorrência pelo mic produtivo).
#
# Hipóteses (§2 do plano):
#   D1 — Filter chain (echo-cancel / rnnoise / webrtc) destruindo voz.
#   D2 — WirePlumber policy roteando para virtual source.
#   D3 — Quantum travado problemático.
#   D4 — Sample rate conflito (graph ≠ default ≠ client).
#   D5 — Outro processo segurou o source.
#   D6 — pcm.default → alsa-pa → pipewire-pulse (3 saltos, 3 resamples).
#   D7 — Script Lua customizado em ~/.config/wireplumber.
#   D8 — pw-metadata com default_target apontando node errado.
#   D9 — XDG portal interferindo.
#
# Capturas W5–W9, W14b neste layer (em S_IDLE, default source acessível).

_capture_pipewire_wav() {
    # Mesma pegada de _capture_arecord_v2: evita run_step para preservar WAV.
    # Uso: _capture_pipewire_wav <cid> <tool> <device> <rate> <channels> <fmt_suffix> <duration> [mode]
    local cid="$1" tool="$2" device="$3" rate="$4" channels="$5" fmt="$6" duration="$7" mode="${8:-}"
    local cap_dir="$SOVYX_DIAG_OUTDIR/D_pipewire/captures"
    local subdir="$cap_dir/$cid"
    mkdir -p "$subdir"
    local wav="$subdir/capture.wav"
    local meta="$subdir/capture.meta.json"
    local log="$subdir/tool.log"
    local phrase='Sovyx, me ouça agora: um, dois, três, quatro, cinco.'

    if [[ "$mode" != "silence" ]]; then
        printf '\n\033[1;36m>>> [%s] Fale agora (%ss): "%s"\033[0m\n' "$cid" "$duration" "$phrase" >&2
    else
        printf '\n\033[1;36m>>> [%s] Mantenha SILÊNCIO (%ss)\033[0m\n' "$cid" "$duration" >&2
    fi

    local start_utc start_mono end_utc end_mono duration_ms rc cmd_str
    start_utc=$(now_utc_ns)
    start_mono=$(now_monotonic_ns)

    case "$tool" in
        arecord)
            cmd_str="arecord -D $device -f $fmt -r $rate -c $channels -d $duration $wav"
            set +o pipefail
            timeout --preserve-status --kill-after=5 $((duration + 5)) \
                arecord -D "$device" -f "$fmt" -r "$rate" -c "$channels" -d "$duration" "$wav" \
                > "$log" 2>&1
            rc=$?
            set -o pipefail
            ;;
        pw-record)
            local pw_fmt="s16"
            [[ "$fmt" = "S16_LE" ]] && pw_fmt="s16"
            cmd_str="pw-record --target=$device --rate=$rate --channels=$channels --format=$pw_fmt $wav"
            # pw-record precisa de timeout externo — ele não sai sozinho.
            set +o pipefail
            (
                timeout --preserve-status --kill-after=3 $((duration + 2)) \
                    pw-record --target="$device" --rate="$rate" --channels="$channels" \
                              --format="$pw_fmt" "$wav" > "$log" 2>&1 &
                pid=$!
                sleep "$duration"
                kill -TERM "$pid" 2>/dev/null
                wait "$pid" 2>/dev/null
            )
            rc=$?
            set -o pipefail
            ;;
        parecord)
            cmd_str="parecord --device=$device --rate=$rate --channels=$channels --format=s16le --file-format=wav $wav"
            set +o pipefail
            (
                timeout --preserve-status --kill-after=3 $((duration + 2)) \
                    parecord --device="$device" --rate="$rate" --channels="$channels" \
                             --format=s16le --file-format=wav "$wav" > "$log" 2>&1 &
                pid=$!
                sleep "$duration"
                kill -TERM "$pid" 2>/dev/null
                wait "$pid" 2>/dev/null
            )
            rc=$?
            set -o pipefail
            ;;
        *)
            echo "unknown tool: $tool" > "$log"
            rc=99
            ;;
    esac

    end_utc=$(now_utc_ns)
    end_mono=$(now_monotonic_ns)
    duration_ms=$(( (end_mono - start_mono) / 1000000 ))

    # AUDIT v3 — normalize format token to upper-case (S16_LE) so the
    # field matches C_alsa's schema. D_pipewire accepts lowercase `s16`
    # or `s16le` from callers but serializes a canonical form.
    local fmt_canonical
    case "$fmt" in
        s16|s16le|S16_LE) fmt_canonical="S16_LE" ;;
        s24|s24le|S24_LE) fmt_canonical="S24_LE" ;;
        s32|s32le|S32_LE) fmt_canonical="S32_LE" ;;
        *)                fmt_canonical="$fmt" ;;
    esac
    cat > "$meta" <<EOF
{
  "capture_id": "$cid",
  "state": "$SOVYX_DIAG_STATE",
  "layer": "D_pipewire",
  "tool": "$tool",
  "device": "$device",
  "sample_rate": $rate,
  "channels": $channels,
  "format": "$fmt_canonical",
  "format_raw": "$fmt",
  "duration_s_requested": $duration,
  "duration_s_actual": $(awk -v ms="$duration_ms" 'BEGIN{printf "%.3f", ms/1000}'),
  "duration_ms_actual": $duration_ms,
  "retcode": $rc,
  "start_utc": "$start_utc",
  "start_monotonic_ns": $start_mono,
  "end_utc": "$end_utc",
  "end_monotonic_ns": $end_mono,
  "mode": "$mode"
}
EOF

    # v2 (audit post-SVX-VOICE-LINUX-20260422) — wall-clock + wav-size
    # sanity floors, symmetric with C_alsa.sh and E_portaudio.sh.
    # pw-record and parecord are especially prone to silent short-fail
    # when the PipeWire graph is in an unstable state (module hot-
    # reload, session-manager restart). A 200 ms capture that returns
    # rc=0 used to count as "ok"; now the duration floor catches it.
    local min_duration_ms=$(( duration * 1000 * 95 / 100 ))
    local duration_pass=0
    [[ $duration_ms -ge $min_duration_ms ]] && duration_pass=1
    local min_wav_bytes=$(( 44 + rate * channels * 2 / 2 ))  # ≥ 0.5 s of samples
    local wav_size
    wav_size=$(stat -c%s "$wav" 2>/dev/null || echo 0)
    local size_pass=0
    [[ $wav_size -ge $min_wav_bytes ]] && size_pass=1

    local note
    if [[ $rc -ne 0 ]]; then
        if [[ $rc -eq 124 ]]; then
            note="TIMEOUT"; (( SOVYX_DIAG_STEPS_TIMEOUT++ )) || true
        else
            note="retcode=$rc"; (( SOVYX_DIAG_STEPS_FAIL++ )) || true
        fi
    elif [[ $duration_pass -eq 0 ]]; then
        note="capture_too_short:${duration_ms}ms<${min_duration_ms}ms"
        (( SOVYX_DIAG_STEPS_FAIL++ )) || true
    elif [[ $size_pass -eq 0 ]]; then
        note="empty_or_header_only_wav:${wav_size}B"
        (( SOVYX_DIAG_STEPS_FAIL++ )) || true
    else
        note="ok"; (( SOVYX_DIAG_STEPS_OK++ )) || true
    fi
    (( SOVYX_DIAG_STEPS_TOTAL++ )) || true

    if [[ "$note" = "ok" ]]; then
        printf '    \033[32m✓ captured %s.%03ss (tool=%s rc=0, %sB WAV)\033[0m\n' \
            $((duration_ms / 1000)) \
            $((duration_ms % 1000)) \
            "$tool" "$wav_size" >&2
    else
        printf '    \033[31m✗ FAILED tool=%s rc=%s note=%s actual=%s.%03ss size=%sB\033[0m\n' \
            "$tool" "$rc" "$note" \
            $((duration_ms / 1000)) \
            $((duration_ms % 1000)) \
            "$wav_size" >&2
    fi
    timeline_append "D_${cid}" "$SOVYX_DIAG_STATE" "$start_utc" "$start_mono" \
                    "$end_utc" "$end_mono" "$duration_ms" "$cmd_str" "$rc" "$wav" "$note"
    printf '[%s] step=D_%s state=%s retcode=%s duration_ms=%s out=%s notes=%s\n' \
        "$start_utc" "$cid" "$SOVYX_DIAG_STATE" "$rc" "$duration_ms" "$wav" "$note" \
        >> "$SOVYX_DIAG_RUNLOG"

    # Analyze.
    # V4 Track H: capture rc + verify output file existence so a Python
    # crash cannot silently omit analysis.json while the capture remains
    # marked "ok". Emit an error JSON + alert when analysis fails.
    if [[ -s "$wav" && -n "$SOVYX_DIAG_PYTHON" ]]; then
        "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/analyze_wav.py" \
            --wav "$wav" --state "$SOVYX_DIAG_STATE" \
            --source "$tool:$device" --capture-id "$cid" \
            --monotonic-ns "$start_mono" --utc-iso-ns "$start_utc" \
            --out "$subdir/analysis.json" 2>"$subdir/analyze.log"
        local analyze_rc=$?
        if [[ $analyze_rc -ne 0 ]] || [[ ! -s "$subdir/analysis.json" ]]; then
            printf '{"error":"analyze_wav_failed","rc":%d,"log":"analyze.log","capture_id":"%s"}\n' \
                "$analyze_rc" "$cid" > "$subdir/analysis.json"
            alert_append "warn" "analyze_wav failed rc=$analyze_rc on $cid; analysis.json contains error marker"
        fi

        "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/silero_probe.py" \
            --wav "$wav" --out "$subdir/silero.json" \
            2>>"$subdir/analyze.log"
        local silero_rc=$?
        if [[ $silero_rc -ne 0 ]] || [[ ! -s "$subdir/silero.json" ]]; then
            printf '{"available":false,"reason":"silero_probe_failed","rc":%d,"capture_id":"%s"}\n' \
                "$silero_rc" "$cid" > "$subdir/silero.json"
            alert_append "warn" "silero_probe failed rc=$silero_rc on $cid; silero.json contains error marker"
        fi
    fi

    manifest_append "D_${cid}" "D_pipewire/captures/${cid}/" \
        "Captura via $tool em $device. Path PipeWire/Pulse/shim." "D1-D6"

    sleep 1
}


run_layer_D() {
    local dir="$SOVYX_DIAG_OUTDIR/D_pipewire"
    local cfg_dir="$dir/configs"
    mkdir -p "$dir" "$cfg_dir"
    log_info "=== Layer D: PipeWire (S_IDLE) ==="

    # ── Inventário pactl ─────────────────────────────────────────────────
    if tool_has pactl >/dev/null; then
        run_step "D_pactl_info"           "$dir/pactl_info.txt"           10 pactl info
        run_step "D_pactl_sources"        "$dir/pactl_sources.txt"        15 pactl list sources
        run_step "D_pactl_sinks"          "$dir/pactl_sinks.txt"          15 pactl list sinks
        run_step "D_pactl_source_outputs" "$dir/pactl_source-outputs.txt" 10 pactl list source-outputs
        run_step "D_pactl_sink_inputs"    "$dir/pactl_sink-inputs.txt"    10 pactl list sink-inputs
        run_step "D_pactl_modules"        "$dir/pactl_modules.txt"        10 pactl list modules
        run_step "D_pactl_clients"        "$dir/pactl_clients.txt"        10 pactl list clients
        manifest_append "D_pactl" "D_pipewire/pactl_*.txt" \
            "Inventário completo pactl. Alimenta D1 (modules), D2/D5." "D1/D2/D5"
    fi

    # ── pw-dump: JSON estruturado + filtro de nodes Processor/Filter ─────
    if tool_has pw-dump >/dev/null; then
        run_step_pipe "D_pw_dump" "$dir/pw_dump.json" 20 pw-dump --no-colors
        # Filtros DSP/processor que destruiriam voz.
        if tool_has jq >/dev/null; then
            jq '[.[] | select(.info.props["media.class"]? | tostring | test("Filter|Processor"))
                      | {id: .id, name: .info.props["node.name"], media_class: .info.props["media.class"]}]' \
                < "$dir/pw_dump.json" > "$dir/pw_dump_filters.json" 2>/dev/null || \
                echo "[]" > "$dir/pw_dump_filters.json"
            manifest_append "D_pw_filters" "D_pipewire/pw_dump_filters.json" \
                "Filtros DSP no grafo — raiz candidata D1 se não-vazio." "D1"
        fi
    fi

    # ── pw-top (snapshot de latência/xruns por node) ─────────────────────
    if tool_has pw-top >/dev/null; then
        run_step "D_pw_top" "$dir/pw_top.txt" 12 pw-top -b -n 5
    fi

    # ── pw-metadata (settings globais + default targets) ─────────────────
    if tool_has pw-metadata >/dev/null; then
        run_step "D_pw_metadata_settings" "$dir/pw_metadata_settings.txt" 10 pw-metadata -n settings 0
        run_step "D_pw_metadata_full"     "$dir/pw_metadata_full.txt"     10 pw-metadata 0
        manifest_append "D_pw_metadata" "D_pipewire/pw_metadata_*.txt" \
            "Metadata global — default.audio.source/sink, target.object, quantum, rate. Alimenta D3/D4/D8." \
            "D3/D4/D8"
    fi

    # ── wpctl (WirePlumber) ─────────────────────────────────────────────
    if tool_has wpctl >/dev/null; then
        run_step "D_wpctl_status"  "$dir/wpctl_status.txt"  10 wpctl status
        run_step "D_wpctl_settings" "$dir/wpctl_settings.txt" 10 wpctl settings
        [[ -n "$SOVYX_DIAG_DEFAULT_SOURCE_ID" ]] && \
            run_step "D_wpctl_inspect_src" "$dir/wpctl_inspect_default_source.txt" 10 \
                wpctl inspect "$SOVYX_DIAG_DEFAULT_SOURCE_ID"
        [[ -n "$SOVYX_DIAG_DEFAULT_SINK_ID" ]] && \
            run_step "D_wpctl_inspect_sink" "$dir/wpctl_inspect_default_sink.txt" 10 \
                wpctl inspect "$SOVYX_DIAG_DEFAULT_SINK_ID"
    fi

    # ── pw-cli dump all (legado, diff com pw-dump) ───────────────────────
    if tool_has pw-cli >/dev/null; then
        run_step "D_pw_cli_info" "$dir/pw_cli_info_all.txt" 15 pw-cli info all
    fi

    # ── Pacotes instalados (versão exata) ────────────────────────────────
    run_step "D_pkg_versions" "$dir/pkg_audio_versions.txt" 15 \
        bash -c "dpkg -l 2>/dev/null | grep -iE 'pipewire|wireplumber|pulse|easyeffects|noisetorch|rnnoise|webrtc|alsa|libasound|portaudio' || echo 'dpkg unavailable'"

    # ── Configs user + system ────────────────────────────────────────────
    for user_dir in "$HOME/.config/pipewire" "$HOME/.config/wireplumber" "$HOME/.config/pulse"; do
        [[ -d "$user_dir" ]] || continue
        local name
        name=$(basename "$user_dir")
        mkdir -p "$cfg_dir/user_$name"
        find "$user_dir" -type f \( -name '*.conf' -o -name '*.lua' -o -name '*.json' -o -name '*.conf.d' \) \
            -exec cp --parents {} "$cfg_dir/user_$name/" \; 2>/dev/null || true
    done
    for sys_dir in /etc/pipewire /etc/wireplumber /usr/share/wireplumber; do
        [[ -d "$sys_dir" ]] || continue
        local name
        name=$(basename "$sys_dir")
        mkdir -p "$cfg_dir/sys_$name"
        find "$sys_dir" -maxdepth 4 -type f \( -name '*.conf' -o -name '*.lua' -o -name '*.json' \) \
            -print > "$cfg_dir/sys_${name}_listing.txt" 2>/dev/null || true
    done
    run_step "D_configs_listing" "$dir/configs_listing.txt" 10 \
        bash -c "ls -laR \"$cfg_dir\" 2>/dev/null | head -500"
    manifest_append "D_configs" "D_pipewire/configs/" \
        "Configs PipeWire/WirePlumber/Pulse do usuário + system — detecta scripts Lua custom (D7)." "D7"

    # ── systemd --user status ───────────────────────────────────────────
    for unit in pipewire pipewire-pulse wireplumber; do
        run_step "D_systemd_${unit}" "$dir/systemd_user_${unit}.txt" 10 \
            bash -c "systemctl --user status $unit --no-pager 2>&1 | head -50 || true"
    done

    # journalctl dos units (última 1h).
    for unit in pipewire pipewire-pulse wireplumber; do
        run_step "D_journalctl_${unit}" "$dir/journalctl_user_${unit}.txt" 20 \
            bash -c "journalctl --user -u $unit --since '1 hour ago' --output=short-iso-precise --no-pager 2>&1 | tail -200 || true"
    done

    # Sockets unix — quem conecta ao PipeWire.
    run_step "D_ss_pipewire" "$dir/ss_pipewire.txt" 10 \
        bash -c 'ss -xl 2>/dev/null | grep -E "pipewire|pulse" || echo "no matches"'

    # ── Capturas ─────────────────────────────────────────────────────────
    if [[ "$SOVYX_DIAG_FLAG_SKIP_CAPTURES" = "1" ]]; then
        log_info "skipping D captures (--skip-captures)"
        manifest_append "D_layer" "D_pipewire/" "Camada D parcial (sem .wav)." "D1-D9"
        return
    fi

    if ! tool_has arecord >/dev/null; then
        log_warn "arecord unavailable — default/pipewire ALSA captures skipped"
    else
        prompt_user "Próximas capturas PipeWire (5 wavs). Frase: 'Sovyx, me ouça agora: um, dois, três, quatro, cinco.'" 30 || true

        # W5 — arecord -D default 48k 1ch (alsa-pa bridge)
        _capture_pipewire_wav "W5_default_48k_s16_1ch" arecord default 48000 1 S16_LE 7

        # W6 — arecord -D default 16k 1ch (caminho que Sovyx usou — E4)
        _capture_pipewire_wav "W6_default_16k_s16_1ch" arecord default 16000 1 S16_LE 7

        # W7 — arecord -D pipewire 16k 1ch (plugin ALSA→PipeWire)
        _capture_pipewire_wav "W7_pipewire_16k_s16_1ch" arecord pipewire 16000 1 S16_LE 7

        # W14b — baseline de silêncio via default (floor de ruído através do servidor)
        _capture_pipewire_wav "W14b_default_silence_16k" arecord default 16000 1 S16_LE 2 "silence"
    fi

    # W8 — pw-record direto no default source ID (API PipeWire nativa)
    if tool_has pw-record >/dev/null && [[ -n "$SOVYX_DIAG_DEFAULT_SOURCE_ID" ]]; then
        _capture_pipewire_wav "W8_pwrecord_default_16k" pw-record \
            "$SOVYX_DIAG_DEFAULT_SOURCE_ID" 16000 1 s16 7
    else
        log_warn "W8 skipped: pw-record missing or default source ID not resolved"
    fi

    # W9 — parecord (API PulseAudio)
    if tool_has parecord >/dev/null && [[ -n "$SOVYX_DIAG_DEFAULT_SOURCE_NAME" ]]; then
        _capture_pipewire_wav "W9_parecord_default_16k" parecord \
            "$SOVYX_DIAG_DEFAULT_SOURCE_NAME" 16000 1 s16le 7
    else
        log_warn "W9 skipped: parecord missing or default source name not resolved"
    fi

    manifest_append "D_layer" "D_pipewire/" \
        "Camada D — PipeWire/WirePlumber/Pulse. Inventário + configs + journalctl + capturas W5-W9, W14b." \
        "D1-D9"
}
