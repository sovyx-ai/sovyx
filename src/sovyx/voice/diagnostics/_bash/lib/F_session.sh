#!/usr/bin/env bash
# lib/F_session.sh — Camada F: sessão, permissões, IPC, D-Bus, namespaces.
#
# Objetivo: descartar problemas de autorização e de ambiente de sessão.
# Roda em S_IDLE (precisa do PID do Sovyx para /proc/PID/*).
#
# Hipóteses (§2 do plano):
#   F1 — Usuário fora do grupo audio.
#   F2 — /dev/snd com permissões errôneas ou ACL ausente.
#   F3 — Sessão systemd --user degradada (bus faltando, XDG_RUNTIME_DIR mal).
#   F4 — AppArmor/SELinux bloqueando Sovyx ou Python.
#   F5 — Shell diferente com env diferente.
#   F6 — Duplicado: mais de uma instância do daemon.
#   F7 — Sovyx em cgroup restritivo.
#   F8 — Polkit rule bloqueando RealtimeKit1.
#   F9 — XDG portal interferindo.

run_layer_F() {
    local dir="$SOVYX_DIAG_OUTDIR/F_session"
    mkdir -p "$dir"
    log_info "=== Layer F: session ==="

    # Identidade + grupos.
    run_step "F_id_groups" "$dir/id_groups.txt" 5 \
        bash -c 'echo "--- id ---"; id; echo ""; echo "--- groups ---"; groups; echo ""; echo "--- audio/pipewire/rtkit ---"; for g in audio pipewire rtkit render; do getent group $g 2>/dev/null || echo "$g: absent"; done'
    manifest_append "F_id_groups" "F_session/id_groups.txt" \
        "User, grupos, membership em audio/pipewire/rtkit. Alimenta F1, F8." "F1/F8"

    # Permissões /dev/snd.
    run_step "F_dev_snd_perms" "$dir/dev_snd_perms.txt" 5 \
        bash -c 'ls -laR /dev/snd 2>/dev/null || echo "no /dev/snd"'
    if tool_has getfacl >/dev/null; then
        run_step "F_dev_snd_acl" "$dir/dev_snd_acl.txt" 10 \
            bash -c 'getfacl /dev/snd/* 2>/dev/null || echo "no ACLs"'
    fi
    if tool_has namei >/dev/null; then
        run_step "F_namei_pcm" "$dir/namei_pcm.txt" 5 \
            bash -c 'namei -mo /dev/snd/pcmC*D0c 2>/dev/null || echo "no pcm devices"'
    fi

    # Env redigido.
    env | redact_stream > "$dir/env_redacted.txt"
    header_write "$dir/env_redacted.txt" "F_env_redacted" "env | redact" 0 0

    # XDG + session type.
    run_step "F_xdg_session" "$dir/xdg_session.txt" 5 \
        bash -c 'echo "XDG_SESSION_TYPE=$XDG_SESSION_TYPE"; echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"; echo "XDG_CURRENT_DESKTOP=$XDG_CURRENT_DESKTOP"; echo "XDG_SESSION_DESKTOP=$XDG_SESSION_DESKTOP"; echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"'

    # loginctl session.
    if tool_has loginctl >/dev/null; then
        run_step "F_loginctl" "$dir/loginctl.txt" 10 \
            bash -c 'sid=$(loginctl | awk -v u="$USER" "\$3==u {print \$1; exit}"); [[ -n "$sid" ]] && loginctl show-session "$sid" --no-pager || echo "no session"'
    fi

    # systemd --user status.
    run_step "F_systemd_user_failed" "$dir/systemd_user_failed.txt" 10 \
        bash -c 'systemctl --user list-units --state=failed,running --no-pager 2>&1 || echo "systemctl unavailable"'
    run_step "F_systemd_user_critical" "$dir/systemd_user_critical_chain.txt" 20 \
        bash -c 'systemd-analyze --user critical-chain 2>&1 | head -80 || echo "systemd-analyze unavailable"'

    # AppArmor / SELinux.
    run_step "F_apparmor" "$dir/aa_status.txt" 10 \
        bash -c 'aa-status 2>&1 | head -100 || echo "AppArmor unavailable"'
    run_step "F_selinux" "$dir/selinux_status.txt" 10 \
        bash -c 'sestatus 2>&1 || echo "SELinux unavailable"'
    run_step "F_denied_logs" "$dir/denied_logs.txt" 20 \
        bash -c 'journalctl -k --since "24 hours ago" 2>/dev/null | grep -iE "denied|apparmor|selinux" | tail -100 || true'

    # Processos de áudio.
    run_step "F_ps_audio" "$dir/ps_audio.txt" 10 \
        bash -c 'ps -ef 2>/dev/null | grep -E "sovyx|pipewire|wireplumber|pulseaudio|rtkit" | grep -v grep || echo "no matches"'
    run_step "F_pgrep_sovyx" "$dir/pgrep_sovyx.txt" 5 \
        bash -c 'pgrep -af "python.*sovyx|/sovyx" 2>/dev/null || echo "no sovyx processes"'

    # Sockets Sovyx-related.
    run_step "F_ss_unix" "$dir/sockets.txt" 10 \
        bash -c 'ss -lnpx 2>/dev/null | grep -E "sovyx|pipewire|pulse" || echo "no matches"'

    # D-Bus.
    if tool_has busctl >/dev/null; then
        run_step "F_busctl_user"   "$dir/dbus_user_list.txt"   15 busctl --user list --no-pager
        run_step "F_busctl_system" "$dir/dbus_system_list.txt" 15 busctl list --no-pager
    fi
    if tool_has dbus-send >/dev/null; then
        run_step "F_dbus_listnames" "$dir/dbus_session_names.txt" 15 \
            dbus-send --session --print-reply \
                --dest=org.freedesktop.DBus /org/freedesktop/DBus \
                org.freedesktop.DBus.ListNames
    fi
    run_step "F_dbus_env" "$dir/dbus_env.txt" 5 \
        bash -c 'echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"; ls -la $XDG_RUNTIME_DIR/bus 2>/dev/null || echo "no bus socket"'

    # Process-level Sovyx detail.
    # Filtra via /proc/exe para não casar com o próprio script bash.
    local sovyx_pid
    sovyx_pid=$(_sovyx_daemon_pids | head -1)
    if [[ -n "$sovyx_pid" && -d "/proc/$sovyx_pid" ]]; then
        run_step "F_sovyx_status" "$dir/sovyx_proc_status.txt" 5 \
            bash -c "cat /proc/$sovyx_pid/status 2>/dev/null || true"
        run_step "F_sovyx_limits" "$dir/sovyx_proc_limits.txt" 5 \
            bash -c "cat /proc/$sovyx_pid/limits 2>/dev/null || true"
        run_step "F_sovyx_cgroup" "$dir/sovyx_proc_cgroup.txt" 5 \
            bash -c "cat /proc/$sovyx_pid/cgroup 2>/dev/null || true"
        run_step "F_sovyx_ns" "$dir/sovyx_proc_namespaces.txt" 5 \
            bash -c "ls -la /proc/$sovyx_pid/ns/ 2>/dev/null || true"
        run_step "F_sovyx_fd_count" "$dir/sovyx_proc_fd_count.txt" 5 \
            bash -c "ls /proc/$sovyx_pid/fd 2>/dev/null | wc -l || echo 0"
        run_step "F_sovyx_fd_list" "$dir/sovyx_proc_fd_list.txt" 10 \
            bash -c "ls -la /proc/$sovyx_pid/fd 2>/dev/null | head -80 || true"
        run_step "F_sovyx_maps_libs" "$dir/sovyx_proc_maps_libs.txt" 15 \
            bash -c "awk '{print \$6}' /proc/$sovyx_pid/maps 2>/dev/null | sort -u || true"
        run_step "F_sovyx_threads" "$dir/sovyx_threads.txt" 5 \
            bash -c "ps -L -p $sovyx_pid 2>/dev/null || true"
        manifest_append "F_sovyx_proc" "F_session/sovyx_proc_*" \
            "Detalhes de runtime do processo Sovyx (cgroup, namespaces, fds, libs, threads). Alimenta F7, F8." \
            "F7/F8"
    else
        log_warn "Sovyx PID not found in F layer — sovyx_proc_* skipped"
        echo "Sovyx PID not found during F layer — daemon may not be running." \
             > "$dir/sovyx_proc_UNAVAILABLE.txt"
    fi

    # Capabilities do binário sovyx.
    if tool_has getcap >/dev/null; then
        local sovyx_bin
        sovyx_bin=$(command -v sovyx 2>/dev/null || true)
        if [[ -n "$sovyx_bin" ]]; then
            run_step "F_sovyx_caps" "$dir/sovyx_capabilities.txt" 5 \
                bash -c "real=\"\$(readlink -f '$sovyx_bin')\"; echo \"bin=\$real\"; getcap \"\$real\" 2>&1 || echo no caps"
        fi
    fi

    # Ulimits.
    run_step "F_ulimit" "$dir/ulimit.txt" 5 bash -c 'ulimit -a'

    # Flatpak / Snap.
    run_step "F_flatpak" "$dir/flatpak_snap.txt" 10 \
        bash -c 'echo "--- flatpak ---"; flatpak list --columns=application,version 2>/dev/null || echo "no flatpak"; echo ""; echo "--- snap ---"; snap list 2>/dev/null || echo "no snap"; echo ""; echo "--- /run/flatpak ---"; ls /run/flatpak 2>/dev/null || echo "no /run/flatpak"'
    run_step "F_xdg_portal" "$dir/xdg_portal.txt" 10 \
        bash -c 'systemctl --user status xdg-desktop-portal --no-pager 2>&1 | head -40 || echo "no xdg portal"'

    manifest_append "F_layer" "F_session/" \
        "Camada F — sessão, permissões, D-Bus, namespaces, AppArmor/SELinux, Flatpak/Snap." \
        "F1-F9"
}
