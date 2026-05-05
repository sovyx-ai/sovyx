#!/usr/bin/env bash
# lib/E_portaudio.sh — Camada E: PortAudio / sounddevice.
#
# Objetivo: isolar o caminho que o Sovyx efetivamente usa. É aqui que a
# bateria W10-W13 reproduz o stream exato da aplicação, com resolução
# DINÂMICA de device (substring → index) para não depender dos índices
# 7/6/4 do log (R7 do ADR).
#
# Roda em S_ACTIVE — o Sovyx está rodando COM voice on, mas o Sovyx usa
# exclusivamente stream próprio (sounddevice InputStream com callback),
# que não impede outro processo de abrir streams do mesmo device em shared
# mode. Se acontecer conflito, registramos e pulamos.
#
# Hipóteses (§2 do plano):
#   E_P1 — PortAudio resamplando 48→16k destrutivamente.
#   E_P2 — default com max_input_channels=64 forçando downmix incorreto.
#   E_P3 — libportaudio com bug para PipeWire.
#   E_P4 — Latência/buffer 32ms incompatível com graph.
#   E_P5 — Wheel do sounddevice com libportaudio estática patch destoante.

_capture_sd() {
    # Uso: _capture_sd <cid> <device_substring> <rate> <channels> <duration> [mode]
    local cid="$1" substring="$2" rate="$3" channels="$4" duration="$5" mode="${6:-}"
    local cap_dir="$SOVYX_DIAG_OUTDIR/E_portaudio/captures"
    local subdir="$cap_dir/$cid"
    mkdir -p "$subdir"
    local wav="$subdir/capture.wav"
    local phrase='Sovyx, me ouça agora: um, dois, três, quatro, cinco.'

    # V4.3 — strace opt-in attaching ao DAEMON Sovyx durante a captura
    # (não ao processo sd_capture.py que está rodando no nosso venv).
    # Loga openat de /dev/snd/* — comprova qual /dev/snd/pcmCxDxc o
    # Sovyx daemon abriu DURANTE esta janela. Sem isso, a coleta lista
    # cards mas nunca prova qual W11 efetivamente usou.
    #
    # Gates:
    #   - flag --trace-syscalls (default ON em common.sh)
    #   - strace presente
    #   - ptrace_scope < 2 OU --with-sudo
    #   - daemon Sovyx vivo (pid resolvido)
    #
    # Roda em background; killed após sd_capture.
    local strace_pid=""
    local strace_log="$subdir/sovyx_strace_openat.txt"
    if [[ "${SOVYX_DIAG_FLAG_TRACE_SYSCALLS:-1}" = "1" ]] \
            && command -v strace >/dev/null 2>&1; then
        local sovyx_pid scope
        sovyx_pid=$(_sovyx_daemon_pids 2>/dev/null | head -1)
        scope=1
        [[ -r /proc/sys/kernel/yama/ptrace_scope ]] && \
            scope=$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 1)
        if [[ -n "$sovyx_pid" ]] && [[ "$sovyx_pid" =~ ^[0-9]+$ ]]; then
            local strace_cmd
            if [[ "$scope" -ge 2 ]] && [[ "${SOVYX_DIAG_FLAG_WITH_SUDO:-0}" = "1" ]]; then
                strace_cmd="sudo -n strace -f -e trace=openat -p $sovyx_pid"
            elif [[ "$scope" -lt 2 ]]; then
                strace_cmd="strace -f -e trace=openat -p $sovyx_pid"
            else
                strace_cmd=""
                echo "skipped: ptrace_scope=$scope without --with-sudo" \
                    > "$strace_log"
            fi
            if [[ -n "$strace_cmd" ]]; then
                # Filtra só /dev/snd/* — reduz log noise drasticamente.
                ( $strace_cmd 2>&1 | grep --line-buffered '/dev/snd/' \
                    > "$strace_log" ) &
                strace_pid=$!
                # Pequeno delay para strace anexar antes do sd_capture.
                sleep 0.2
            fi
        else
            echo "skipped: sovyx daemon pid not resolved" > "$strace_log"
        fi
    fi

    if [[ "$mode" != "silence" ]]; then
        printf '\n\033[1;36m>>> [%s] Fale agora (%ss): "%s"\033[0m\n' "$cid" "$duration" "$phrase" >&2
    else
        printf '\n\033[1;36m>>> [%s] Mantenha SILÊNCIO (%ss)\033[0m\n' "$cid" "$duration" >&2
    fi

    local start_utc start_mono end_utc end_mono duration_ms rc
    start_utc=$(now_utc_ns)
    start_mono=$(now_monotonic_ns)
    local silent_flag=""
    [[ "$mode" = "silence" ]] && silent_flag="--silent"
    # v3 (audit post-session-2): wrap sd_capture.py in ``timeout`` so
    # a PortAudio deadlock (known failure mode under PipeWire session-
    # manager restart) cannot hang the whole layer. Budget = duration
    # + 10 s (sd_capture needs time for PortAudio init + ONNX/numpy
    # import + graceful close). timeout rc 124 surfaces cleanly.
    local budget_s
    budget_s=$(awk -v d="$duration" 'BEGIN{printf "%d", d + 10}')
    timeout --preserve-status --kill-after=5 "$budget_s" \
        "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/sd_capture.py" \
        --device-substring "$substring" \
        --rate "$rate" --channels "$channels" --duration "$duration" \
        --out "$wav" $silent_flag > "$subdir/sd_capture.log" 2>&1
    rc=$?
    end_utc=$(now_utc_ns)
    end_mono=$(now_monotonic_ns)
    duration_ms=$(( (end_mono - start_mono) / 1000000 ))

    # V4.3 — kill strace attaching ao Sovyx daemon (se foi spawned).
    if [[ -n "$strace_pid" ]]; then
        kill -INT "$strace_pid" 2>/dev/null || true
        wait "$strace_pid" 2>/dev/null || true
        # Sanity: arquivo strace_log deve ter pelo menos 1 openat
        # de /dev/snd se daemon estava ativo. Se vazio, anota.
        if [[ ! -s "$strace_log" ]]; then
            echo "(strace ran but captured no /dev/snd opens during this window)" \
                > "$strace_log"
        fi
    fi

    # v3 REGRESSION FIX — the prior v2 added sanity-floors asymmetric
    # vs C_alsa/D_pipewire, producing THREE distinct bugs:
    #
    #   (1) ``min_wav_bytes`` was HARDCODED to 16 kHz mono (= 16044 B),
    #       so W13 @ 48 kHz 2 ch silently accepted a 0.17 s fast-fail
    #       as ``ok`` because 48k-stereo-0.17s == 16380 B > 16044 B.
    #       Fix: parameterize on ``rate`` + ``channels`` exactly as
    #       C_alsa.sh / D_pipewire.sh do.
    #
    #   (2) The note-composition for WAV-size failure used ``note=
    #       "$note; empty..."`` (APPEND with semicolon) whereas C+D
    #       REPLACE the note. ``grep note=ok`` on timeline.csv matched
    #       E's ``ok; empty_or_header_only_wav`` as "ok" — false
    #       positive. Fix: gate OK vs FAIL on BOTH rc AND size_pass
    #       before the case, same shape as C+D.
    #
    #   (3) Counter incremented ``STEPS_OK`` when rc=0 even if the WAV
    #       was header-only. Fix: counter decision follows the gate.
    #
    # Audit trail: every capture now writes a ``sanity_checks`` array
    # to meta.json with the exact thresholds used, so analyst can
    # recompute pass/fail from raw data.
    local min_duration_ms
    min_duration_ms=$(awk -v d="$duration" 'BEGIN{printf "%d", d * 1000 * 0.95}')
    local min_wav_bytes=$(( 44 + rate * channels * 2 / 2 ))  # ≥ 0.5 s samples
    local wav_size
    wav_size=$(stat -c%s "$wav" 2>/dev/null)
    : "${wav_size:=0}"
    local duration_pass=0
    [[ $duration_ms -ge $min_duration_ms ]] && duration_pass=1
    local size_pass=0
    [[ $wav_size -ge $min_wav_bytes ]] && size_pass=1

    # Per-capture duration reported by sd_capture.py (audio-accurate,
    # from actual total_frames / actual_sr). Falls back to bash-
    # computed wall-clock if the meta file is absent or malformed.
    local actual_s="?"
    local actual_audio_s="?"
    if [[ -s "${wav}.capture_meta.json" ]]; then
        local meta_read
        meta_read=$("$SOVYX_DIAG_PYTHON" -c '
import json, sys
try:
    m = json.load(open(sys.argv[1]))
    print("{0}|{1}".format(
        m.get("duration_s_actual", "?"),
        m.get("duration_s_from_audio", m.get("duration_s_actual", "?")),
    ))
except Exception:
    print("?|?")
' "${wav}.capture_meta.json" 2>/dev/null)
        if [[ -n "$meta_read" ]]; then
            actual_s="${meta_read%%|*}"
            actual_audio_s="${meta_read##*|}"
        fi
    fi

    local note
    if [[ $rc -ne 0 ]]; then
        case "$rc" in
            124)     note="TIMEOUT" ;;
            # V4.3: --preserve-status faz timeout retornar signal rc.
            # 137 (SIGKILL via --kill-after) e 143 (SIGTERM) são
            # semanticamente timeouts — sem isso aparecem como FAIL
            # genérico e analyst não distingue de stream_open_failed.
            137|143) note="TIMEOUT_via_signal" ;;
            3)       note="stream_open_or_runtime_failed" ;;
            4)       note="capture_too_short_py" ;;
            5)       note="capture_too_few_frames_py" ;;
            2)       note="device_not_resolved" ;;
            1)       note="sounddevice_or_numpy_not_importable" ;;
            *)       note="retcode=$rc" ;;
        esac
    elif [[ $duration_pass -eq 0 ]]; then
        note="capture_too_short_bash:${duration_ms}ms<${min_duration_ms}ms"
    elif [[ $size_pass -eq 0 ]]; then
        note="empty_or_header_only_wav:${wav_size}B<${min_wav_bytes}B"
    else
        note="ok"
    fi

    case "$note" in
        ok)                                  (( SOVYX_DIAG_STEPS_OK++ )) || true ;;
        TIMEOUT|TIMEOUT_via_signal)          (( SOVYX_DIAG_STEPS_TIMEOUT++ )) || true ;;
        *)                                    (( SOVYX_DIAG_STEPS_FAIL++ )) || true ;;
    esac
    (( SOVYX_DIAG_STEPS_TOTAL++ )) || true

    # Per-capture status line — gated on [[ -t 2 ]] so non-TTY captures
    # (tee to file, CI, SSH without pty) don't get ANSI garbage.
    if [[ -t 2 ]]; then
        if [[ "$note" = "ok" ]]; then
            printf '    \033[32m✓ captured wall=%ss audio=%ss (rc=0, %sB WAV)\033[0m\n' \
                "$actual_s" "$actual_audio_s" "$wav_size" >&2
        else
            printf '    \033[31m✗ FAILED rc=%s note=%s wall=%ss audio=%ss size=%sB\033[0m\n' \
                "$rc" "$note" "$actual_s" "$actual_audio_s" "$wav_size" >&2
        fi
    else
        if [[ "$note" = "ok" ]]; then
            printf '    [OK] captured wall=%ss audio=%ss rc=0 size=%sB\n' \
                "$actual_s" "$actual_audio_s" "$wav_size" >&2
        else
            printf '    [FAIL] rc=%s note=%s wall=%ss audio=%ss size=%sB\n' \
                "$rc" "$note" "$actual_s" "$actual_audio_s" "$wav_size" >&2
        fi
    fi

    timeline_append "E_${cid}" "$SOVYX_DIAG_STATE" "$start_utc" "$start_mono" \
                    "$end_utc" "$end_mono" "$duration_ms" \
                    "sd_capture.py --device-substring $substring -r $rate -c $channels -d $duration" \
                    "$rc" "$wav" "$note"
    printf '[%s] step=E_%s state=%s retcode=%s duration_ms=%s out=%s notes=%s\n' \
        "$start_utc" "$cid" "$SOVYX_DIAG_STATE" "$rc" "$duration_ms" "$wav" "$note" \
        >> "$SOVYX_DIAG_RUNLOG"

    # Analyze.
    # V4 Track H: rc + output verification so analysis failures surface as
    # alerts + error JSON instead of a missing file (analyst would assume
    # capture ok, analysis ok, and silently miss the whole layer verdict).
    if [[ -s "$wav" ]]; then
        "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/analyze_wav.py" \
            --wav "$wav" --state "$SOVYX_DIAG_STATE" \
            --source "portaudio:$substring" --capture-id "$cid" \
            --monotonic-ns "$start_mono" --utc-iso-ns "$start_utc" \
            --out "$subdir/analysis.json" 2>"$subdir/analyze.log"
        local analyze_rc=$?
        if [[ $analyze_rc -ne 0 ]] || [[ ! -s "$subdir/analysis.json" ]]; then
            printf '{"error":"analyze_wav_failed","rc":%d,"log":"analyze.log","capture_id":"%s"}\n' \
                "$analyze_rc" "$cid" > "$subdir/analysis.json"
            alert_append "warn" "analyze_wav failed rc=$analyze_rc on E/$cid"
        fi

        "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/silero_probe.py" \
            --wav "$wav" --out "$subdir/silero.json" \
            2>>"$subdir/analyze.log"
        local silero_rc=$?
        if [[ $silero_rc -ne 0 ]] || [[ ! -s "$subdir/silero.json" ]]; then
            printf '{"available":false,"reason":"silero_probe_failed","rc":%d,"capture_id":"%s"}\n' \
                "$silero_rc" "$cid" > "$subdir/silero.json"
            alert_append "warn" "silero_probe failed rc=$silero_rc on E/$cid"
        fi
    fi

    # AUDIT v3+ T2: per-capture Sovyx context snapshot. Capture what
    # the Sovyx daemon was SEEING (voice_status + capture-diagnostics +
    # log slice) during this exact capture window. Without this, the
    # analyst cannot correlate "pipeline reported X during W11" with
    # "WAV W11 has RMS Y and VAD prob Z".
    _snapshot_sovyx_context "$cid" "$start_mono" "$end_mono" "$subdir"

    manifest_append "E_${cid}" "E_portaudio/captures/${cid}/" \
        "Captura PortAudio via sd_capture.py (substring=$substring). Reproduz stream Sovyx. Inclui sovyx_context/ sincronizado." "E_P1-E_P5"

    sleep 1
}


