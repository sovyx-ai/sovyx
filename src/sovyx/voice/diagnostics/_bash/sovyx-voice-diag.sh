#!/usr/bin/env bash
# sovyx-voice-diag — bateria completa de diagnóstico de voz/áudio Sovyx (Linux).
#
# Fase 2 do plano docs-internal/diagnostics/voice-linux-mint-2026-04-22-plan.md (v2).
# Ver README-DIAG.md para uso.
#
# Licença: interno.

set -uo pipefail

# AUDIT v3 — force deterministic locale so every external tool
# (arecord, pactl, pw-record, journalctl, amixer, ls, lsof, wc, sort,
# grep, awk) emits English messages that our parsers/regex can match.
# User's interactive locale is still available via ORIGINAL_LANG for
# display purposes if needed; the diagnostic artifacts are
# locale-independent.
export LC_ALL=C
export LANG=C
export LANGUAGE=C

# AUDIT v3 — nullglob so patterns that match zero files expand to
# empty, not to the literal glob string. Prevents ``for f in *.txt``
# loops from iterating with ``$f`` set to the literal ``*.txt``.
shopt -s nullglob

# ─────────────────────────────────────────────────────────────────────────
# Path resolution
# ─────────────────────────────────────────────────────────────────────────

_resolve_script_dir() {
    local src="${BASH_SOURCE[0]}"
    while [[ -L "$src" ]]; do
        local dir
        dir=$(cd -P "$(dirname "$src")" && pwd)
        src=$(readlink "$src")
        [[ "$src" != /* ]] && src="$dir/$src"
    done
    cd -P "$(dirname "$src")" && pwd
}
readonly SOVYX_DIAG_SCRIPT_DIR="$(_resolve_script_dir)"
readonly SOVYX_DIAG_LIB_DIR="$SOVYX_DIAG_SCRIPT_DIR/lib"

# ─────────────────────────────────────────────────────────────────────────
# Args
# ─────────────────────────────────────────────────────────────────────────

print_usage() {
    cat <<EOF
Uso: sovyx-voice-diag.sh [flags]

Flags:
  --outdir DIR               Diretório de saída (default: \$HOME/sovyx-diag-<ts>)
  --initial-evidence DIR     Fonte de bug_sovyx.txt + screenshots (default: ./initial_evidence)
  --yes                      Pula consent prompt (automação)
  --non-interactive          Pula prompts de fala (captura cegamente)
  --with-sudo                Tenta segundo passe com sudo para dmidecode/dmesg/nft
  --skip-captures            Pula W*/K* — só snapshots + configs
  --test-suspend             [INTRUSIVO] Adiciona S_POST_SUSPEND (systemctl suspend)
  --test-external-grab       Adiciona S_EXTERNAL_GRAB (prompt manual)
  --intrusive-restart-audio  [INTRUSIVO] Reinicia pipewire/wireplumber durante H
  --with-powertop            Roda powertop 3s em J
  --trace-syscalls           strace -c 5s em G (default on; auto-off se ptrace bloqueia)
  --no-trace-syscalls        Desabilita strace mesmo que possível
  --skip-operator-prompts    Pula prompts interativos ao operador (reduz cobertura perceptual)
  --skip-guardian            Pula Temporal Guardian (background monitors)
  --enable-ftrace            [INTRUSIVO] Habilita ftrace em G (requer sudo + debugfs)
  --only LIST                Roda só as camadas listadas (vírgula, ex: "A,C,D,E,J").
                             Default = todas. Calibração (sovyx --calibrate) usa
                             "A,C,D,E,J" para baixar de ~10min para ~30s. Phase
                             enter/exit + selftest sempre rodam.
  -h, --help                 Esta mensagem

Saída:
  <outdir>/sovyx-voice-diag_<hostname>_<ts>_<uuid>.tar.gz

Tempo esperado: 8-12 min (padrão) + 1-2 min por flag intrusiva.

Ver README-DIAG.md para detalhes e exemplos.
EOF
}

parse_args() {
    SOVYX_DIAG_OUTDIR_ARG=""
    SOVYX_DIAG_INITIAL_EVIDENCE_DIR=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --outdir)               SOVYX_DIAG_OUTDIR_ARG="$2"; shift 2 ;;
            --initial-evidence)     SOVYX_DIAG_INITIAL_EVIDENCE_DIR="$2"; shift 2 ;;
            --yes)                  SOVYX_DIAG_FLAG_YES=1; shift ;;
            --non-interactive)      SOVYX_DIAG_FLAG_NON_INTERACTIVE=1; shift ;;
            --with-sudo)            SOVYX_DIAG_FLAG_WITH_SUDO=1; shift ;;
            --skip-captures)        SOVYX_DIAG_FLAG_SKIP_CAPTURES=1; shift ;;
            --test-suspend)         SOVYX_DIAG_FLAG_TEST_SUSPEND=1; shift ;;
            --test-external-grab)   SOVYX_DIAG_FLAG_TEST_EXTERNAL_GRAB=1; shift ;;
            --intrusive-restart-audio) SOVYX_DIAG_FLAG_INTRUSIVE_RESTART_AUDIO=1; shift ;;
            --with-powertop)        SOVYX_DIAG_FLAG_WITH_POWERTOP=1; shift ;;
            --trace-syscalls)       SOVYX_DIAG_FLAG_TRACE_SYSCALLS=1; shift ;;
            --no-trace-syscalls)    SOVYX_DIAG_FLAG_TRACE_SYSCALLS=0; shift ;;
            --skip-operator-prompts) SOVYX_DIAG_FLAG_SKIP_OPERATOR_PROMPTS=1; shift ;;
            --skip-guardian)        SOVYX_DIAG_FLAG_SKIP_GUARDIAN=1; shift ;;
            --enable-ftrace)        SOVYX_DIAG_FLAG_ENABLE_FTRACE=1; shift ;;
            --only)                 SOVYX_DIAG_FLAG_ONLY="$2"; shift 2 ;;
            -h|--help)              print_usage; exit 0 ;;
            *)                      echo "Unknown flag: $1" >&2; print_usage; exit 2 ;;
        esac
    done
}

