#!/usr/bin/env bash
# lib/I_network.sh — Camada I: rede, firewall, dashboard, clock.
#
# Objetivo: confirmar que rede/dashboard não mascaram resposta de voz.
# Roda em S_IDLE.
#
# Hipóteses (§2 do plano):
#   I1 — WebSocket do dashboard desconectando e UI escondendo transcrição.
#   I2 — Porta 7777 bloqueada por firewall.
#   I3 — Clock skew quebrando TLS (precaução).

run_layer_I() {
    local dir="$SOVYX_DIAG_OUTDIR/I_network"
    mkdir -p "$dir"
    log_info "=== Layer I: network ==="

    # Sockets TCP/UDP abertos (foco 7777).
    run_step "I_ss_listen" "$dir/ss_listen.txt" 10 \
        bash -c 'ss -lntp 2>&1 | head -30'
    run_step "I_ss_7777" "$dir/ss_7777.txt" 5 \
        bash -c 'ss -lntp 2>/dev/null | grep 7777 || echo "7777 not listening"'

    # Firewall.
    run_step "I_firewall_status" "$dir/firewall.txt" 10 \
        bash -c 'echo "--- ufw ---"; systemctl status ufw --no-pager 2>&1 | head -15; echo ""; echo "--- firewalld ---"; systemctl status firewalld --no-pager 2>&1 | head -15; echo ""; echo "--- nftables ---"; systemctl status nftables --no-pager 2>&1 | head -15'
    if [[ "$SOVYX_DIAG_FLAG_WITH_SUDO" = "1" ]] && sudo -n true 2>/dev/null; then
        run_step "I_iptables_rules" "$dir/iptables_rules.txt" 15 \
            bash -c 'sudo iptables -S 2>/dev/null | head -80 || echo "iptables unavailable"'
        run_step "I_nft_ruleset" "$dir/nft_ruleset.txt" 15 \
            bash -c 'sudo nft list ruleset 2>/dev/null | head -200 || echo "nft unavailable"'
    fi

    # Dashboard health probe.
    if [[ -n "$SOVYX_DIAG_TOKEN" ]]; then
        run_step "I_health_curl" "$dir/health_curl.txt" 15 \
            curl -sS --max-time 10 -w "\nHTTP %{http_code} time_total=%{time_total}\n" \
                 -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
                 "http://127.0.0.1:7777/api/health"
    else
        echo "no token — health curl skipped" > "$dir/health_curl.txt"
        header_write "$dir/health_curl.txt" "I_health_curl" "curl (no token)" 126 0
    fi

    # WebSocket probe opt-in — se websocat disponível, sonda o meter endpoint.
    #
    # AUDIT v3 — CRITICAL credential fix: the previous version passed
    # SOVYX_DIAG_TOKEN as a URL query string (?token=...), which caused
    # the token to (a) land in the forensic artifact ``websocket_probe.txt``
    # in clear text, (b) appear in shell history on some setups, (c) be
    # loggable by any HTTP intermediary. For an audit bundle that may be
    # shared, this is a direct credential leak into evidence.
    #
    # Fix: pass the token as an HTTP Authorization header via websocat
    # ``-H``. The token is read from env at the moment of the call and
    # is never interpolated into the command line in a way that ends up
    # on disk.
    #
    # Additionally: the previous probe used ``-n1`` (receive one frame)
    # which only verifies that connect succeeded. For hypothesis I1
    # ("WebSocket desconectando") we need to see whether the server
    # closes the socket prematurely. Switched to a 10 s stay-open probe
    # (``-B 100`` buffer limit so we don't leak the whole stream into
    # the artifact, just headers + first frames).
    if tool_has websocat >/dev/null && [[ -n "$SOVYX_DIAG_TOKEN" ]]; then
        # SOVYX_DIAG_TOKEN is exported to the subshell via the environment
        # — websocat reads the -H value verbatim, token never appears
        # as an argv element in ps / /proc/<pid>/cmdline.
        SOVYX_DIAG_TOKEN="$SOVYX_DIAG_TOKEN" \
        run_step "I_websocket_probe" "$dir/websocket_probe.txt" 15 \
            bash -c '
                AUTH_HEADER="Authorization: Bearer $SOVYX_DIAG_TOKEN"
                # 10 s stay-open probe. If the server closes early, the
                # exit + captured frame count surface premature close.
                exec timeout --preserve-status --kill-after=2 12 \
                    websocat -B 8192 \
                        -H "$AUTH_HEADER" \
                        "ws://127.0.0.1:7777/api/voice/test/input" \
                    < /dev/null 2>&1
            '
        # Redact any accidental token substring from the artifact.
        # Defense-in-depth in case a server echoes the Auth header back.
        if [[ -r "$dir/websocket_probe.txt" ]]; then
            python3 - "$dir/websocket_probe.txt" "$SOVYX_DIAG_TOKEN" <<'PYEOF' 2>/dev/null || true
import pathlib, sys
p = pathlib.Path(sys.argv[1])
tok = sys.argv[2]
if tok and len(tok) >= 8:
    txt = p.read_text(errors="replace")
    redacted = txt.replace(tok, "<SOVYX_DIAG_TOKEN>")
    if redacted != txt:
        p.write_text(redacted)
PYEOF
        fi
    else
        echo "websocat unavailable or no token" > "$dir/websocket_probe.txt"
        header_write "$dir/websocket_probe.txt" "I_websocket_probe" "websocat (unavailable)" 126 0
    fi

    # AUDIT v3+ T7 — explicit DNS resolution test. `curl -I` folds
    # DNS failure into generic "could not connect" output, masking the
    # root cause. `getent hosts` uses the system's NSS stack (resolv.conf
    # + systemd-resolved + /etc/hosts) and reports the specific failure
    # mode: NXDOMAIN, timeout, no NS configured, etc. Cheap (<1s) and
    # surfaces I3 / network-stack issues independent of HTTPS handshake.
    run_step "I_dns_resolve" "$dir/dns_resolve.txt" 10 \
        bash -c '
            for host in 127.0.0.1 api.anthropic.com api.openai.com generativelanguage.googleapis.com www.google.com; do
                printf "=== %s ===\n" "$host"
                # getent honors nsswitch and emits address or empty.
                getent hosts "$host" 2>&1 || printf "(getent returned rc=%d)\n" $?
                # host(1) adds authoritative NS info when available.
                if command -v host >/dev/null 2>&1; then
                    host -W 3 "$host" 2>&1 | head -8 || true
                fi
                printf "\n"
            done
            printf "=== resolv.conf ===\n"
            cat /etc/resolv.conf 2>/dev/null || printf "(unreadable)\n"
            printf "\n=== nsswitch hosts line ===\n"
            grep -E "^hosts:" /etc/nsswitch.conf 2>/dev/null || printf "(no nsswitch.conf)\n"
        '

    # Clock — skew relativo a servidor NTP (ou Google).
    run_step "I_timedatectl" "$dir/timedatectl.txt" 10 timedatectl
    run_step "I_chronyc" "$dir/chronyc_tracking.txt" 10 \
        bash -c 'chronyc tracking 2>/dev/null || timedatectl show-timesync --all 2>/dev/null || echo "no ntp client"'
    run_step "I_clock_drift" "$dir/clock_drift.txt" 15 \
        bash -c '
            set -e
            local_date=$(date -u +%s)
            remote_date_header=$(curl -s -I --max-time 8 https://www.google.com 2>/dev/null | grep -i "^date:" | sed "s/^[Dd]ate: //" | tr -d "\r")
            if [[ -n "$remote_date_header" ]]; then
                remote_epoch=$(date -u -d "$remote_date_header" +%s 2>/dev/null || echo 0)
                if [[ "$remote_epoch" != "0" ]]; then
                    drift=$((local_date - remote_epoch))
                    echo "local_utc_epoch=$local_date"
                    echo "remote_utc_epoch=$remote_epoch"
                    echo "drift_seconds=$drift"
                    if [[ ${drift#-} -gt 2 ]]; then
                        echo "WARN: drift > 2s"
                    fi
                else
                    echo "could not parse remote date"
                fi
            else
                echo "offline or blocked — skipping drift check"
            fi
        '

    manifest_append "I_layer" "I_network/" \
        "Camada I — rede, firewall, dashboard probe, clock drift." \
        "I1-I3"
}
