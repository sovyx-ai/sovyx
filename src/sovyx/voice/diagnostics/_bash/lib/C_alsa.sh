#!/usr/bin/env bash
# lib/C_alsa.sh — Camada C: ALSA direto (espaço de usuário, nível mais baixo).
#
# Objetivo CRÍTICO: provar que o sinal está vivo ANTES de qualquer servidor
# de áudio. É a única camada que não passa pelo PipeWire. Se W1-W4 vivos
# e W5-W9 mortos → raiz no PipeWire. Se W1-W4 mortos → raiz no
# hardware/codec/kernel.
#
# Hipóteses (§2 do plano):
#   C1 — Controle Capture/Internal Mic Boost em 0.
#   C2 — Mixer switch (Capture Switch) desligado.
#   C3 — Input source errado (Rear Mic em vez de Internal Mic).
#   C4 — Banda limitada no ADC (DSP do codec).
#   C5 — plughw:1,0 saudável mas default/pipewire destroi.
#   C6 — UCM carregou profile com DSP voicecall que corta banda.
#
# Roda em S_OFF. Ordem CRÍTICA (anti-contaminação):
#   1. amixer/alsactl/UCM (leitura pura)
#   2. lsof/fuser do /dev/snd ANTES de qualquer arecord
#   3. W1 → cool 1s → W2 → cool 1s → W3 → cool 1s → W4 → W14_silence → cool 30s → W4b
#   Cada arecord compete pelo mesmo hw:1,0, então cooldown.

