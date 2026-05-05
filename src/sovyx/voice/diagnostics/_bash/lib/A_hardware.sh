#!/usr/bin/env bash
# lib/A_hardware.sh — Camada A: hardware físico, codec, BIOS, USB/Bluetooth.
#
# Objetivo: provar que o dispositivo físico está vivo e não há concorrência.
# Roda em S_OFF (leitura pura de /proc, /sys; sem dependência de daemons).
#
# Hipóteses (§2 do plano):
#   A1 — Switch físico de privacidade do mic (Fn+F1 em VAIOs).
#   A2 — Mic interno com defeito.
#   A3 — BIOS com mic desabilitado.
#   A4 — Codec SN6180 em estado travado.
#   A5 — Feature flags do BIOS inválidas.
#   A6 — Hijack por Bluetooth headset / USB mic / HDMI audio.

run_layer_A() {
    local prev_state="$SOVYX_DIAG_STATE"
    # Camada A roda em S_OFF, mas não muda estado por conta própria.
    local dir="$SOVYX_DIAG_OUTDIR/A_hardware"
    mkdir -p "$dir"
    log_info "=== Layer A: hardware ==="

    # BIOS via dmidecode — requer sudo.
    if [[ "$SOVYX_DIAG_FLAG_WITH_SUDO" = "1" ]] && sudo -n true 2>/dev/null; then
        run_step "A_dmidecode" "$dir/dmidecode.txt" 20 \
            sudo dmidecode -t 0 -t 1 -t 2 -t 4
        manifest_append "A_dmidecode" "A_hardware/dmidecode.txt" \
            "BIOS vendor/version, placa, CPU. Alimenta A3 e A5." \
            "A3/A5"
    else
        echo "skipped — requires --with-sudo" > "$dir/dmidecode.txt"
        header_write "$dir/dmidecode.txt" "A_dmidecode" "dmidecode (skipped)" 126 0
        manifest_append "A_dmidecode" "A_hardware/dmidecode.txt" \
            "dmidecode PULADO (sem --with-sudo)." ""
    fi

    # PCI audio + USB.
    if tool_has lspci >/dev/null; then
        run_step "A_lspci_audio" "$dir/lspci_audio.txt" 15 \
            bash -c 'lspci -vv -nn -k 2>/dev/null | grep -A 30 -iE "audio|multimedia" || true'
        manifest_append "A_lspci_audio" "A_hardware/lspci_audio.txt" \
            "PCI audio devices + kernel drivers. Alimenta A4/A6." "A4/A6"
    fi
    if tool_has lsusb >/dev/null; then
        run_step "A_lsusb" "$dir/lsusb.txt" 15 lsusb -vt
        manifest_append "A_lsusb" "A_hardware/lsusb.txt" \
            "USB tree. Detecta USB mic/headset concorrente (A6)." "A6"
    fi

    # Bluetooth — hijack de default source.
    {
        echo "--- hciconfig ---"
        hciconfig -a 2>/dev/null || echo "hciconfig unavailable"
        echo ""
        echo "--- bluetoothctl show ---"
        bluetoothctl show 2>/dev/null || echo "bluetoothctl unavailable"
        echo ""
        echo "--- bluetoothctl devices Connected ---"
        bluetoothctl devices Connected 2>/dev/null || echo "no connected devices or cmd unavailable"
    } > "$dir/bluetooth.txt" 2>&1
    header_write "$dir/bluetooth.txt" "A_bluetooth" "hciconfig+bluetoothctl" 0 0
    manifest_append "A_bluetooth" "A_hardware/bluetooth.txt" \
        "Estado Bluetooth + devices conectados. Alimenta A6." "A6"

    # /proc/asound — a verdade nua do kernel sobre as placas.
    run_step "A_proc_asound_cards"   "$dir/proc_asound_cards.txt"   5 \
        bash -c 'cat /proc/asound/cards 2>/dev/null || echo "no /proc/asound/cards"'
    run_step "A_proc_asound_devices" "$dir/proc_asound_devices.txt" 5 \
        bash -c 'cat /proc/asound/devices 2>/dev/null || echo "no /proc/asound/devices"'

    # Codec dumps para cada card.
    if [[ -d /proc/asound ]]; then
        for card_dir in /proc/asound/card*; do
            [[ -d "$card_dir" ]] || continue
            local card_id
            card_id=$(basename "$card_dir")
            for codec_file in "$card_dir"/codec#*; do
                [[ -r "$codec_file" ]] || continue
                local cname
                cname=$(basename "$codec_file" | tr '#' '_')
                run_step "A_codec_${card_id}_${cname}" \
                    "$dir/codec_${card_id}_${cname}.txt" 5 \
                    cat "$codec_file"
            done
            if [[ -r "$card_dir/controls" ]]; then
                run_step "A_controls_${card_id}" "$dir/controls_${card_id}.txt" 5 \
                    cat "$card_dir/controls"
            fi
            if [[ -r "$card_dir/id" ]]; then
                cat "$card_dir/id" > "$dir/id_${card_id}.txt"
            fi
        done
    fi

    # hw_params + status por PCM. Só legível quando stream ativo — em S_OFF
    # tipicamente "closed"; em S_ACTIVE revela sample format, rate, period
    # negociados pelo codec.
    for params_file in /proc/asound/card*/pcm*c/sub0/hw_params; do
        [[ -r "$params_file" ]] || continue
        local tag
        tag=$(echo "$params_file" | tr '/' '_' | sed 's/^_//')
        run_step "A_hw_params_${tag}" "$dir/${tag}.txt" 5 cat "$params_file"
    done
    for status_file in /proc/asound/card*/pcm*c/sub0/status; do
        [[ -r "$status_file" ]] || continue
        local tag
        tag=$(echo "$status_file" | tr '/' '_' | sed 's/^_//')
        run_step "A_pcm_status_${tag}" "$dir/${tag}.txt" 5 cat "$status_file"
    done

    # aplay/arecord -l/-L.
    if tool_has arecord >/dev/null; then
        run_step "A_arecord_l" "$dir/arecord_l.txt" 10 arecord -l
        run_step "A_arecord_L" "$dir/arecord_L.txt" 10 arecord -L
    fi
    if tool_has aplay >/dev/null; then
        run_step "A_aplay_l" "$dir/aplay_l.txt" 10 aplay -l
        run_step "A_aplay_L" "$dir/aplay_L.txt" 10 aplay -L
    fi

    # alsa-info se disponível (saída canônica de bugs ALSA).
    if command -v alsa-info.sh >/dev/null 2>&1; then
        run_step "A_alsa_info" "$dir/alsa_info.txt" 60 \
            bash -c 'alsa-info.sh --no-upload --with-aplay --with-amixer --stdout 2>&1'
        manifest_append "A_alsa_info" "A_hardware/alsa_info.txt" \
            "Dump canônico ALSA — melhor evidência para bugs de codec/driver." "A1-A5"
    fi

    # Módulos do kernel de áudio carregados.
    run_step "A_lsmod_snd" "$dir/lsmod_snd.txt" 5 \
        bash -c 'lsmod 2>/dev/null | grep -iE "snd|sof" || echo "no snd/sof modules"'
    for mod in snd_hda_intel snd_hda_codec_realtek snd_hda_codec_hdmi snd_soc_sof_intel_pci; do
        if lsmod 2>/dev/null | grep -q "^$mod "; then
            run_step "A_modinfo_${mod}" "$dir/modinfo_${mod}.txt" 10 modinfo "$mod"
        fi
    done

    manifest_append "A_layer" "A_hardware/" \
        "Camada A — hardware físico, codec, BIOS, USB/Bluetooth. Leitura pura, não-invasiva." \
        "A1-A6"

    # Restore anterior state (no-op — não mudamos).
    SOVYX_DIAG_STATE="$prev_state"
}