run_layer_E() {
    local dir="$SOVYX_DIAG_OUTDIR/E_portaudio"
    mkdir -p "$dir"
    log_info "=== Layer E: PortAudio (S_ACTIVE) ==="

    # ── Registrar venv_path ──────────────────────────────────────────────
    {
        echo "sovyx_python: $SOVYX_DIAG_PYTHON"
        echo "sovyx_python_kind: $SOVYX_DIAG_PYTHON_KIND"
    } > "$dir/venv_path.txt"
    header_write "$dir/venv_path.txt" "E_venv_path" "venv resolution" 0 0

    if [[ -z "$SOVYX_DIAG_PYTHON" ]]; then
        log_error "SOVYX_DIAG_PYTHON unresolved — E layer skipped entirely"
        echo "Python do Sovyx não resolvido. Camada E não pôde rodar." \
             > "$dir/LAYER_SKIPPED.txt"
        manifest_append "E_layer" "E_portaudio/LAYER_SKIPPED.txt" \
            "Camada E pulada — Python Sovyx não resolvido." ""
        return
    fi

    # ── sounddevice.query_devices + query_hostapis ───────────────────────
    run_step "E_sd_query_devices" "$dir/sounddevice_query.txt" 20 \
        "$SOVYX_DIAG_PYTHON" -c 'import sounddevice as sd, pprint; pprint.pprint(sd.query_devices())'
    run_step "E_sd_query_hostapis" "$dir/sounddevice_hostapis.txt" 20 \
        "$SOVYX_DIAG_PYTHON" -c 'import sounddevice as sd, pprint; pprint.pprint(sd.query_hostapis())'
    run_step "E_sd_version" "$dir/sounddevice_version.txt" 10 \
        "$SOVYX_DIAG_PYTHON" -c 'import sounddevice as sd, sys; print("sounddevice:", sd.__version__); print("location:", sd.__file__); print("python:", sys.version)'

    # ── pip show de pacotes críticos ─────────────────────────────────────
    for pkg in sounddevice numpy onnxruntime kokoro-onnx piper-phonemize; do
        run_step "E_pip_show_${pkg//-/_}" "$dir/pip_show_${pkg//-/_}.txt" 10 \
            "$SOVYX_DIAG_PYTHON" -m pip show "$pkg" 2>/dev/null
    done

    # ── dpkg — libportaudio/alsa-lib de sistema ──────────────────────────
    run_step "E_dpkg_audio_libs" "$dir/dpkg_portaudio.txt" 15 \
        bash -c 'dpkg -l 2>/dev/null | grep -iE "portaudio|alsa-lib|libasound2" || echo "dpkg unavailable"'

    # ── ldd libportaudio (bundle do wheel E sistema) ─────────────────────
    # Bundle: procurar dentro do venv.
    local venv_base
    venv_base=$(dirname "$(dirname "$SOVYX_DIAG_PYTHON")")
    {
        echo "--- venv base: $venv_base ---"
        find "$venv_base" -name "libportaudio*" -o -name "_sounddevice_data*" 2>/dev/null | head -20
        echo ""
        echo "--- system libportaudio ---"
        find /usr/lib /usr/lib64 /usr/lib/x86_64-linux-gnu -name "libportaudio*" 2>/dev/null | head -10
    } > "$dir/libportaudio_search.txt" 2>&1
    header_write "$dir/libportaudio_search.txt" "E_libportaudio_search" "find libportaudio" 0 0

    # ldd do primeiro libportaudio encontrado (bundle preferível).
    local bundle_pa sys_pa
    bundle_pa=$(find "$venv_base" -name "libportaudio*" 2>/dev/null | head -1)
    sys_pa=$(find /usr/lib /usr/lib64 /usr/lib/x86_64-linux-gnu -name "libportaudio*" 2>/dev/null | head -1)
    if [[ -n "$bundle_pa" ]]; then
        run_step "E_ldd_bundle_pa" "$dir/ldd_portaudio_bundle.txt" 15 ldd "$bundle_pa"
    fi
    if [[ -n "$sys_pa" ]]; then
        run_step "E_ldd_system_pa" "$dir/ldd_portaudio_system.txt" 15 ldd "$sys_pa"
    fi

    # ── Capturas W10-W13, W14c ───────────────────────────────────────────
    if [[ "$SOVYX_DIAG_FLAG_SKIP_CAPTURES" = "1" ]]; then
        log_info "skipping E captures (--skip-captures)"
        manifest_append "E_layer" "E_portaudio/" "Camada E parcial (sem .wav)." "E_P1-E_P5"
        return
    fi

    prompt_user "Próximas capturas PortAudio (5 wavs). Frase: 'Sovyx, me ouça agora: um, dois, três, quatro, cinco.'" 30 || true

    # W10 — PortAudio default (qualquer que seja o host API default)
    _capture_sd "W10_pa_systemdefault_16k_1ch" "" 16000 1 7

    # W11 — PortAudio substring "default" (equivalente ao device index 7 do log E4)
    _capture_sd "W11_pa_default_16k_1ch" "default" 16000 1 7

    # W12 — PortAudio substring "pipewire" (equivalente ao index 6)
    _capture_sd "W12_pa_pipewire_16k_1ch" "pipewire" 16000 1 7

    # W13 — PortAudio substring "SN6180 Analog" (equivalente ao index 4; hardware direto)
    _capture_sd "W13_pa_sn6180_48k_2ch" "SN6180 Analog" 48000 2 7

    # V4.3 — W14a/W14b cobertura de 44.1 kHz. Sovyx prod pode estar
    # negociando 44.1k (formato HD-Audio default em algumas placas) e
    # W10-W13 (16k/48k) deixariam blind spot. Se 44.1k captura silêncio
    # mas 48k não → resampler de saída do PortAudio quebrou.
    _capture_sd "W14a_pa_default_44k1_1ch" "default" 44100 1 7
    _capture_sd "W14b_pa_default_44k1_2ch" "default" 44100 2 7

    # W14c — silêncio via PortAudio default
    _capture_sd "W14c_pa_silence_16k_1ch" "default" 16000 1 2 "silence"

    manifest_append "E_layer" "E_portaudio/" \
        "Camada E — PortAudio / sounddevice. venv resolution + pip + ldd + capturas W10-W13, W14c." \
        "E_P1-E_P5"
}