run_layer_C() {
    local dir="$SOVYX_DIAG_OUTDIR/C_alsa"
    local cap_dir="$dir/captures"
    mkdir -p "$dir" "$cap_dir"
    log_info "=== Layer C: ALSA direto (S_OFF) ==="

    # ── Mixer state ──────────────────────────────────────────────────────
    # Identifica card do mic interno (tipicamente card 1 com SN6180).
    #
    # AUDIT v3 — regex fix. A versão anterior era permissiva demais
    # (`SN6180|Internal|HDA|Realtek|Analog`) e, em laptops com placa HDMI
    # cujo longname começa com `HDA ATI HDMI`, retornava o card HDMI em
    # vez do analógico — rotearia a bateria inteira W1-W4 para OUT-only
    # HDMI, falhando todas as capturas com "Invalid argument", mas os
    # artifacts indicariam `hw:0,0` e o analista concluiria "mic morto"
    # incorretamente.
    #
    # Estratégia v3: SCAN TODOS os cards e escolher o que tem capture
    # stream (pcm*c). Empate desempata por presença de "SN6180|Internal
    # Mic|Analog" no longname. Rejeitar qualquer card que case "HDMI",
    # "Digital Output", "S/PDIF" no longname — esses são playback-only.
    # Cada card candidato é REGISTRADO em `cards_candidates.txt` para
    # auditoria do raciocínio.
    local target_card=""
    local cards_candidates="$dir/cards_candidates.txt"
    : > "$cards_candidates"
    if [[ -r /proc/asound/cards ]]; then
        target_card=$(LC_ALL=C awk '
            # Parse: lines alternate as
            #   " 1 [Generic_1      ]: HDA-Intel - HD-Audio Generic\n"
            #   "                      HDA Intel PCH at 0xf7318000 irq 143\n"
            # Strategy: for each index line, pair with the longname line,
            # check whether card has a capture PCM via /proc/asound/cardN,
            # then rank candidates analog-mic-likely.
            /^ *[0-9]+ / {
                gsub(/^ *|\[.*$/, "", $1); cid=$1
                getline line2
                combined = $0 " " line2
                has_capture = (system("ls /proc/asound/card" cid "/pcm*c >/dev/null 2>&1") == 0)
                # Hard-reject playback-only cards by longname hints.
                if (combined ~ /HDMI|Digital Output|S\/PDIF|DP Audio/) next
                if (!has_capture) next
                # Score analog-mic affinity. Higher wins.
                score = 0
                if (combined ~ /SN6180/)        score += 50
                if (combined ~ /Internal Mic/)  score += 30
                if (combined ~ /Analog/)        score += 20
                if (combined ~ /Conexant/)      score += 15
                if (combined ~ /Realtek/)       score += 10
                if (combined ~ /HDA/)           score += 5
                printf "%s\t%s\n", score, cid > "'"$cards_candidates"'"
                if (!best_set || score > best_score) {
                    best_score = score; best_cid = cid; best_set = 1
                }
            }
            END { if (best_set) print best_cid }
        ' /proc/asound/cards 2>/dev/null)
    fi
    if [[ -z "$target_card" ]]; then
        log_warn "target_card unresolved — ALSA /proc enumeration produced no capture-capable card"
        echo "UNRESOLVED" > "$dir/target_card.txt"
        manifest_append "C_target_card" "C_alsa/cards_candidates.txt" \
            "Resolução do card alvo falhou — nenhum card com PCM capture foi encontrado. Analista deve inspecionar cards_candidates.txt + /proc/asound/cards manualmente." "C1-C6"
        # Refuse to proceed with arbitrary fallback; audit-critical step.
        log_error "C layer aborting: no analog-capture card resolved (no silent fallback)"
        manifest_append "C_layer" "C_alsa/" "Camada C abortada — target_card unresolved." "C1-C6"
        alert_append "error" "ALSA target card unresolved; C layer refused silent fallback"
        return
    fi
    echo "$target_card" > "$dir/target_card.txt"
    log_info "ALSA target card resolved: $target_card (candidates: $(wc -l < "$cards_candidates" 2>/dev/null | tr -d ' '))"

    if tool_has amixer >/dev/null; then
        run_step "C_amixer_scontents" "$dir/amixer_card${target_card}_scontents.txt" 10 \
            amixer -c "$target_card" scontents
        run_step "C_amixer_contents"  "$dir/amixer_card${target_card}_contents.txt"  10 \
            amixer -c "$target_card" contents
        run_step "C_amixer_dB"        "$dir/amixer_card${target_card}_dB.txt"        10 \
            amixer -c "$target_card" -M contents
        manifest_append "C_amixer" "C_alsa/amixer_card${target_card}_*.txt" \
            "Estado do mixer ALSA. Alimenta C1, C2." "C1/C2"
    fi

    if tool_has alsactl >/dev/null; then
        # Correção do plano v1: usar --file com caminho explícito.
        run_step "C_alsactl_store" "$dir/alsactl_store_card${target_card}.ini" 15 \
            alsactl --file "$dir/alsactl_store_card${target_card}.ini" store "$target_card"
        # Se alsactl store foi bem-sucedido, o arquivo contém dump canônico.
        # V4.3: alsactl info global mostra todos os controles + valor
        # corrente — útil pra detectar mute/0-volume sem ter que parsear
        # o INI.
        run_step "C_alsactl_info" "$dir/alsactl_info.txt" 10 \
            bash -c 'alsactl --no-ucm info 2>&1 || alsactl info 2>&1'
    fi

    # V4.3 — coletar config ALSA user/system. Hipóteses C5/C7:
    #   - ~/.asoundrc redefine !pcm.!default → sounddevice abre device
    #     diferente do que /proc/asound mostra como hardware
    #   - /etc/asound.conf system-wide override
    #   - ~/.asound.state mixer state file (audit-trail de quem mexeu)
    # Sem esses, "sounddevice abriu plughw:1,0" pode na verdade ser
    # plughw:0,0 via plugin chain — analyst nunca saberia.
    {
        echo "=== ~/.asoundrc ==="
        if [[ -r "$HOME/.asoundrc" ]]; then
            cat "$HOME/.asoundrc"
        else
            echo "(no ~/.asoundrc — defaults from /etc/asound.conf or ALSA built-in)"
        fi
        echo ""
        echo "=== /etc/asound.conf ==="
        if [[ -r /etc/asound.conf ]]; then
            cat /etc/asound.conf
        else
            echo "(no /etc/asound.conf — defaults from ALSA built-in)"
        fi
        echo ""
        echo "=== /etc/alsa/conf.d/ ==="
        if [[ -d /etc/alsa/conf.d ]]; then
            ls -la /etc/alsa/conf.d/ 2>&1
            for cf in /etc/alsa/conf.d/*.conf; do
                [[ -r "$cf" ]] || continue
                echo "--- $cf ---"
                cat "$cf"
            done
        else
            echo "(no /etc/alsa/conf.d/)"
        fi
        echo ""
        echo "=== ~/.asound.state (mixer state file) ==="
        if [[ -r "$HOME/.asound.state" ]]; then
            cat "$HOME/.asound.state"
        else
            echo "(no ~/.asound.state)"
        fi
    } > "$dir/alsa_user_config.txt" 2>&1
    header_write "$dir/alsa_user_config.txt" "C_alsa_user_config" \
        "asoundrc + asound.conf + conf.d + asound.state" 0 0
    manifest_append "C_alsa_user_config" "C_alsa/alsa_user_config.txt" \
        "Config ALSA usuário+sistema. Detecta !pcm.!default redefining capture device + plugin chain. Crítico se sounddevice abriu device diferente do /proc/asound visível." \
        "C5/C7"

    # arecord -L resolvido — lista TODAS as PCMs lógicas (incluindo
    # plugins definidos em .asoundrc). Diff vs arecord -l (hw apenas)
    # mostra plugin chain.
    if tool_has arecord >/dev/null; then
        run_step "C_arecord_L_logical" "$dir/arecord_L_logical_pcms.txt" 10 \
            bash -c 'arecord -L 2>&1'
        run_step "C_arecord_l_hardware" "$dir/arecord_l_hardware_pcms.txt" 10 \
            bash -c 'arecord -l 2>&1'
    fi

    # ── UCM — Use Case Manager (hipótese C6) ──────────────────────────────
    if tool_has alsaucm >/dev/null; then
        local card_id
        card_id=$(cat "/proc/asound/card${target_card}/id" 2>/dev/null || echo "")
        if [[ -n "$card_id" ]]; then
            run_step "C_ucm_verbs"     "$dir/ucm_verbs.txt"     10 alsaucm -c "$card_id" list _verbs
            run_step "C_ucm_devices"   "$dir/ucm_devices.txt"   10 alsaucm -c "$card_id" list _devices
            run_step "C_ucm_modifiers" "$dir/ucm_modifiers.txt" 10 alsaucm -c "$card_id" list _modifiers
            manifest_append "C_ucm" "C_alsa/ucm_*.txt" \
                "UCM profile ativo. Alimenta C6 (DSP voicecall cortando banda)." "C6"
        fi
    fi
    if [[ -d /usr/share/alsa/ucm2 ]]; then
        run_step "C_ucm_system_profiles" "$dir/ucm_system_profiles.txt" 10 \
            bash -c 'ls -la /usr/share/alsa/ucm2/ 2>/dev/null || echo "no ucm2 dir"'
    fi

    # Codec dump específico do target card.
    if [[ -d "/proc/asound/card${target_card}" ]]; then
        for codec_file in /proc/asound/card${target_card}/codec#*; do
            [[ -r "$codec_file" ]] || continue
            local cname
            cname=$(basename "$codec_file" | tr '#' '_')
            run_step "C_codec_dump_${cname}" "$dir/codec_dump_${cname}.txt" 5 \
                cat "$codec_file"
        done
    fi

    # ── Capturas de áudio (se --skip-captures não setado) ────────────────
    if [[ "$SOVYX_DIAG_FLAG_SKIP_CAPTURES" = "1" ]]; then
        log_info "skipping captures (--skip-captures)"
        manifest_append "C_layer" "C_alsa/" "Camada C parcial (sem .wav)." "C1-C6"
        return
    fi

    if ! tool_has arecord >/dev/null; then
        log_warn "arecord unavailable — all C captures skipped"
        manifest_append "C_layer" "C_alsa/" "Camada C parcial — arecord ausente." "C1-C6"
        return
    fi

    # ORDEM ANTI-CONTAMINAÇÃO: lsof/fuser do /dev/snd ANTES de capturas.
    run_step "C_lsof_snd_pre" "$dir/lsof_snd_pre_captures.txt" 10 \
        bash -c 'lsof /dev/snd/* 2>/dev/null || echo "no open handles"'
    run_step "C_fuser_snd_pre" "$dir/fuser_snd_pre_captures.txt" 10 \
        bash -c 'fuser -v /dev/snd/* 2>&1 || echo "no handles"'

    # Instrução ao usuário.
    local phrase='Sovyx, me ouça agora: um, dois, três, quatro, cinco.'
    prompt_emit_structured "speak" "$phrase" 4
    if ! prompt_user "Próximas capturas (5x): vou pedir que você fale '$phrase' por 4 segundos. Pressione ENTER quando pronto." 60; then
        log_warn "C captures: user did not confirm; proceeding blindly"
    fi

    # Captura arecord inline — NÃO usa run_step porque run_step prepend
    # cabeçalho texto via header_write, que corromperia o WAV binário.
    # Escreve timeline/runlog/manifest manualmente.
    _capture_arecord_v2() {
        local cid="$1" device="$2" rate="$3" channels="$4" fmt="$5" duration="$6" mode="${7:-}"
        local subdir="$cap_dir/$cid"
        mkdir -p "$subdir"
        local wav="$subdir/capture.wav"
        local meta="$subdir/capture.meta.json"
        local log="$subdir/arecord.log"

        if [[ "$mode" != "silence" ]]; then
            printf '\n\033[1;36m>>> [%s] Fale agora (%ss): "%s"\033[0m\n' "$cid" "$duration" "$phrase" >&2
        else
            printf '\n\033[1;36m>>> [%s] Mantenha SILÊNCIO (%ss)\033[0m\n' "$cid" "$duration" >&2
        fi

        local start_utc start_mono end_utc end_mono duration_ms rc retry=0 max_retry=1
        while :; do
            start_utc=$(now_utc_ns)
            start_mono=$(now_monotonic_ns)
            # AUDIT v3: LC_ALL=C forces arecord error messages into
            # English so the busy/occupied grep below matches
            # deterministically regardless of user locale (pt-BR
            # produces `ocupado`, de-DE produces `Gerät belegt`, etc.).
            # Without this, the retry path is locale-dependent.
            LC_ALL=C timeout --preserve-status --kill-after=5 $((duration + 5)) \
                arecord -D "$device" -f "$fmt" -r "$rate" -c "$channels" -d "$duration" "$wav" \
                > "$log" 2>&1
            rc=$?
            end_utc=$(now_utc_ns)
            end_mono=$(now_monotonic_ns)
            duration_ms=$(( (end_mono - start_mono) / 1000000 ))

            if [[ $rc -eq 0 || $retry -ge $max_retry ]]; then
                break
            fi
            # Resource busy → aguarda + retry 1x.
            # AUDIT v3: extended regex to cover non-English error codes
            # (Linux ALSA prints raw strings below errno in busy path).
            if grep -qiE 'busy|occupied|resource temporarily unavailable|device or resource' "$log" 2>/dev/null; then
                log_warn "arecord $cid returned busy; retrying in 2s"
                sleep 2
                retry=$((retry + 1))
                continue
            fi
            break
        done

        # Metadados da captura em JSON separado.
        cat > "$meta" <<EOF
{
  "capture_id": "$cid",
  "state": "$SOVYX_DIAG_STATE",
  "layer": "C_alsa",
  "tool": "arecord",
  "device": "$device",
  "sample_rate": $rate,
  "channels": $channels,
  "format": "$fmt",
  "duration_s_requested": $duration,
  "duration_s_actual": $(awk -v ms="$duration_ms" 'BEGIN{printf "%.3f", ms/1000}'),
  "duration_ms_actual": $duration_ms,
  "retcode": $rc,
  "start_utc": "$start_utc",
  "start_monotonic_ns": $start_mono,
  "end_utc": "$end_utc",
  "end_monotonic_ns": $end_mono,
  "mode": "$mode",
  "retries": $retry
}
EOF

        # Timeline + RUNLOG entries. v2 (audit post-SVX-VOICE-LINUX-
        # 20260422): enforce wall-clock floor on the actual recording
        # window so a ``resource busy`` arecord that exits after 200 ms
        # is not silently marked ``ok`` — fast-fail pollutes the spectral
        # metrics downstream because a 7 s phrase analysed as 200 ms of
        # silence skews every statistic.
        local min_duration_ms=$(( duration * 1000 * 95 / 100 ))  # 95% floor
        local duration_pass=0
        [[ $duration_ms -ge $min_duration_ms ]] && duration_pass=1
        # arecord writes ~44-byte WAV header even on empty streams; the
        # old ``! -s $wav`` test accepted those as valid. Require at
        # least half a second of samples to pass the size gate.
        local min_wav_bytes=$(( 44 + rate * channels * 2 / 2 ))
        local wav_size
        wav_size=$(stat -c%s "$wav" 2>/dev/null || echo 0)
        local size_pass=0
        [[ $wav_size -ge $min_wav_bytes ]] && size_pass=1

        local note
        if [[ $rc -ne 0 ]]; then
            if [[ $rc -eq 124 ]]; then
                note="TIMEOUT"; (( SOVYX_DIAG_STEPS_TIMEOUT++ )) || true
            elif [[ $rc -eq 143 || $rc -eq 137 ]]; then
                # V4.3: --preserve-status repassa signal rc; 143/137 são
                # timeouts via SIGTERM/SIGKILL — manter semantica TIMEOUT.
                note="TIMEOUT_via_signal"; (( SOVYX_DIAG_STEPS_TIMEOUT++ )) || true
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

        # Per-capture visible status line so the operator sees each
        # result inline rather than having to read the summary.
        if [[ "$note" = "ok" ]]; then
            printf '    \033[32m✓ captured %s.%03ss (rc=0, %sB WAV)\033[0m\n' \
                $((duration_ms / 1000)) \
                $((duration_ms % 1000)) \
                "$wav_size" >&2
        else
            printf '    \033[31m✗ FAILED rc=%s note=%s actual=%s.%03ss size=%sB\033[0m\n' \
                "$rc" "$note" \
                $((duration_ms / 1000)) \
                $((duration_ms % 1000)) \
                "$wav_size" >&2
        fi
        timeline_append "C_${cid}" "$SOVYX_DIAG_STATE" "$start_utc" "$start_mono" \
                        "$end_utc" "$end_mono" "$duration_ms" \
                        "arecord -D $device -f $fmt -r $rate -c $channels -d $duration" \
                        "$rc" "$wav" "$note"
        {
            printf '[%s] step=C_%s state=%s retcode=%s duration_ms=%s out=%s notes=%s\n' \
                "$start_utc" "$cid" "$SOVYX_DIAG_STATE" "$rc" \
                "$duration_ms" "$wav" "$note"
        } >> "$SOVYX_DIAG_RUNLOG"

        # Roda análise (analyze_wav.py encadeia silero_probe.py automaticamente).
        if [[ -s "$wav" && -n "$SOVYX_DIAG_PYTHON" ]]; then
            "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/analyze_wav.py" \
                --wav "$wav" --state "$SOVYX_DIAG_STATE" \
                --source "$device" --capture-id "$cid" \
                --monotonic-ns "$start_mono" --utc-iso-ns "$start_utc" \
                --out "$subdir/analysis.json" 2>"$subdir/analyze.log" || \
                log_warn "analyze_wav failed for $cid"
            # Silero probe isolado (paralelo ao que analyze_wav já faz).
            "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/silero_probe.py" \
                --wav "$wav" --out "$subdir/silero.json" \
                2>>"$subdir/analyze.log" || true
            # wav_header dump.
            "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/wav_header.py" "$wav" \
                > "$subdir/wav_header.json" 2>>"$subdir/analyze.log" || true
        fi

        manifest_append "C_${cid}" "C_alsa/captures/${cid}/" \
            "Captura direta ALSA — device=$device, rate=${rate}Hz, ${channels}ch, $fmt. ${mode:-voz}." \
            "C1-C6"

        # Cooldown entre capturas que competem pelo mesmo hw.
        sleep 1
    }

    # W1 — hw:1,0 48k 2ch S16_LE (baseline do codec)
    _capture_arecord_v2 "W1_hw${target_card}0_48k_s16_2ch" \
        "hw:${target_card},0" 48000 2 S16_LE 7

    # W2 — hw:1,0 48k 2ch S32_LE (se S16=0 mas S32=vivo → truncamento)
    _capture_arecord_v2 "W2_hw${target_card}0_48k_s32_2ch" \
        "hw:${target_card},0" 48000 2 S32_LE 7

    # W3 — plughw:1,0 48k 1ch S16_LE (downmix ALSA direto)
    _capture_arecord_v2 "W3_plughw${target_card}0_48k_s16_1ch" \
        "plughw:${target_card},0" 48000 1 S16_LE 7

    # W4 — plughw:1,0 16k 1ch S16_LE (resample ALSA nativo — caminho similar ao Sovyx)
    _capture_arecord_v2 "W4_plughw${target_card}0_16k_s16_1ch" \
        "plughw:${target_card},0" 16000 1 S16_LE 7

    # W14 — silêncio (baseline de ruído direto do codec)
    _capture_arecord_v2 "W14_silence_plughw${target_card}0" \
        "plughw:${target_card},0" 16000 1 S16_LE 2 "silence"

    # Pausa de 30s para teste de intermitência (W4 vs W4b).
    log_info "30s cooldown antes de W4b (teste de intermitência)..."
    sleep 30

    _capture_arecord_v2 "W4b_plughw${target_card}0_16k_s16_1ch_repeat" \
        "plughw:${target_card},0" 16000 1 S16_LE 7

    # lsof/fuser pós-captura — detecta se alguém ficou segurando /dev/snd.
    run_step "C_lsof_snd_post" "$dir/lsof_snd_post_captures.txt" 10 \
        bash -c 'lsof /dev/snd/* 2>/dev/null || echo "no open handles"'
    run_step "C_fuser_snd_post" "$dir/fuser_snd_post_captures.txt" 10 \
        bash -c 'fuser -v /dev/snd/* 2>&1 || echo "no handles"'

    manifest_append "C_layer" "C_alsa/" \
        "Camada C — ALSA direto (S_OFF). Bateria W1/W2/W3/W4/W4b + silence. Análise em cada captures/<id>/analysis.json." \
        "C1-C6"
}
