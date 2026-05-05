#!/usr/bin/env bash
# lib/G_sovyx.sh — Camada G: runtime do Sovyx.
#
# Reconcilia a config efetiva do Sovyx com o log do incidente.
# Roda em S_ACTIVE (voice on) — APIs que dependem de pipeline vivo
# funcionam aqui.
#
# Hipóteses (§2 do plano):
#   G1 — SOVYX_TUNING__VOICE__* em env ou config.yaml sobrescrevendo.
#   G2 — Estado persistente inconsistente (combo_store healthy + pipeline morto).
#   G3 — 0.21.1 vs 0.21.2 — VLX-001..008.
#   G4 — Divergência cascade winner ↔ opener runtime (race).
#   G5 — Moonshine English-only (ortogonal ao mic morto).
#   G6 — config.yaml mtime recente (mudança silenciosa).
#   G7 — API keys ausentes/expiradas.

_api_get_to_file() {
    # Uso: _api_get_to_file <path> <out_file> [<timeout_s>]
    local api_path="$1" out="$2" timeout_s="${3:-15}"
    mkdir -p "$(dirname "$out")"
    if [[ -z "$SOVYX_DIAG_TOKEN" ]]; then
        echo "{\"error\": \"no_token\"}" > "$out"
        return 1
    fi
    local step_id
    step_id="G_api_$(echo "$api_path" | tr '/ {}' '___' | tr -s '_' | sed 's/^_//;s/_$//')"
    run_step_pipe "$step_id" "$out" "$timeout_s" \
        curl -sS --max-time "$timeout_s" \
             -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
             "http://127.0.0.1:7777$api_path"
    local rc=$?

    # V4 Track H: if the output is empty or doesn't look like JSON,
    # overwrite with a structured error JSON. Without this, the forensic
    # artifact is an empty file — cross_correlation index + automated
    # consumers can't distinguish "API returned empty" from "curl failed".
    # The original stderr is preserved in <out>.stderr (run_step_pipe).
    if [[ ! -s "$out" ]]; then
        printf '{"error":"api_empty_response","curl_rc":%d,"path":"%s","stderr_sidecar":"%s.stderr"}\n' \
            "$rc" "$api_path" "$(basename "$out")" > "$out"
    else
        local first_char
        first_char=$(head -c 1 "$out" 2>/dev/null)
        if [[ "$first_char" != "{" && "$first_char" != "[" ]]; then
            # Response wasn't JSON — preserve original content as sidecar
            # and emit error JSON.
            mv -f "$out" "${out}.non_json_body"
            printf '{"error":"api_non_json_response","curl_rc":%d,"path":"%s","body_sidecar":"%s.non_json_body"}\n' \
                "$rc" "$api_path" "$(basename "$out")" > "$out"
        fi
    fi
    return "$rc"
}

