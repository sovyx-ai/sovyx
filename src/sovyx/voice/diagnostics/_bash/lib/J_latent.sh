#!/usr/bin/env bash
# lib/J_latent.sh — Camada J: problemas latentes, auditoria ampla.
#
# Objetivo: examinar riscos ortogonais. Detecta disco cheio, OOM, thermal
# throttling, governor powersave, TLP agressivo, coredumps, PSI pressure.
# Roda em S_IDLE.

run_layer_J() {
    local dir="$SOVYX_DIAG_OUTDIR/J_latent"
    mkdir -p "$dir"
    log_info "=== Layer J: latent ==="

    # Disco + memória.
    run_step "J_df"         "$dir/df.txt"         10 df -h
    run_step "J_du_sovyx"   "$dir/du_sovyx.txt"   10 bash -c 'du -sh ~/.sovyx 2>/dev/null || echo "no ~/.sovyx"'
    run_step "J_free"       "$dir/free.txt"        5 free -h
    run_step "J_vmstat"     "$dir/vmstat.txt"     10 vmstat 1 3
    run_step "J_meminfo"    "$dir/meminfo.txt"     5 bash -c 'head -30 /proc/meminfo'
    run_step "J_swap"       "$dir/swap.txt"        5 bash -c 'swapon --show; echo ""; cat /proc/swaps'

    # Uptime + last.
    run_step "J_uptime"     "$dir/uptime.txt"      5 uptime
    run_step "J_last"       "$dir/last.txt"       10 bash -c 'last -n 10 2>&1 || echo "last unavailable"'
    run_step "J_last_boot"  "$dir/last_boot.txt"  10 bash -c 'last reboot -n 5 2>&1 || echo "last reboot unavailable"'

    # Units + timers.
    run_step "J_failed_units" "$dir/failed_units.txt" 10 \
        bash -c 'systemctl --failed --no-pager 2>&1 | head -40'
    run_step "J_timers" "$dir/timers.txt" 10 \
        bash -c 'systemctl list-timers --no-pager 2>&1 | head -60'

    # dmesg errors.
    run_step "J_dmesg_errors" "$dir/dmesg_errors.txt" 15 \
        bash -c 'journalctl -k --since "7 days ago" --no-pager 2>/dev/null | grep -iE "oom|panic|segfault|tainted|xrun|thermal|throttl" | tail -200 || true'

    # Coredumps recentes.
    run_step "J_coredumps" "$dir/coredumps.txt" 10 \
        bash -c 'find /var/crash /var/lib/systemd/coredump -type f -mtime -14 2>/dev/null || echo "none"'

    # PSI (pressure stall info) — detecta contenção sustentada.
    if [[ -d /proc/pressure ]]; then
        run_step "J_psi" "$dir/psi.txt" 5 \
            bash -c 'for f in /proc/pressure/*; do echo "--- $f ---"; cat "$f" 2>/dev/null; done'
    fi

    # Thermal.
    if [[ -d /sys/class/thermal ]]; then
        run_step "J_thermal" "$dir/thermal.txt" 10 \
            bash -c '
                for zone in /sys/class/thermal/thermal_zone*; do
                    [[ -d "$zone" ]] || continue
                    tzname=$(cat "$zone/type" 2>/dev/null)
                    temp=$(cat "$zone/temp" 2>/dev/null)
                    printf "%s: type=%s temp=%s\n" "$(basename "$zone")" "$tzname" "$temp"
                done
            '
    fi
    if tool_has sensors >/dev/null; then
        run_step "J_sensors" "$dir/sensors.txt" 10 sensors
    fi

    # CPU governor + freq atual.
    run_step "J_cpufreq" "$dir/cpufreq.txt" 10 \
        bash -c '
            if [[ -d /sys/devices/system/cpu/cpu0/cpufreq ]]; then
                echo "--- governors ---"
                cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u
                echo ""
                echo "--- current freqs ---"
                for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq; do
                    [[ -r "$cpu" ]] && printf "%s: %s kHz\n" "$(dirname "$cpu" | xargs basename | xargs dirname | xargs basename)" "$(cat "$cpu")"
                done
            else
                echo "no cpufreq"
            fi
        '

    # TLP stat (laptop power management).
    if tool_has tlp-stat >/dev/null; then
        run_step "J_tlp_stat" "$dir/tlp_stat.txt" 30 \
            bash -c 'tlp-stat -s -c -p 2>&1 | head -200 || echo "tlp-stat failed"'
        manifest_append "J_tlp" "J_latent/tlp_stat.txt" \
            "TLP power management status — agressivo pode causar autosuspend de codec." "B2/B4"
    fi

    # Power supply.
    run_step "J_power_supply" "$dir/power_supply.txt" 5 \
        bash -c '
            echo "--- AC ---"
            for f in /sys/class/power_supply/AC*/online; do
                [[ -r "$f" ]] && printf "%s: %s\n" "$f" "$(cat "$f")"
            done
            echo ""
            echo "--- Battery ---"
            for f in /sys/class/power_supply/BAT*/status /sys/class/power_supply/BAT*/capacity; do
                [[ -r "$f" ]] && printf "%s: %s\n" "$f" "$(cat "$f")"
            done
        '

    # Powertop (opt-in — takes 3+ seconds).
    if [[ "$SOVYX_DIAG_FLAG_WITH_POWERTOP" = "1" ]] && tool_has powertop >/dev/null; then
        run_step "J_powertop" "$dir/powertop.csv" 15 \
            bash -c 'powertop --time=3 --csv="$dir/powertop.csv" >/dev/null 2>&1 && cat "$dir/powertop.csv" 2>/dev/null || echo "powertop failed"'
    fi

    # Flatpak/Snap (duplicado para J porque pode afetar audio routing).
    run_step "J_flatpak_snap" "$dir/flatpak_snap.txt" 10 \
        bash -c 'echo "--- flatpak ---"; flatpak list 2>/dev/null || echo "no flatpak"; echo ""; echo "--- snap ---"; snap list 2>/dev/null || echo "no snap"'

    # ldconfig audio libs.
    if tool_has ldconfig >/dev/null; then
        run_step "J_ldconfig_audio" "$dir/ldconfig_audio.txt" 10 \
            bash -c 'ldconfig -p 2>/dev/null | grep -iE "asound|portaudio|pipewire|pulse" | head -30 || echo "none"'
    fi

    manifest_append "J_layer" "J_latent/" \
        "Camada J — latent: disco, memória, thermal, cpufreq, TLP, coredumps, PSI, Flatpak/Snap." \
        "latent"
}