# ─────────────────────────────────────────────────────────────────────────
# Load common (precisa carregar antes de referenciar variáveis globais)
# ─────────────────────────────────────────────────────────────────────────

# shellcheck source=lib/common.sh
source "$SOVYX_DIAG_LIB_DIR/common.sh"

# Parse após o source — flags mexem em variáveis de common.sh.
parse_args "$@"

# rc.6 (Agent 2 C.3): valida o conjunto de letras de layer passado em
# --only. Pre-rc.6, `--only=Z` (letra desconhecida) era silenciosamente
# no-op: nenhum layer match → tarball vazio + exit 0 ("diag completed"
# sem fazer nada). Operadores que digitavam typo perdiam o run sem aviso.
# Agora rejeita com erro acionável citando as letras válidas.
if [[ -n "$SOVYX_DIAG_FLAG_ONLY" ]]; then
    _SOVYX_VALID_LAYER_LETTERS="A B C D E F G H I J K"
    _SOVYX_INVALID_LETTERS=""
    IFS=',' read -ra _SOVYX_REQUESTED_LAYERS <<< "$SOVYX_DIAG_FLAG_ONLY"
    for _letter in "${_SOVYX_REQUESTED_LAYERS[@]}"; do
        # Trim whitespace.
        _letter="${_letter// /}"
        if [[ -z "$_letter" ]]; then continue; fi
        # Check membership.
        if [[ ! " $_SOVYX_VALID_LAYER_LETTERS " =~ \ $_letter\  ]]; then
            _SOVYX_INVALID_LETTERS="$_SOVYX_INVALID_LETTERS $_letter"
        fi
    done
    if [[ -n "$_SOVYX_INVALID_LETTERS" ]]; then
        echo "sovyx-voice-diag: --only contains unknown layer letter(s):${_SOVYX_INVALID_LETTERS}" >&2
        echo "Valid layers: A,B,C,D,E,F,G,H,I,J,K (case-sensitive). Example: --only A,C,D,E,J" >&2
        exit 2
    fi
    unset _letter _SOVYX_VALID_LAYER_LETTERS _SOVYX_INVALID_LETTERS _SOVYX_REQUESTED_LAYERS
fi

# ─────────────────────────────────────────────────────────────────────────
# Plataforma
# ─────────────────────────────────────────────────────────────────────────

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "sovyx-voice-diag só roda em Linux. Detectado: $(uname -s)" >&2
    exit 2
fi

# ─────────────────────────────────────────────────────────────────────────
# Outdir
# ─────────────────────────────────────────────────────────────────────────

_generate_outdir_name() {
    local ts host uuid
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    host=$(hostname 2>/dev/null || echo unknown)
    uuid=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])' 2>/dev/null || echo 00000000)
    echo "$HOME/sovyx-diag-${host}-${ts}-${uuid}"
}

