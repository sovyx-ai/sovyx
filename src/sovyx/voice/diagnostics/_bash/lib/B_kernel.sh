#!/usr/bin/env bash
# lib/B_kernel.sh — Camada B: kernel, módulos, power, sched, apt history.
#
# Objetivo: descartar regressão de kernel, módulo travado, DKMS quebrado,
# power management agressivo, tainting, jitter de scheduler, update recente.
# Roda em S_OFF.
#
# Hipóteses (§2 do plano):
#   B1 — Regressão no driver HDA para SN6180.
#   B2 — snd_hda_intel.power_save adormecendo o codec.
#   B3 — Kernel taint ou erro em dmesg.
#   B4 — Runtime PM agressivo (power/control=auto).
#   B5 — PREEMPT_DYNAMIC / HZ baixo causando jitter.
#   B6 — DKMS quebrado → kernel tainted.
#   B7 — Update recente de kernel/audio → regressão.

run_layer_B() {
    local dir="$SOVYX_DIAG_OUTDIR/B_kernel"
    mkdir -p "$dir"
    log_info "=== Layer B: kernel ==="

    # Identidade do kernel + distro.
    run_step "B_uname"       "$dir/uname.txt"       5  uname -a
    run_step "B_os_release"  "$dir/os_release.txt"  5  cat /etc/os-release
    run_step "B_mint_ver"    "$dir/mint_version.txt" 5 \
        bash -c 'cat /etc/mint-version 2>/dev/null || echo "not Mint"'
    run_step "B_lsb_release" "$dir/lsb_release.txt" 5 \
        bash -c 'lsb_release -a 2>/dev/null || cat /etc/lsb-release 2>/dev/null || echo "no lsb"'

    # Taint + proc version.
    run_step "B_tainted"      "$dir/tainted.txt"      5 cat /proc/sys/kernel/tainted
    run_step "B_proc_version" "$dir/proc_version.txt" 5 cat /proc/version

    # DKMS status.
    if tool_has dkms >/dev/null; then
        run_step "B_dkms_status" "$dir/dkms_status.txt" 10 dkms status
    fi

    # dmesg — últimas 24h filtradas por áudio.
    if tool_has dmesg >/dev/null; then
        local restrict=1
        [[ -r /proc/sys/kernel/dmesg_restrict ]] && \
            restrict=$(cat /proc/sys/kernel/dmesg_restrict 2>/dev/null || echo 1)
        if [[ "$restrict" = "0" ]]; then
            run_step "B_dmesg_audio" "$dir/dmesg_audio.txt" 20 \
                bash -c 'dmesg --human --level=err,warn,info 2>/dev/null | grep -iE "snd|hda|sof|audio|alsa|pipewire|wireplumber|pulse|dma|xrun|denied" | tail -500'
        elif [[ "$SOVYX_DIAG_FLAG_WITH_SUDO" = "1" ]] && sudo -n true 2>/dev/null; then
            run_step "B_dmesg_audio" "$dir/dmesg_audio.txt" 20 \
                bash -c 'sudo dmesg --human --level=err,warn,info 2>/dev/null | grep -iE "snd|hda|sof|audio|alsa|pipewire|wireplumber|pulse|dma|xrun|denied" | tail -500'
        else
            echo "dmesg restricted (dmesg_restrict=$restrict) and no --with-sudo" > "$dir/dmesg_audio.txt"
            header_write "$dir/dmesg_audio.txt" "B_dmesg_audio" "dmesg (restricted)" 126 0
        fi
    fi

    # journalctl -k (kernel) últimas 24h — unprivileged via journald.
    if tool_has journalctl >/dev/null; then
        run_step "B_journalctl_kernel_24h" "$dir/journalctl_kernel.txt" 30 \
            bash -c 'journalctl -k --since "24 hours ago" --no-pager 2>&1 | grep -iE "snd|hda|sof|audio|alsa|pipewire|wireplumber|pulse|dma|xrun|denied" | tail -500 || true'
        run_step "B_journalctl_err_7d" "$dir/journalctl_err_7d.txt" 30 \
            bash -c 'journalctl -p err --since "7 days ago" --no-pager 2>&1 | tail -500 || true'
    fi

    # Parâmetros do módulo snd_hda_intel.
    if [[ -d /sys/module/snd_hda_intel/parameters ]]; then
        run_step "B_snd_hda_intel_params" "$dir/module_params_snd_hda_intel.txt" 5 \
            bash -c 'for f in /sys/module/snd_hda_intel/parameters/*; do printf "%s=%s\n" "$(basename "$f")" "$(cat "$f" 2>/dev/null)"; done'
    fi

    # Power management dos cards.
    {
        echo "--- /sys/class/sound/card*/device/power/ ---"
        for p in /sys/class/sound/card*/device/power; do
            [[ -d "$p" ]] || continue
            echo ""
            echo "=== $p ==="
            for f in autosuspend autosuspend_delay_ms control runtime_status runtime_suspended_time runtime_active_time; do
                [[ -r "$p/$f" ]] && printf "  %s=%s\n" "$f" "$(cat "$p/$f" 2>/dev/null)"
            done
        done
        echo ""
        echo "--- parent PCI power ---"
        for p in /sys/class/sound/card*/device/../power; do
            [[ -d "$p" ]] || continue
            echo ""
            echo "=== $p ==="
            for f in control runtime_status autosuspend_delay_ms; do
                [[ -r "$p/$f" ]] && printf "  %s=%s\n" "$f" "$(cat "$p/$f" 2>/dev/null)"
            done
        done
    } > "$dir/power_states.txt" 2>&1
    header_write "$dir/power_states.txt" "B_power_states" "sys/class/sound power readout" 0 0
    manifest_append "B_power_states" "B_kernel/power_states.txt" \
        "Autosuspend do codec. Alimenta B2, B4." "B2/B4"

    # Kernel cmdline.
    run_step "B_cmdline" "$dir/cmdline.txt" 5 cat /proc/cmdline

    # Interrupções — delta entre t=0 e t=30s para detectar stall/underrun do HDA.
    # Roda em subshell background; PID guardado para `wait` específico no final
    # (sem argumentos, `wait` esperaria os followers que nunca terminam).
    # AUDIT v3 — interrupts_t30 + delta are CRITICAL forensic artifacts
    # but the previous version:
    #   (1) never appended them to manifest.jsonl → forensically
    #       invisible; analyst following manifest wouldn't find them.
    #   (2) used paste+awk with a BROKEN column model that treated the
    #       IRQ name column (e.g. "IR-IO-APIC") as a 0 counter and
    #       ignored real per-CPU counts in middle columns.
    #
    # Fix: proper per-IRQ delta computed via python (reliable CSV-ish
    # parser), and explicit manifest_append for all three files.
    local _b_interrupts_pid=""
    if [[ -r /proc/interrupts ]]; then
        cat /proc/interrupts > "$dir/interrupts_t0.txt" 2>/dev/null || true
        header_write "$dir/interrupts_t0.txt" "B_interrupts_t0" "cat /proc/interrupts (t=0)" 0 0
        manifest_append "B_interrupts_t0" "B_kernel/interrupts_t0.txt" \
            "Snapshot de /proc/interrupts no início da camada B." "B1"
        (
            sleep 30
            cat /proc/interrupts > "$dir/interrupts_t30.txt" 2>/dev/null || true
            header_write "$dir/interrupts_t30.txt" "B_interrupts_t30" "cat /proc/interrupts (t=30)" 0 30000
            # AUDIT v3 — python-based delta, reliable.
            if command -v python3 >/dev/null 2>&1; then
                python3 - "$dir/interrupts_t0.txt" "$dir/interrupts_t30.txt" \
                    > "$dir/interrupts_delta.txt" 2>"$dir/interrupts_delta.err" <<'PYEOF'
import sys, re

def parse(path):
    """Parse /proc/interrupts. Returns {irq_name: (per_cpu_sum, name_label)}.

    Layout (example):
        CPU0  CPU1  CPU2  ...
      0:   47    0    0  IR-IO-APIC   0-edge    timer
    """
    rows = {}
    header_cpus = 0
    with open(path, errors="replace") as f:
        first = f.readline()
        header_cpus = len(re.findall(r"CPU\d+", first))
        for line in f:
            parts = line.rstrip("\n").split()
            if not parts:
                continue
            irq = parts[0].rstrip(":")
            # Next `header_cpus` tokens are per-CPU integer counts.
            try:
                counts = [int(x) for x in parts[1 : 1 + header_cpus]]
            except (ValueError, IndexError):
                continue
            if len(counts) != header_cpus:
                continue
            name = " ".join(parts[1 + header_cpus:])
            rows[irq] = (sum(counts), name)
    return rows

t0 = parse(sys.argv[1])
t30 = parse(sys.argv[2])

print("# delta = sum(t30) - sum(t0) across all CPUs, per IRQ.")
print("# Only HDA/SND-labelled IRQs shown.")
print("# format: IRQ  DELTA  NAME")
for irq, (count30, name) in t30.items():
    count0, _ = t0.get(irq, (0, name))
    delta = count30 - count0
    if re.search(r"hda|snd", name, re.I) or delta > 0 and re.search(r"audio|pulse|pipewire", name, re.I):
        print(f"{irq}  {delta}  {name}")
PYEOF
            else
                echo "python3 unavailable — delta not computed" > "$dir/interrupts_delta.txt"
            fi
            header_write "$dir/interrupts_delta.txt" "B_interrupts_delta" "interrupt delta over 30s" 0 30000
            manifest_append "B_interrupts_t30" "B_kernel/interrupts_t30.txt" \
                "Snapshot de /proc/interrupts 30s após t0 (background task)." "B1"
            manifest_append "B_interrupts_delta" "B_kernel/interrupts_delta.txt" \
                "Delta de interrupções HDA/SND em 30s (python-parsed; detecta storms ou starvation)." "B1"
        ) &
        _b_interrupts_pid=$!
    fi

    # Kconfig — só leitura, não bloqueia mesmo sem sudo.
    local kconfig="/boot/config-$(uname -r 2>/dev/null)"
    if [[ -r "$kconfig" ]]; then
        run_step "B_kconfig_sound" "$dir/kconfig_sound.txt" 10 \
            bash -c "grep -E '^(CONFIG_PREEMPT|CONFIG_HZ|CONFIG_SND_|CONFIG_HIGH_RES_TIMERS|CONFIG_RT)' '$kconfig' 2>/dev/null"
    else
        echo "kconfig not readable: $kconfig" > "$dir/kconfig_sound.txt"
        header_write "$dir/kconfig_sound.txt" "B_kconfig_sound" "kconfig (unreadable)" 1 0
    fi

    # Sched params.
    {
        echo "--- /proc/sys/kernel/sched_* ---"
        for f in /proc/sys/kernel/sched_latency_ns /proc/sys/kernel/sched_wakeup_granularity_ns /proc/sys/kernel/sched_min_granularity_ns; do
            [[ -r "$f" ]] && printf "  %s=%s\n" "$f" "$(cat "$f" 2>/dev/null)"
        done
    } > "$dir/sched_params.txt" 2>&1
    header_write "$dir/sched_params.txt" "B_sched_params" "sched params" 0 0

    # AUDIT v3 — Ftrace trace_pipe captura só funciona se um tracer
    # estiver ATIVO (default é `nop` → stream vazio). A versão anterior
    # lia de trace_pipe sem habilitar tracer e retornava empty file com
    # rc=0 — falso negativo completo sobre "HDA stalls".
    #
    # Fix: habilita events/snd/enable temporariamente, captura 2s, e
    # restaura o estado original. Gated em --with-sudo e no --enable-ftrace
    # (opt-in adicional) para não mexer em debugfs de forma inesperada.
    if [[ "$SOVYX_DIAG_FLAG_WITH_SUDO" = "1" ]] \
        && [[ "${SOVYX_DIAG_FLAG_ENABLE_FTRACE:-0}" = "1" ]] \
        && sudo -n true 2>/dev/null \
        && [[ -r /sys/kernel/debug/tracing/current_tracer ]]; then
        run_step "B_ftrace_trace_pipe" "$dir/ftrace_trace_pipe.txt" 15 \
            bash -c '
                TRACING=/sys/kernel/debug/tracing
                # Save original state and restore on exit — never leave
                # ftrace in a surprising configuration.
                orig_tracer=$(sudo cat "$TRACING/current_tracer" 2>/dev/null || echo "nop")
                orig_snd=$(sudo cat "$TRACING/events/snd/enable" 2>/dev/null || echo "0")
                trap "sudo tee \"$TRACING/events/snd/enable\" </dev/null <<<\"$orig_snd\" >/dev/null 2>&1; sudo tee \"$TRACING/current_tracer\" </dev/null <<<\"$orig_tracer\" >/dev/null 2>&1" EXIT

                # Enable snd events + nop tracer (event tracing doesnt
                # need a function tracer).
                echo 1 | sudo tee "$TRACING/events/snd/enable" >/dev/null 2>&1 || true
                # Drain any stale events, then capture 2s.
                sudo sh -c "cat /sys/kernel/debug/tracing/trace_pipe >/dev/null 2>&1" &
                drain_pid=$!
                sleep 0.5
                kill $drain_pid 2>/dev/null || true

                sudo timeout --preserve-status --kill-after=1 2 \
                    cat "$TRACING/trace_pipe" 2>&1 | head -5000
            '
    else
        echo "trace_pipe skipped — requires --with-sudo AND --enable-ftrace; previous versions emitted empty file silently due to default nop tracer" > "$dir/ftrace_trace_pipe.txt"
        header_write "$dir/ftrace_trace_pipe.txt" "B_ftrace_trace_pipe" "trace_pipe (skipped)" 126 0
    fi

    # Histórico de updates (últimos 7 dias).
    if [[ -r /var/log/apt/history.log ]]; then
        run_step "B_apt_history" "$dir/apt_history_recent.txt" 10 \
            bash -c 'tail -n 300 /var/log/apt/history.log 2>/dev/null || echo "no apt history"'
    fi
    if [[ -r /var/log/dpkg.log ]]; then
        run_step "B_dpkg_log" "$dir/dpkg_log_recent.txt" 10 \
            bash -c 'tail -n 500 /var/log/dpkg.log 2>/dev/null || echo "no dpkg log"'
    fi
    run_step "B_audio_pkg_versions" "$dir/audio_pkg_versions.txt" 15 \
        bash -c 'dpkg -l 2>/dev/null | grep -iE "linux-image|pipewire|wireplumber|alsa|libasound|portaudio|sounddevice" || echo "dpkg unavailable"'

    manifest_append "B_layer" "B_kernel/" \
        "Camada B — kernel, módulos, power, sched, histórico de atualizações." \
        "B1-B7"

    # Aguarda APENAS o subshell de interrupts_t30 — NUNCA usar `wait` sem arg
    # aqui: ele esperaria também os followers (journalctl -f / dmesg -w) que
    # rodam em background e nunca terminam, travando o script indefinidamente.
    if [[ -n "$_b_interrupts_pid" ]]; then
        wait "$_b_interrupts_pid" 2>/dev/null || true
    fi
}