run_layer_G() {
    local dir="$SOVYX_DIAG_OUTDIR/G_sovyx"
    mkdir -p "$dir"
    log_info "=== Layer G: Sovyx runtime ==="

    # ── Versão Sovyx, pipx, installed location ───────────────────────────
    run_step "G_sovyx_version" "$dir/version.txt" 15 \
        bash -c 'sovyx --version 2>&1; echo ""; which sovyx; echo ""; readlink -f "$(which sovyx)"'
    if tool_has pipx >/dev/null; then
        run_step "G_pipx_list" "$dir/pipx_list.txt" 15 \
            bash -c 'pipx list --include-injected 2>&1 || echo "pipx list failed"'
        run_step "G_pipx_runpip" "$dir/pipx_runpip_show.txt" 15 \
            bash -c 'pipx runpip sovyx show sovyx 2>&1 || echo "pipx runpip failed"'
    fi

    # ── sovyx doctor (todas as variantes disponíveis) ────────────────────
    run_step "G_doctor_general" "$dir/doctor.txt" "$SOVYX_DIAG_DOCTOR_TIMEOUT" \
        bash -c 'sovyx doctor --json 2>&1 || sovyx doctor 2>&1'
    run_step "G_doctor_voice" "$dir/doctor_voice.txt" "$SOVYX_DIAG_DOCTOR_TIMEOUT" \
        bash -c 'sovyx doctor voice --json 2>&1 || sovyx doctor voice 2>&1'
    run_step "G_doctor_cascade" "$dir/doctor_cascade.txt" "$SOVYX_DIAG_DOCTOR_TIMEOUT" \
        bash -c 'sovyx doctor cascade --json 2>&1 || sovyx doctor cascade 2>&1'
    # linux_session_manager_grab existe em 0.21.1+ (ADR R_X3).
    run_step "G_doctor_linux_sm_grab" "$dir/doctor_linux_session_manager_grab.txt" "$SOVYX_DIAG_DOCTOR_TIMEOUT" \
        bash -c 'sovyx doctor linux_session_manager_grab --json 2>&1 || sovyx doctor linux_session_manager_grab 2>&1'
    # voice_capture_apo NÃO existe em 0.21.1 (ADR R_X3). Tenta e registra.
    run_step "G_doctor_voice_capture_apo" "$dir/doctor_voice_capture_apo.txt" 10 \
        bash -c 'sovyx doctor voice_capture_apo 2>&1 || echo "subcommand not found in this version (expected in 0.21.1)"'

    # ── APIs Sovyx (precisam do token) ───────────────────────────────────
    _api_get_to_file "/api/health"                    "$dir/api_health.json"                     || true
    _api_get_to_file "/api/status"                    "$dir/api_status.json"                     || true
    _api_get_to_file "/api/voice/status"              "$dir/api_voice_status.json"               || true
    _api_get_to_file "/api/voice/capture-diagnostics" "$dir/api_voice_capture_diagnostics.json"  || true
    _api_get_to_file "/api/voice/hardware-detect"     "$dir/api_voice_hardware_detect.json"      || true
    _api_get_to_file "/api/voice/linux-mixer-diagnostics" "$dir/api_voice_linux_mixer_diagnostics.json" || true
    _api_get_to_file "/api/voice/health"              "$dir/api_voice_health.json"               || true
    _api_get_to_file "/api/voice/health/quarantine"   "$dir/api_voice_health_quarantine.json"    || true
    _api_get_to_file "/api/voice/models/status"       "$dir/api_voice_models_status.json"        || true
    _api_get_to_file "/api/voice/test/devices"        "$dir/api_voice_test_devices.json"         || true

    manifest_append "G_apis" "G_sovyx/api_*.json" \
        "Dumps das APIs do dashboard — status, capture-diagnostics, hardware-detect, mixer, health, quarantine, models." \
        "G2/G3/G4"

    # ── Logs + audit (tail) ──────────────────────────────────────────────
    local sovyx_log="$HOME/.sovyx/logs/sovyx.log"
    if [[ -r "$sovyx_log" ]]; then
        run_step "G_log_tail" "$dir/sovyx_log_tail.txt" 15 \
            bash -c "tail -n 5000 '$sovyx_log' 2>&1"
    else
        echo "sovyx log not found at $sovyx_log" > "$dir/sovyx_log_tail.txt"
        header_write "$dir/sovyx_log_tail.txt" "G_log_tail" "tail (no log)" 1 0
    fi

    local audit_log="$HOME/.sovyx/audit/audit.jsonl"
    if [[ -r "$audit_log" ]]; then
        run_step "G_audit_tail" "$dir/audit_tail.jsonl" 15 \
            bash -c "tail -n 1000 '$audit_log' 2>&1"
    fi

    # ── Árvore do data_dir com mtime (detecta mudança silenciosa) ────────
    run_step "G_data_dir_tree" "$dir/data_dir_tree_with_mtime.txt" 20 \
        bash -c 'find ~/.sovyx -maxdepth 3 -type f -printf "%TY-%Tm-%Td %TH:%TM %p %s\n" 2>/dev/null | sort || echo "no ~/.sovyx"'

    # ── Modelos ──────────────────────────────────────────────────────────
    run_step "G_models_listing" "$dir/models_listing.txt" 10 \
        bash -c 'ls -la ~/.sovyx/models/voice/ 2>/dev/null; echo ""; ls -la ~/.sovyx/models/voice/kokoro/ 2>/dev/null'
    run_step "G_models_sha256" "$dir/models_sha256.txt" 30 \
        bash -c 'find ~/.sovyx/models -type f -name "*.onnx" -o -name "*.bin" 2>/dev/null | xargs -r sha256sum 2>/dev/null || echo "no models"'

    # ── Combo store + overrides ──────────────────────────────────────────
    run_step "G_combo_store" "$dir/combo_store_dump.txt" 10 \
        bash -c '
            if [[ -r ~/.sovyx/voice/combo_store.json ]]; then
                cat ~/.sovyx/voice/combo_store.json
            elif [[ -r ~/.sovyx/voice/combo_store.db ]]; then
                sqlite3 ~/.sovyx/voice/combo_store.db .dump 2>/dev/null || echo "sqlite3 unavailable"
            else
                echo "no combo_store in ~/.sovyx/voice/"
            fi
        '
    run_step "G_capture_overrides" "$dir/capture_overrides.txt" 5 \
        bash -c 'cat ~/.sovyx/voice/capture_overrides.json 2>/dev/null || echo "no capture_overrides"'

    # ── Config efetiva (redigida) ────────────────────────────────────────
    local config_path="$HOME/.sovyx/system.yaml"
    if [[ -r "$config_path" ]]; then
        run_step "G_config" "$dir/config_effective_redacted.yaml" 10 \
            bash -c "cat '$config_path' | sed -E 's/((api[_-]?key|token|secret|password|passwd|bearer)[^:]*:)[^\\n]*$/\\1 <redacted>/gi'"
    else
        echo "no system.yaml at $config_path" > "$dir/config_effective_redacted.yaml"
        header_write "$dir/config_effective_redacted.yaml" "G_config" "cat (no file)" 1 0
    fi

    # Mind config (por mind id).
    run_step "G_mind_configs" "$dir/mind_configs_listing.txt" 10 \
        bash -c 'find ~/.sovyx/minds -maxdepth 2 -name "*.yaml" 2>/dev/null -printf "%TY-%Tm-%Td %TH:%TM %p\n" || echo "no minds dir"'

    # ── Env SOVYX_* (redigido) ───────────────────────────────────────────
    env | grep '^SOVYX_' | redact_stream > "$dir/env_SOVYX_redacted.txt"
    header_write "$dir/env_SOVYX_redacted.txt" "G_env_sovyx" "env SOVYX_* | redact" 0 0

    # ── V4.3: pip list + ldd + LD_LIBRARY_PATH (version drift hunting) ──
    # Bug em scipy 1.11→1.12 resample, numpy 1.x→2.x ABI break,
    # sounddevice + libportaudio version mismatch, onnxruntime versão
    # incompatível com Silero VAD model — todos invisíveis sem dump.
    if [[ -n "$SOVYX_DIAG_PYTHON" ]]; then
        run_step "G_pip_list_full" "$dir/pip_list_full.json" 30 \
            "$SOVYX_DIAG_PYTHON" -m pip list --format=json
        run_step "G_pip_check" "$dir/pip_check.txt" 20 \
            "$SOVYX_DIAG_PYTHON" -m pip check
    fi
    # ldd do binário sovyx — mostra versões REAIS de .so carregadas
    # (libportaudio/libasound do sistema vs venv-bundled).
    local sovyx_bin
    sovyx_bin=$(command -v sovyx 2>/dev/null || true)
    if [[ -n "$sovyx_bin" ]]; then
        local sovyx_resolved
        sovyx_resolved=$(readlink -f "$sovyx_bin" 2>/dev/null || echo "$sovyx_bin")
        {
            echo "=== which sovyx: $sovyx_bin ==="
            echo "=== readlink -f: $sovyx_resolved ==="
            echo ""
            echo "=== file $sovyx_resolved ==="
            file "$sovyx_resolved" 2>&1
            echo ""
            echo "=== ldd $sovyx_resolved ==="
            ldd "$sovyx_resolved" 2>&1 || echo "(ldd failed — script entry, not ELF)"
            echo ""
            echo "=== LD_LIBRARY_PATH ==="
            echo "${LD_LIBRARY_PATH:-(unset)}"
            echo ""
            echo "=== LD_PRELOAD ==="
            echo "${LD_PRELOAD:-(unset)}"
        } > "$dir/sovyx_binary_libs.txt" 2>&1
        header_write "$dir/sovyx_binary_libs.txt" "G_sovyx_binary_libs" \
            "ldd sovyx + LD_*" 0 0
    fi
    # ldd das .so do venv que importam libportaudio/libasound.
    if [[ -n "$SOVYX_DIAG_PYTHON" ]]; then
        local venv_base
        venv_base=$(dirname "$(dirname "$SOVYX_DIAG_PYTHON")")
        {
            echo "=== sounddevice _lib._name + ldd ==="
            "$SOVYX_DIAG_PYTHON" -c '
import sounddevice as sd
print("sounddevice:", sd.__version__, sd.__file__)
print("portaudio binary:", sd._lib._name)
' 2>&1
            echo ""
            local pa_so
            pa_so=$("$SOVYX_DIAG_PYTHON" -c \
                'import sounddevice as sd; print(sd._lib._name)' 2>/dev/null)
            if [[ -n "$pa_so" && -f "$pa_so" ]]; then
                echo "=== ldd $pa_so ==="
                ldd "$pa_so" 2>&1
            fi
        } > "$dir/portaudio_runtime_libs.txt" 2>&1
        header_write "$dir/portaudio_runtime_libs.txt" "G_portaudio_runtime_libs" \
            "ldd portaudio runtime" 0 0
    fi
    manifest_append "G_version_drift" \
        "G_sovyx/pip_list_full.json G_sovyx/pip_check.txt G_sovyx/sovyx_binary_libs.txt G_sovyx/portaudio_runtime_libs.txt" \
        "Captura completa de versões Python deps + bindings nativos. Detecta version drift (numpy/scipy/sounddevice/onnxruntime), conflitos venv vs sistema, LD_LIBRARY_PATH/LD_PRELOAD pollution." \
        "G3 (versão Sovyx) + E_P3 (libportaudio bug) + E_P5 (wheel patch destoante)"

    # ── API keys presence (sem valor) ────────────────────────────────────
    python3 - > "$dir/api_keys_presence.json" <<'PYEOF'
import json, os
keys = {}
for k, v in os.environ.items():
    kl = k.lower()
    if any(p in kl for p in ("api_key", "apikey", "token", "secret", "password", "bearer")):
        if k.startswith("SOVYX_"):
            keys[k] = {"present": True, "length": len(v)}
print(json.dumps({"keys": keys}, indent=2))
PYEOF

    # ── strace -c 5s (opt-in; ptrace-gated) ──────────────────────────────
    if [[ "$SOVYX_DIAG_FLAG_TRACE_SYSCALLS" = "1" ]] && tool_has strace >/dev/null; then
        local sovyx_pid scope
        sovyx_pid=$(_sovyx_daemon_pids | head -1)
        scope=1
        [[ -r /proc/sys/kernel/yama/ptrace_scope ]] && \
            scope=$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 1)

        if [[ -z "$sovyx_pid" ]]; then
            echo "no sovyx pid for strace" > "$dir/strace_summary.txt"
            header_write "$dir/strace_summary.txt" "G_strace" "strace (no pid)" 1 0
        elif [[ "$scope" -ge 2 ]] && [[ "$SOVYX_DIAG_FLAG_WITH_SUDO" != "1" ]]; then
            echo "ptrace_scope=$scope without --with-sudo" > "$dir/strace_summary.txt"
            header_write "$dir/strace_summary.txt" "G_strace" "strace (ptrace blocked)" 126 0
        else
            local strace_cmd=(strace -f -c -p "$sovyx_pid")
            if [[ "$scope" -ge 2 ]]; then strace_cmd=(sudo "${strace_cmd[@]}"); fi
            run_step "G_strace" "$dir/strace_summary.txt" 8 \
                bash -c "${strace_cmd[*]} 2>&1 & pid=\$!; sleep 5; kill -INT \$pid 2>/dev/null; wait \$pid 2>/dev/null; true"
        fi
    else
        echo "strace disabled via --no-trace-syscalls or unavailable" > "$dir/strace_summary.txt"
        header_write "$dir/strace_summary.txt" "G_strace" "strace (disabled)" 0 0
    fi

    manifest_append "G_layer" "G_sovyx/" \
        "Camada G — runtime Sovyx: version, doctor (todas variantes), APIs, logs, audit, models, combo_store, config, env, API keys presence, strace." \
        "G1-G7"
}