if [[ -z "$SOVYX_DIAG_OUTDIR_ARG" ]]; then
    SOVYX_DIAG_OUTDIR_ARG=$(_generate_outdir_name)
fi

_init_common "$SOVYX_DIAG_OUTDIR_ARG"
install_trap

log_info "sovyx-voice-diag v$SOVYX_DIAG_VERSION starting"
log_info "outdir: $SOVYX_DIAG_OUTDIR"

# ─────────────────────────────────────────────────────────────────────────
# Pre-flight — resolução de paths e checagem de ambiente
# ─────────────────────────────────────────────────────────────────────────

# Registrar ferramentas-chave no environment_matrix (resolvidas via tool_has).
# V4.1: added `sovyx` to probe — prior version left sovyx_cli_present=false
# even when pipx resolved the venv, confusing external auditors.
for t in sovyx arecord aplay amixer alsactl alsaucm pactl pw-dump pw-cli \
         pw-top pw-metadata wpctl pw-record pw-play parecord paplay sox \
         websocat curl jq lspci lsusb dmidecode bluetoothctl dmesg \
         journalctl systemctl lsof fuser ss timedatectl chronyc ldd dpkg \
         dkms flatpak snap tlp-stat sensors powertop alsa-info.sh strace \
         python3 git getcap getfacl namei udevadm inotifywait; do
    tool_has "$t" >/dev/null || true
done

resolve_sovyx_python || true
resolve_dashboard_token || true

# ─────────────────────────────────────────────────────────────────────────
# Consent + estado inicial
# ─────────────────────────────────────────────────────────────────────────

log_info "=== Consent + estado inicial ==="

echo "" >&2
cat >&2 <<'WARN'
Este script vai:
  1. PARAR o daemon Sovyx (se estiver rodando) por ~8-12 minutos.
  2. Iniciar/parar o Sovyx múltiplas vezes para coletar snapshots por estado.
  3. Gravar breves capturas (.wav) de fala do usuário em janelas controladas.
  4. Ler configs, logs, estado do sistema e do PipeWire/ALSA.
  5. Tudo é não-destrutivo: nenhum config é editado, nenhum pacote é (des)instalado.

Dados sensíveis (tokens/API keys) são redigidos antes de salvar.
O pacote final vai para ~/sovyx-diag-... — você decide o que enviar.
WARN

if ! prompt_yn "Continuar?"; then
    log_error "user declined; abort"
    exit 0
fi

# Registrar estado inicial ANTES de qualquer mudança.
if sovyx_is_running; then
    SOVYX_DIAG_INITIAL_SOVYX_RUNNING="yes"
    SOVYX_DIAG_INITIAL_VOICE_ENABLED=$(sovyx_voice_enabled_state)
else
    SOVYX_DIAG_INITIAL_SOVYX_RUNNING="no"
    SOVYX_DIAG_INITIAL_VOICE_ENABLED="no"
fi
log_info "initial state: sovyx_running=$SOVYX_DIAG_INITIAL_SOVYX_RUNNING voice_enabled=$SOVYX_DIAG_INITIAL_VOICE_ENABLED"

# Grava em SUMMARY.json (será finalizado em T11).
cat > "$SOVYX_DIAG_SUMMARY_JSON" <<EOF
{
  "schema_version": 1,
  "script_version": "$SOVYX_DIAG_VERSION",
  "hostname": "$(hostname 2>/dev/null || echo unknown)",
  "user": "$(id -un 2>/dev/null || echo unknown)",
  "started_utc_ns": "$SOVYX_DIAG_START_UTC_NS",
  "started_monotonic_ns": $SOVYX_DIAG_START_MONO_NS,
  "initial_sovyx_running": "$SOVYX_DIAG_INITIAL_SOVYX_RUNNING",
  "initial_voice_enabled": "$SOVYX_DIAG_INITIAL_VOICE_ENABLED",
  "flags": {
    "yes": $SOVYX_DIAG_FLAG_YES,
    "non_interactive": $SOVYX_DIAG_FLAG_NON_INTERACTIVE,
    "with_sudo": $SOVYX_DIAG_FLAG_WITH_SUDO,
    "skip_captures": $SOVYX_DIAG_FLAG_SKIP_CAPTURES,
    "test_suspend": $SOVYX_DIAG_FLAG_TEST_SUSPEND,
    "test_external_grab": $SOVYX_DIAG_FLAG_TEST_EXTERNAL_GRAB,
    "intrusive_restart_audio": $SOVYX_DIAG_FLAG_INTRUSIVE_RESTART_AUDIO,
    "with_powertop": $SOVYX_DIAG_FLAG_WITH_POWERTOP,
    "trace_syscalls": $SOVYX_DIAG_FLAG_TRACE_SYSCALLS,
    "skip_operator_prompts": $SOVYX_DIAG_FLAG_SKIP_OPERATOR_PROMPTS,
    "skip_guardian": $SOVYX_DIAG_FLAG_SKIP_GUARDIAN,
    "enable_ftrace": $SOVYX_DIAG_FLAG_ENABLE_FTRACE,
    "only": "$SOVYX_DIAG_FLAG_ONLY"
  },
  "python": {
    "path": "$SOVYX_DIAG_PYTHON",
    "kind": "$SOVYX_DIAG_PYTHON_KIND"
  },
  "token_available": $([[ -n "$SOVYX_DIAG_TOKEN" ]] && echo true || echo false),
  "status": "running"
}
EOF

# ─────────────────────────────────────────────────────────────────────────
# Initial evidence (bug_sovyx.txt + screenshots) — opcional
# ─────────────────────────────────────────────────────────────────────────

if [[ -z "$SOVYX_DIAG_INITIAL_EVIDENCE_DIR" ]]; then
    SOVYX_DIAG_INITIAL_EVIDENCE_DIR="$(pwd)/initial_evidence"
fi
if [[ -d "$SOVYX_DIAG_INITIAL_EVIDENCE_DIR" ]]; then
    log_info "copying initial evidence from $SOVYX_DIAG_INITIAL_EVIDENCE_DIR"
    cp -r "$SOVYX_DIAG_INITIAL_EVIDENCE_DIR"/. "$SOVYX_DIAG_OUTDIR/initial_evidence/" 2>/dev/null || \
        log_warn "failed to copy some initial evidence files"
else
    log_warn "no initial evidence dir at $SOVYX_DIAG_INITIAL_EVIDENCE_DIR"
fi

# ─────────────────────────────────────────────────────────────────────────
# Start followers (T2 completa isso; stub aqui)
# ─────────────────────────────────────────────────────────────────────────

start_followers

# ─────────────────────────────────────────────────────────────────────────
# Run layers (cada layer é sourced se existir; T4-T10 preenchem)
# ─────────────────────────────────────────────────────────────────────────

_maybe_source() {
    local lib="$1"
    if [[ -r "$SOVYX_DIAG_LIB_DIR/$lib" ]]; then
        # shellcheck source=/dev/null
        source "$SOVYX_DIAG_LIB_DIR/$lib"
        return 0
    fi
    log_warn "lib not present (skipping): $lib"
    return 1
}

# Finalize + alerts + states.
_maybe_source "finalize.sh" || log_warn "finalize.sh missing — tarball will not be built"
_maybe_source "alerts.sh"   || log_warn "alerts.sh missing — no proactive alerts"
_maybe_source "states.sh"   || log_warn "states.sh missing — T2 not implemented yet"

# AUDIT v3+ T7 — calibration, temporal guardian, operator prompts.
_maybe_source "selftest.sh"  || log_warn "selftest.sh missing — analyzer calibration skipped"
_maybe_source "T_guardian.sh" || log_warn "T_guardian.sh missing — temporal guardian skipped"
_maybe_source "O_prompts.sh"  || log_warn "O_prompts.sh missing — operator prompts skipped"

# AUDIT v3+ T0 — analyzer self-test MUST run before any measurement. If
# the instruments are miscalibrated, every downstream metric is suspect.
if declare -F run_analyzer_selftest >/dev/null 2>&1; then
    if ! run_analyzer_selftest; then
        log_error "T0 selftest FAILED — refusing to proceed with uncalibrated analyzer"
        alert_append "error" "analyzer_selftest FAILED at boot — aborting run to avoid producing untrustworthy metrics"
        exit 3
    fi
fi

# AUDIT v3+ T1 — start Temporal Guardian (background followers for
# intermittent events between discrete snapshots).
if declare -F start_guardian >/dev/null 2>&1; then
    start_guardian || log_warn "start_guardian reported a non-fatal error"
fi

# Camadas — preenchidas por T4..T10.
# Ordem do §3 do plano: zero-touch → ALSA(S_OFF) → PipeWire(S_IDLE) → PortAudio(S_ACTIVE) → Sovyx live → K → snapshots finais.

# Phase 1: S_OFF (A, B, C)
if declare -F enter_S_OFF >/dev/null 2>&1; then
    enter_S_OFF
fi
_layer_enabled "A" && _maybe_source "A_hardware.sh" && declare -F run_layer_A >/dev/null && run_layer_A
_layer_enabled "B" && _maybe_source "B_kernel.sh"   && declare -F run_layer_B >/dev/null && run_layer_B
_layer_enabled "C" && _maybe_source "C_alsa.sh"     && declare -F run_layer_C >/dev/null && run_layer_C

# Phase 2: S_IDLE (D, F, I, J — sem K; K depende do pipeline DESABILITADO
# E vem DEPOIS de H por ordem do plano v2 §3: zero-touch→C→D→E→H→K).
if declare -F enter_S_IDLE >/dev/null 2>&1; then
    enter_S_IDLE
fi
_layer_enabled "D" && _maybe_source "D_pipewire.sh" && declare -F run_layer_D >/dev/null && run_layer_D
_layer_enabled "F" && _maybe_source "F_session.sh"  && declare -F run_layer_F >/dev/null && run_layer_F
_layer_enabled "I" && _maybe_source "I_network.sh"  && declare -F run_layer_I >/dev/null && run_layer_I
_layer_enabled "J" && _maybe_source "J_latent.sh"   && declare -F run_layer_J >/dev/null && run_layer_J

# Phase 3: S_ACTIVE (E, G, H)
if declare -F enter_S_ACTIVE >/dev/null 2>&1; then
    enter_S_ACTIVE
fi
_layer_enabled "E" && _maybe_source "E_portaudio.sh"     && declare -F run_layer_E >/dev/null && run_layer_E
_layer_enabled "G" && _maybe_source "G_sovyx.sh"         && declare -F run_layer_G >/dev/null && run_layer_G
_layer_enabled "H" && _maybe_source "H_pipeline_live.sh" && declare -F run_layer_H >/dev/null && run_layer_H

# Phase 3.5: volta para S_IDLE (disable voice) para rodar K — o endpoint
# /api/voice/test/output recusa 409 PIPELINE_ACTIVE enquanto o pipeline
# produtivo estiver ativo (voice_test.py:484). Transição sem re-snapshot.
if declare -F transition_to_S_IDLE_for_K >/dev/null 2>&1; then
    transition_to_S_IDLE_for_K
fi
_layer_enabled "K" && _maybe_source "K_output.sh" && declare -F run_layer_K >/dev/null && run_layer_K

# Phase 4: S_RESIDUAL + optional states
if declare -F enter_S_RESIDUAL_t5 >/dev/null 2>&1; then
    enter_S_RESIDUAL_t5
    enter_S_RESIDUAL_t30
fi
if [[ "$SOVYX_DIAG_FLAG_TEST_SUSPEND" = "1" ]] && declare -F enter_S_POST_SUSPEND >/dev/null 2>&1; then
    enter_S_POST_SUSPEND
fi
if [[ "$SOVYX_DIAG_FLAG_TEST_EXTERNAL_GRAB" = "1" ]] && declare -F enter_S_EXTERNAL_GRAB >/dev/null 2>&1; then
    enter_S_EXTERNAL_GRAB
fi

# Phase 5: diffs inter-estado. Alerts + MANIFEST + tarball são gerados
# EXCLUSIVAMENTE dentro de finalize_package (trap EXIT) para evitar
# chamadas duplicadas a generate_alerts que duplicariam alerts.jsonl.
if declare -F generate_state_diffs >/dev/null 2>&1; then
    generate_state_diffs
fi

# AUDIT v3+ T3 — operator prompts (Etapa Final). Fecha gaps perceptuais
# que o script não mede: listen-describe de WAVs, subjective recall
# durante H, inspect de configs Lua, BIOS screenshots. DEVE rodar após
# todas as capturas (porque refere artefatos já gravados) e ANTES do
# finalize_package (que monta MANIFEST/cross-correlation/checksums).
if declare -F run_operator_prompts >/dev/null 2>&1; then
    run_operator_prompts || log_warn "operator prompts reported non-fatal error"
fi

# V4 Track D: mark the run as having reached normal completion. If the
# trap fires BEFORE this line (SIGTERM/SIGINT/SIGHUP mid-run, or any
# unhandled exit), cleanup sees sentinel=0 and forces status=partial
# even when $? happened to be 0 (signal delivered during an idle moment).
SOVYX_DIAG_RUN_COMPLETED=1

# Saída bem-sucedida → trap roda _cleanup → finalize_package(complete)
log_info "run completed; cleanup next"
exit 0
