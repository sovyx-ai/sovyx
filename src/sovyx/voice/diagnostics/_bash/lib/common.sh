#!/usr/bin/env bash
# lib/common.sh — infraestrutura compartilhada do sovyx-voice-diag.
#
# Sourced por sovyx-voice-diag.sh e cada lib/<camada>.sh. Expõe:
#
#   • run_step         — executa comando com timeout, captura stdout/stderr,
#                        registra em timeline.csv + RUNLOG.txt + MANIFEST.
#   • run_step_pipe    — idem, mas para comandos que devem ir direto a arquivo
#                        (ex.: `pw-dump > pw_dump.json`) preservando JSON/CSV.
#   • header_write     — escreve cabeçalho ISO-ns padrão em arquivos texto.
#   • manifest_append  — grava fragmento descritivo de um artefato.
#   • redact_env       — sanitiza secrets de uma stream.
#   • tool_has         — marca ferramenta como disponível/ausente em env matrix.
#   • resolve_sovyx_python / resolve_pw_defaults / etc.
#   • prompt_user      — interação via TTY com fallback não-interativo.
#   • _cleanup (trap)  — restauração de estado + tarball reprodutível.
#
# Convenções:
#   • set -uo pipefail (SEM -e — steps falham individualmente, não abortam).
#   • Todas as variáveis globais prefixadas SOVYX_DIAG_*.
#   • Funções internas prefixadas _.
#   • Nenhum comando fora deste arquivo deve escrever no timeline/runlog
#     diretamente — tudo passa por run_step / run_step_pipe.

set -uo pipefail

# ─────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────

readonly SOVYX_DIAG_VERSION="4.3.0"
readonly SOVYX_DIAG_SCRIPT_NAME="sovyx-voice-diag"
readonly SOVYX_DIAG_DEFAULT_TIMEOUT=30      # segundos, override por step
readonly SOVYX_DIAG_CAPTURE_TIMEOUT=10      # 7s de dado + margem
readonly SOVYX_DIAG_DOCTOR_TIMEOUT=60       # sovyx doctor pode ser lento
readonly SOVYX_DIAG_API_TIMEOUT=15          # curl contra dashboard
readonly SOVYX_DIAG_SUSPEND_RESUME_WAIT=15  # s depois de resume

# Regex de redação — aplicada a env + configs.
readonly SOVYX_DIAG_SECRET_REGEX='(?i)(token|secret|key|password|passwd|auth|cred|api[_-]?key|private[_-]?key|bearer|session[_-]?id)'

# ─────────────────────────────────────────────────────────────────────────
# Estado global (populado pelo main)
# ─────────────────────────────────────────────────────────────────────────

SOVYX_DIAG_OUTDIR=""
SOVYX_DIAG_RUNLOG=""
SOVYX_DIAG_TIMELINE=""
SOVYX_DIAG_MANIFEST_DIR=""
SOVYX_DIAG_ENV_MATRIX=""
SOVYX_DIAG_SUMMARY_JSON=""
SOVYX_DIAG_STATE="INIT"      # estado corrente — eixo 1 do plano
SOVYX_DIAG_PYTHON=""         # python do venv Sovyx (resolvido em resolve_sovyx_python)
SOVYX_DIAG_PYTHON_KIND=""    # pipx | sovyx-bin | system
SOVYX_DIAG_TOKEN=""          # conteúdo de ~/.sovyx/token se existir
SOVYX_DIAG_START_UTC_NS=""
SOVYX_DIAG_START_MONO_NS=""

# Estado inicial do Sovyx (registrado ANTES de qualquer mudança)
SOVYX_DIAG_INITIAL_SOVYX_RUNNING="unknown"   # yes | no | unknown
SOVYX_DIAG_INITIAL_VOICE_ENABLED="unknown"   # yes | no | unknown

# Flags opt-in
SOVYX_DIAG_FLAG_YES=0
SOVYX_DIAG_FLAG_NON_INTERACTIVE=0
SOVYX_DIAG_FLAG_WITH_SUDO=0
SOVYX_DIAG_FLAG_SKIP_CAPTURES=0
SOVYX_DIAG_FLAG_TEST_SUSPEND=0
SOVYX_DIAG_FLAG_TEST_EXTERNAL_GRAB=0
SOVYX_DIAG_FLAG_INTRUSIVE_RESTART_AUDIO=0
SOVYX_DIAG_FLAG_WITH_POWERTOP=0
SOVYX_DIAG_FLAG_TRACE_SYSCALLS=1   # default ON (auto-off se ptrace bloqueia)
# AUDIT v3+ T7 — new enterprise-grade flags.
SOVYX_DIAG_FLAG_SKIP_OPERATOR_PROMPTS=0   # skip Etapa Final de prompts ao operador
SOVYX_DIAG_FLAG_SKIP_GUARDIAN=0           # skip Temporal Guardian followers
SOVYX_DIAG_FLAG_ENABLE_FTRACE=0           # habilita ftrace em G (intrusivo)
# v0.30.19 T2.3 — surgical layer selection. Empty = all layers run
# (default). Comma-separated letters (e.g. "A,C,D,E,J") restrict the
# run to ONLY the listed layers; the calibration measurer uses this
# to cut full diag (~10min) down to the minimum needed for calibration
# rules (~30s). Phase enter/exit + selftest still run unconditionally
# because they own the state-machine + correctness contract.
SOVYX_DIAG_FLAG_ONLY=""
SOVYX_DIAG_INITIAL_EVIDENCE_DIR=""

# Follower PIDs — preenchidos por start_followers, mortos no trap
SOVYX_DIAG_FOLLOWER_PIDS=()

# Intrusive-restart tracking — se 1, trap reinicia pipewire/wireplumber
SOVYX_DIAG_AUDIO_RESTART_PENDING=0

# PipeWire default IDs (resolvidos por resolve_pw_defaults)
SOVYX_DIAG_DEFAULT_SOURCE_ID=""
SOVYX_DIAG_DEFAULT_SOURCE_NAME=""
SOVYX_DIAG_DEFAULT_SINK_ID=""
SOVYX_DIAG_DEFAULT_SINK_NAME=""

# Rastreio de completude — preenchido ao longo da coleta
SOVYX_DIAG_STEPS_TOTAL=0
SOVYX_DIAG_STEPS_OK=0
SOVYX_DIAG_STEPS_WARN=0
SOVYX_DIAG_STEPS_FAIL=0
SOVYX_DIAG_STEPS_TIMEOUT=0

# ─────────────────────────────────────────────────────────────────────────
# Tempo
# ─────────────────────────────────────────────────────────────────────────

# Emite timestamp UTC com precisão de nanossegundo (ISO 8601).
#
# AUDIT v3: `date` com `%N` é GNU-específico (glibc). Em BusyBox ou
# macOS, `%N` é emitido literalmente e produz `2026-04-22T12:00:00.%NZ`
# — corrompe TODO header forensic. ``_init_common`` valida uma vez.
# Aqui, apenas chamamos; o init aborta se incompatível.
now_utc_ns() {
    date -u +%Y-%m-%dT%H:%M:%S.%NZ
}

# Emite monotonic_ns via Python.
#
# AUDIT v3: antes retornava `echo 0` em falha — silenciosamente
# corrompia `duration_ms=(end-start)/1e6` para valores negativos
# gigantes (se só o end falhou) ou 0 (ambos falham). Agora FAIL-FAST
# com mensagem no stderr e retcode distinto — o caller vê o erro.
now_monotonic_ns() {
    python3 -c 'import time; print(time.monotonic_ns())' 2>/dev/null && return 0
    log_error "now_monotonic_ns: python3 unavailable; clock corrupted"
    return 1
}

# Par UTC + monotonic em JSON de uma linha — **atômico**.
#
# AUDIT v3: antes fazia duas chamadas python separadas (cada uma
# com fork+exec overhead de 1-5 ms em hosts carregados). O par
# (utc, monotonic) podia ficar dessincronizado o suficiente para
# quebrar correlações forenses. Agora um único `python3 -c` lê
# ambos os relógios ADJACENTES dentro do mesmo interpretador, com
# máximo ~10 µs de drift inter-leitura — negligível para forensic.
# Também deriva o ISO do SAME `time_ns()` value para garantir que
# a parte fracionária de segundo corresponde ao segundo ISO.
now_pair_json() {
    python3 - <<'PYEOF' 2>/dev/null
import datetime as _dt
import json
import sys
import time

# AUDIT v3: capture BOTH clocks before any formatting work so the
# pair is as close to atomic as the language permits.
ns = time.time_ns()
mono = time.monotonic_ns()

secs = ns // 1_000_000_000
frac = ns % 1_000_000_000
dt = _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc)
iso = dt.strftime('%Y-%m-%dT%H:%M:%S') + f".{frac:09d}Z"

json.dump({"utc_iso_ns": iso, "monotonic_ns": mono}, sys.stdout,
          separators=(",", ":"))
sys.stdout.write("\n")
PYEOF
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        log_error "now_pair_json: python3 unavailable; clock corrupted"
        return 1
    fi
    return 0
}

# ─────────────────────────────────────────────────────────────────────────
# Layer gating (v0.30.19 T2.3 --only flag)
# ─────────────────────────────────────────────────────────────────────────

# Returns 0 (true) if layer ``letter`` should run, 1 otherwise.
#
# When SOVYX_DIAG_FLAG_ONLY is empty (default), every layer runs. When
# set (e.g. "A,C,D,E,J"), only the listed layers run; everything else
# is silently skipped. Phase enter/exit + selftest must NOT be gated
# through this helper -- they own state-machine transitions and
# correctness contracts that downstream layers depend on.
#
# Comparison is case-sensitive on the single-letter layer code that
# matches the lib filename prefix (A_hardware.sh -> "A").
_layer_enabled() {
    local letter="$1"
    [[ -z "$SOVYX_DIAG_FLAG_ONLY" ]] && return 0
    case ",${SOVYX_DIAG_FLAG_ONLY}," in
        *,"$letter",*) return 0 ;;
        *) return 1 ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────
# Logging + manifest
# ─────────────────────────────────────────────────────────────────────────

# Logger humano — vai para stderr para não contaminar stdout de pipes.
log_info()  { printf '[%s] [INFO]  %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
log_warn()  { printf '[%s] [WARN]  %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
log_error() { printf '[%s] [ERROR] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
log_debug() {
    [[ "${SOVYX_DIAG_DEBUG:-0}" = "1" ]] || return 0
    printf '[%s] [DEBUG] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2
}

# Cabeçalho ISO-ns padrão em arquivos texto (§4.5 do plano).
# Uso: header_write <file> <step_id> <cmd> <retcode> <duration_ms> [tool_version]
header_write() {
    local file="$1" step_id="$2" cmd="$3" retcode="$4" duration_ms="$5"
    local tool_version="${6:-}"
    local tmp="${file}.hdr.tmp"
    {
        printf '# sovyx-voice-diag v%s\n' "$SOVYX_DIAG_VERSION"
        printf '# step_id: %s\n' "$step_id"
        printf '# state: %s\n' "$SOVYX_DIAG_STATE"
        printf '# timestamp_utc: %s\n' "$(now_utc_ns)"
        printf '# monotonic_ns: %s\n' "$(now_monotonic_ns)"
        printf '# command: %s\n' "$cmd"
        printf '# retcode: %s\n' "$retcode"
        printf '# duration_ms: %s\n' "$duration_ms"
        [[ -n "$tool_version" ]] && printf '# tool_version: %s\n' "$tool_version"
        printf '#---\n'
        [[ -f "$file" ]] && cat "$file"
    } > "$tmp" 2>/dev/null && mv "$tmp" "$file"
}

# Acrescenta uma linha ao MANIFEST (fragmento por step). Arquivo final é
# montado por assemble_manifest em T11.
#
# Uso: manifest_append <step_id> <path> <purpose> [hypothesis]
manifest_append() {
    local step_id="$1" path="$2" purpose="$3" hypothesis="${4:-}"
    local fragment="$SOVYX_DIAG_MANIFEST_DIR/${step_id}.md"
    {
        printf -- '- **%s** — `%s`\n' "$step_id" "$path"
        printf '    %s\n' "$purpose"
        [[ -n "$hypothesis" ]] && printf '    Hipótese: %s\n' "$hypothesis"
    } >> "$fragment"
}

# Acrescenta alerta proativo (§9 do plano) — usado por T12.
# Uso: alert_append <severity:info|warn|error> <message>
alert_append() {
    local severity="$1" msg="$2"
    local alerts_file="$SOVYX_DIAG_OUTDIR/_diagnostics/alerts.jsonl"
    printf '{"severity":"%s","state":"%s","message":%s,"at":%s}\n' \
        "$severity" "$SOVYX_DIAG_STATE" \
        "$(printf '%s' "$msg" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
        "$(now_pair_json | tr -d '\n')" \
        >> "$alerts_file"
}

# V4.3 — Wrapper para subprocessos "fallíveis" (Python helpers, etc.)
# que historicamente swallow stderr via 2>/dev/null + || true. Captura
# stderr num sidecar pra inspeção forense. Sem isso, exceptions Python
# silenciosas mascaram bugs de análise (ex: numpy ABI break, scipy import
# failed, JSON malformed).
#
# Uso: _run_fallible <label> <cmd...>
# Efeitos:
#   - stdout passa direto (caller faz seu redirect)
#   - stderr → _diagnostics/fallible/<label>.err se rc != 0
#   - rc preservado e retornado
#
# Exemplo:
#   _run_fallible "alert_band_limited" python3 -c '...' < input.json
_run_fallible() {
    local label="$1"; shift
    local err_dir="$SOVYX_DIAG_OUTDIR/_diagnostics/fallible"
    mkdir -p "$err_dir"
    local err_file="$err_dir/${label}.err"
    local rc=0
    "$@" 2> "$err_file"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        # rc=0 → discarta err se está vazio (caso comum); senão preserva
        # como warning side-channel.
        if [[ ! -s "$err_file" ]]; then
            rm -f "$err_file"
        fi
    else
        # rc != 0 → preserva stderr + emite alert.
        if [[ -s "$err_file" ]]; then
            local first_line
            first_line=$(head -1 "$err_file" 2>/dev/null | head -c 200)
            alert_append "warn" "fallible_subprocess_failed: label=$label rc=$rc stderr_head='$first_line' (full in _diagnostics/fallible/${label}.err)"
        else
            alert_append "warn" "fallible_subprocess_failed: label=$label rc=$rc (no stderr)"
        fi
    fi
    return "$rc"
}

# Marca ferramenta no environment_matrix.
# Uso: tool_has <cmd> → 0 se presente, 1 se ausente; registra status.
tool_has() {
    local cmd="$1" version=""
    if command -v "$cmd" >/dev/null 2>&1; then
        version=$("$cmd" --version 2>&1 | head -1 || true)
        printf 'present\t%s\t%s\n' "$cmd" "$version" >> "$SOVYX_DIAG_ENV_MATRIX"
        return 0
    else
        printf 'absent\t%s\t-\n' "$cmd" >> "$SOVYX_DIAG_ENV_MATRIX"
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# CSV atômico
# ─────────────────────────────────────────────────────────────────────────

# Acrescenta linha ao timeline.csv com escape seguro.
# Uso: timeline_append <step_id> <start_utc_ns> <start_mono_ns> <end_utc_ns> \
#                     <end_mono_ns> <duration_ms> <cmd> <retcode> <out_path> <notes>
timeline_append() {
    python3 - "$@" <<'PYEOF' >> "$SOVYX_DIAG_TIMELINE"
import csv, sys
writer = csv.writer(sys.stdout, quoting=csv.QUOTE_MINIMAL)
writer.writerow(sys.argv[1:])
PYEOF
}

# Inicia timeline.csv com header.
_init_timeline() {
    mkdir -p "$(dirname "$SOVYX_DIAG_TIMELINE")"
    {
        echo "step_id,state,start_utc_ns,start_monotonic_ns,end_utc_ns,end_monotonic_ns,duration_ms,cmd,retcode,out_path,notes"
    } > "$SOVYX_DIAG_TIMELINE"
}

# ─────────────────────────────────────────────────────────────────────────
# run_step — execução canônica com timeout, métricas e timeline.
# ─────────────────────────────────────────────────────────────────────────

# Uso:
#   run_step <step_id> <out_path> <timeout_s> <cmd...>
#
# Efeitos:
#   • Cria diretório pai de <out_path>.
#   • Executa <cmd...> com timeout <timeout_s>s. Redireciona stdout+stderr
#     para <out_path>.
#   • Captura retcode; em caso de timeout marca TIMEOUT no manifesto.
#   • Escreve cabeçalho ISO-ns no topo.
#   • Acrescenta linha ao timeline.csv + RUNLOG.txt.
#   • Incrementa contadores globais.
#
# Retorna: retcode do comando (ou 124 em timeout). Nunca aborta o script.
run_step() {
    local step_id="$1" out_path="$2" timeout_s="$3"
    shift 3
    local cmd=("$@")
    local cmd_str
    cmd_str=$(printf '%q ' "${cmd[@]}")
    cmd_str="${cmd_str% }"   # strip trailing space

    mkdir -p "$(dirname "$out_path")"

    local start_utc start_mono end_utc end_mono duration_ms retcode notes=""
    start_utc=$(now_utc_ns)
    start_mono=$(now_monotonic_ns)

    # Execute with timeout. We preserve retcode via a PIPESTATUS trick.
    # `timeout` returns 124 on timeout.
    set +o pipefail
    timeout --preserve-status --kill-after=5 "$timeout_s" \
        "${cmd[@]}" >"$out_path" 2>&1
    retcode=$?
    set -o pipefail

    end_utc=$(now_utc_ns)
    end_mono=$(now_monotonic_ns)
    duration_ms=$(( (end_mono - start_mono) / 1000000 ))

    if [[ $retcode -eq 124 ]]; then
        notes="TIMEOUT after ${timeout_s}s"
        (( SOVYX_DIAG_STEPS_TIMEOUT++ )) || true
    elif [[ $retcode -eq 0 ]]; then
        notes="ok"
        (( SOVYX_DIAG_STEPS_OK++ )) || true
    else
        notes="retcode=$retcode"
        (( SOVYX_DIAG_STEPS_FAIL++ )) || true
    fi

    # Detect empty output (silent failure).
    if [[ ! -s "$out_path" ]]; then
        notes="$notes; empty_output"
    fi

    (( SOVYX_DIAG_STEPS_TOTAL++ )) || true

    header_write "$out_path" "$step_id" "$cmd_str" "$retcode" "$duration_ms"
    timeline_append "$step_id" "$SOVYX_DIAG_STATE" "$start_utc" "$start_mono" \
                    "$end_utc" "$end_mono" "$duration_ms" "$cmd_str" \
                    "$retcode" "$out_path" "$notes"

    {
        printf '[%s] step=%s state=%s retcode=%s duration_ms=%s out=%s notes=%s\n' \
            "$start_utc" "$step_id" "$SOVYX_DIAG_STATE" "$retcode" \
            "$duration_ms" "$out_path" "$notes"
        printf '  cmd: %s\n' "$cmd_str"
    } >> "$SOVYX_DIAG_RUNLOG"

    return "$retcode"
}

# run_step_pipe — preserva stdout do comando SEM cabeçalho (para JSON/CSV/binário).
# Stderr vai para <out_path>.stderr + RUNLOG; cabeçalho vai para <out_path>.meta.
# Timeline e RUNLOG iguais a run_step.
run_step_pipe() {
    local step_id="$1" out_path="$2" timeout_s="$3"
    shift 3
    local cmd=("$@")
    local cmd_str
    cmd_str=$(printf '%q ' "${cmd[@]}")
    cmd_str="${cmd_str% }"

    mkdir -p "$(dirname "$out_path")"

    local start_utc start_mono end_utc end_mono duration_ms retcode notes=""
    start_utc=$(now_utc_ns)
    start_mono=$(now_monotonic_ns)

    set +o pipefail
    timeout --preserve-status --kill-after=5 "$timeout_s" \
        "${cmd[@]}" >"$out_path" 2>"${out_path}.stderr"
    retcode=$?
    set -o pipefail

    end_utc=$(now_utc_ns)
    end_mono=$(now_monotonic_ns)
    duration_ms=$(( (end_mono - start_mono) / 1000000 ))

    if [[ $retcode -eq 124 ]]; then
        notes="TIMEOUT after ${timeout_s}s"
        (( SOVYX_DIAG_STEPS_TIMEOUT++ )) || true
    elif [[ $retcode -eq 0 ]]; then
        notes="ok"
        (( SOVYX_DIAG_STEPS_OK++ )) || true
    else
        notes="retcode=$retcode"
        (( SOVYX_DIAG_STEPS_FAIL++ )) || true
    fi

    if [[ ! -s "$out_path" ]]; then
        notes="$notes; empty_stdout"
    fi

    (( SOVYX_DIAG_STEPS_TOTAL++ )) || true

    # Metadata sidecar — caller pode ler para saber cabeçalho sem contaminar stdout.
    {
        printf 'step_id: %s\n' "$step_id"
        printf 'state: %s\n' "$SOVYX_DIAG_STATE"
        printf 'timestamp_utc: %s\n' "$start_utc"
        printf 'monotonic_ns: %s\n' "$start_mono"
        printf 'command: %s\n' "$cmd_str"
        printf 'retcode: %s\n' "$retcode"
        printf 'duration_ms: %s\n' "$duration_ms"
        printf 'notes: %s\n' "$notes"
    } > "${out_path}.meta"

    timeline_append "$step_id" "$SOVYX_DIAG_STATE" "$start_utc" "$start_mono" \
                    "$end_utc" "$end_mono" "$duration_ms" "$cmd_str" \
                    "$retcode" "$out_path" "$notes"

    {
        printf '[%s] step=%s state=%s retcode=%s duration_ms=%s out=%s notes=%s\n' \
            "$start_utc" "$step_id" "$SOVYX_DIAG_STATE" "$retcode" \
            "$duration_ms" "$out_path" "$notes"
        printf '  cmd: %s\n' "$cmd_str"
    } >> "$SOVYX_DIAG_RUNLOG"

    return "$retcode"
}

# ─────────────────────────────────────────────────────────────────────────
# Redação
# ─────────────────────────────────────────────────────────────────────────

# Redige secrets de stdin → stdout. Linhas que tenham padrão `nome=val` ou
# `nome: val` onde nome contém token/secret/key/... (case-insensitive) têm o
# valor substituído por <redacted>. Preferimos over-redigir a vazar.
redact_stream() {
    python3 - <<'PYEOF'
import re, sys
# Python 3.12+ exige que flags inline estejam no início da expressão; usamos
# re.IGNORECASE diretamente em vez de `(?i)` embutido.
pat = re.compile(
    r'((?:token|secret|key|password|passwd|auth|cred|api[_-]?key|private[_-]?key|bearer|session[_-]?id)\s*[=:]\s*)(\S+)',
    re.IGNORECASE,
)
for line in sys.stdin:
    sys.stdout.write(pat.sub(r'\1<redacted>', line))
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────
# Resolução de paths Sovyx
# ─────────────────────────────────────────────────────────────────────────

# Resolve o Python do venv Sovyx. Popula SOVYX_DIAG_PYTHON e _KIND.
# Retorna 0 se achou, 1 se não.
resolve_sovyx_python() {
    local py=""

    # 1. pipx
    local pipx_dir
    pipx_dir=$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null || true)
    if [[ -n "$pipx_dir" && -x "$pipx_dir/sovyx/bin/python" ]]; then
        py="$pipx_dir/sovyx/bin/python"
        SOVYX_DIAG_PYTHON_KIND="pipx"
    fi

    # 2. Shebang do binário sovyx. Em pipx moderno, `~/.local/bin/sovyx` é
    #    um wrapper Python cujo shebang `#!/.../pipx/venvs/sovyx/bin/python`
    #    aponta exatamente para o Python do venv — mesmo se pipx environment
    #    não listar PIPX_LOCAL_VENVS (várias versões não listam).
    if [[ -z "$py" ]]; then
        local sovyx_bin
        sovyx_bin=$(command -v sovyx 2>/dev/null || true)
        if [[ -n "$sovyx_bin" && -r "$sovyx_bin" ]]; then
            local shebang shebang_py
            shebang=$(head -c 200 "$sovyx_bin" 2>/dev/null | head -1 || true)
            # Forma aceita: #!/abs/path/python[3[.N]] [args]
            if [[ "$shebang" =~ ^\#\![[:space:]]*([^[:space:]]+) ]]; then
                shebang_py="${BASH_REMATCH[1]}"
                if [[ -x "$shebang_py" ]] && "$shebang_py" -c 'import sovyx' >/dev/null 2>&1; then
                    py="$shebang_py"
                    SOVYX_DIAG_PYTHON_KIND="shebang"
                fi
            fi
        fi
    fi

    # 3. readlink do binário sovyx + dirname + python adjacente.
    if [[ -z "$py" ]]; then
        local sovyx_bin
        sovyx_bin=$(command -v sovyx 2>/dev/null || true)
        if [[ -n "$sovyx_bin" ]]; then
            local real_bin
            real_bin=$(readlink -f "$sovyx_bin" 2>/dev/null || true)
            if [[ -n "$real_bin" ]]; then
                local venv_bin
                venv_bin=$(dirname "$real_bin")
                if [[ -x "$venv_bin/python" ]]; then
                    py="$venv_bin/python"
                    SOVYX_DIAG_PYTHON_KIND="sovyx-bin"
                elif [[ -x "$venv_bin/python3" ]]; then
                    py="$venv_bin/python3"
                    SOVYX_DIAG_PYTHON_KIND="sovyx-bin"
                fi
            fi
        fi
    fi

    # 4. system python com import sovyx
    if [[ -z "$py" ]]; then
        local sys_py
        sys_py=$(python3 -c 'import sovyx, sys; print(sys.executable)' 2>/dev/null || true)
        if [[ -n "$sys_py" && -x "$sys_py" ]]; then
            py="$sys_py"
            SOVYX_DIAG_PYTHON_KIND="system"
        fi
    fi

    if [[ -n "$py" ]]; then
        SOVYX_DIAG_PYTHON="$py"
        log_info "sovyx python resolved: $py ($SOVYX_DIAG_PYTHON_KIND)"
        return 0
    fi
    SOVYX_DIAG_PYTHON_KIND="not_found"
    log_warn "sovyx python not resolved — PortAudio / Silero / Kokoro blocks will be skipped"
    return 1
}

# Lê o token do dashboard de ~/.sovyx/token. Popula SOVYX_DIAG_TOKEN.
# Retorna 0 se achou, 1 se não.
resolve_dashboard_token() {
    local path="$HOME/.sovyx/token"
    if [[ -r "$path" ]]; then
        SOVYX_DIAG_TOKEN=$(cat "$path" | tr -d '\r\n')
        log_info "dashboard token loaded (${#SOVYX_DIAG_TOKEN} chars)"
        return 0
    fi
    log_warn "dashboard token not found at $path — API blocks will be skipped"
    return 1
}

# Resolve default PipeWire source/sink — IDs e nomes. Popula as 4 variáveis
# SOVYX_DIAG_DEFAULT_{SOURCE,SINK}_{ID,NAME}. Retorna 0 se ambos resolvidos,
# 1 se algum ausente.
resolve_pw_defaults() {
    SOVYX_DIAG_DEFAULT_SOURCE_ID=""
    SOVYX_DIAG_DEFAULT_SOURCE_NAME=""
    SOVYX_DIAG_DEFAULT_SINK_ID=""
    SOVYX_DIAG_DEFAULT_SINK_NAME=""

    if command -v pactl >/dev/null 2>&1; then
        SOVYX_DIAG_DEFAULT_SOURCE_NAME=$(pactl get-default-source 2>/dev/null || true)
        SOVYX_DIAG_DEFAULT_SINK_NAME=$(pactl get-default-sink 2>/dev/null || true)
    fi

    if command -v wpctl >/dev/null 2>&1; then
        # `wpctl status` mostra linhas tipo "  * 42. Friendly Name [vol: ..]"
        # onde o asterisco indica o default. Extraímos o ID via match() 2-arg
        # (POSIX — funciona em mawk/gawk) + substr.
        local wp_out
        wp_out=$(wpctl status 2>/dev/null || true)
        if [[ -n "$wp_out" ]]; then
            SOVYX_DIAG_DEFAULT_SOURCE_ID=$(
                awk '
                    /^ *Audio/ { in_audio=1 }
                    /^ *Video/ { in_audio=0 }
                    in_audio && /Sources:/ { in_sources=1; next }
                    in_audio && /Sinks:|Filters:|Streams:/ { in_sources=0 }
                    in_sources && /\*/ {
                        if (match($0, /[0-9]+\./)) {
                            print substr($0, RSTART, RLENGTH - 1)
                            exit
                        }
                    }
                ' <<<"$wp_out"
            )
            SOVYX_DIAG_DEFAULT_SINK_ID=$(
                awk '
                    /^ *Audio/ { in_audio=1 }
                    /^ *Video/ { in_audio=0 }
                    in_audio && /Sinks:/ { in_sinks=1; next }
                    in_audio && /Sources:|Filters:|Streams:/ { in_sinks=0 }
                    in_sinks && /\*/ {
                        if (match($0, /[0-9]+\./)) {
                            print substr($0, RSTART, RLENGTH - 1)
                            exit
                        }
                    }
                ' <<<"$wp_out"
            )
        fi
    fi

    if [[ -n "$SOVYX_DIAG_DEFAULT_SOURCE_NAME" && -n "$SOVYX_DIAG_DEFAULT_SINK_NAME" ]]; then
        log_info "pw defaults: source=$SOVYX_DIAG_DEFAULT_SOURCE_NAME id=${SOVYX_DIAG_DEFAULT_SOURCE_ID:-?} sink=$SOVYX_DIAG_DEFAULT_SINK_NAME id=${SOVYX_DIAG_DEFAULT_SINK_ID:-?}"
        return 0
    fi
    log_warn "pw defaults incomplete — some captures will be skipped"
    return 1
}

# ─────────────────────────────────────────────────────────────────────────
# Sovyx lifecycle helpers (usados por lib/states.sh mas úteis aqui)
# ─────────────────────────────────────────────────────────────────────────

# Retorna stdout com PIDs APENAS do daemon Sovyx (um por linha).
# Filtra pelo link /proc/$pid/exe — só aceita PIDs cujo exe é um Python
# interpreter ou o binário sovyx instalado. Exclui:
#   - o próprio script (bash)
#   - subshells bash
#   - o PID atual ($$)
#   - grep/pgrep/sed/awk/etc.
# Isso evita o suicídio clássico quando pgrep -f 'sovyx' casa com o próprio
# script (cujo nome é sovyx-voice-diag.sh).
_sovyx_daemon_pids() {
    local pid exe_base
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        [[ "$pid" = "$$" ]] && continue
        # Skip any ancestor (parent of this script).
        [[ "$pid" = "$PPID" ]] && continue
        # Resolve /proc/$pid/exe → only accept python or the sovyx binary.
        exe_base=$(readlink "/proc/$pid/exe" 2>/dev/null | xargs -r basename 2>/dev/null || true)
        case "$exe_base" in
            python|python3|python3.*|sovyx)
                printf '%s\n' "$pid"
                ;;
        esac
    done < <(pgrep -f 'sovyx' 2>/dev/null || true)
}

# Verifica se o daemon Sovyx está rodando. Retorna 0 se sim, 1 se não.
# Considera: socket Unix existe OU há pelo menos 1 PID de daemon (filtrado
# via /proc/exe para não casar com o próprio script bash).
sovyx_is_running() {
    if [[ -S "$HOME/.sovyx/sovyx.sock" ]]; then
        return 0
    fi
    local -a pids=()
    mapfile -t pids < <(_sovyx_daemon_pids)
    [[ "${#pids[@]}" -gt 0 ]]
}

# Mata todos os PIDs do daemon Sovyx com o sinal dado. NUNCA mata o próprio
# script. Uso: _kill_sovyx_daemon <SIGNAL>
_kill_sovyx_daemon() {
    local signal="${1:-TERM}"
    local -a pids=()
    mapfile -t pids < <(_sovyx_daemon_pids)
    local pid
    for pid in "${pids[@]}"; do
        [[ -n "$pid" ]] && kill "-$signal" "$pid" 2>/dev/null || true
    done
}

# GET /api/voice/status (leve) para detectar voice_enabled. Retorna "yes",
# "no" ou "unknown" via stdout. Silencia erros.
sovyx_voice_enabled_state() {
    [[ -z "$SOVYX_DIAG_TOKEN" ]] && { echo "unknown"; return; }
    local body
    body=$(curl -sS --max-time "$SOVYX_DIAG_API_TIMEOUT" \
        -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
        "http://127.0.0.1:7777/api/voice/status" 2>/dev/null || echo "")
    if [[ -z "$body" ]]; then
        echo "unknown"; return
    fi
    # Simples: se o body contém "enabled":true / "state":"running" → yes.
    if grep -qE '"(enabled|running)":[[:space:]]*true' <<<"$body"; then
        echo "yes"
    elif grep -qE '"(enabled|running)":[[:space:]]*false' <<<"$body"; then
        echo "no"
    else
        echo "unknown"
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────

# prompt_user <msg> [timeout_s] — solicita ENTER do usuário. Silencioso em
# --non-interactive. Aborta se sem TTY e sem flag.
# Retorna 0 se usuário confirmou, 1 se timeout ou sem TTY com flag.
prompt_user() {
    local msg="$1" timeout_s="${2:-120}"
    if [[ "$SOVYX_DIAG_FLAG_NON_INTERACTIVE" = "1" ]]; then
        log_info "non-interactive mode: skipping prompt '$msg'"
        return 1
    fi
    if [[ ! -t 0 ]]; then
        log_error "no TTY available and --non-interactive not set. Abort."
        exit 2
    fi
    printf '\n\033[1;36m>>> %s\033[0m\n' "$msg" >&2
    printf '    (pressione ENTER para continuar — timeout %ss)\n' "$timeout_s" >&2
    local _
    if ! read -r -t "$timeout_s" _; then
        log_warn "prompt timed out"
        return 1
    fi
    return 0
}

# prompt_emit_structured <type> <phrase> [seconds] — emite o prompt
# atual como JSONL para uma file side-channel observada pelo orquestrador
# Python. NO-OP quando $SOVYX_DIAG_PROMPTS_FILE não está setada (operadores
# CLI rodando sovyx doctor voice --full-diag diretamente).
#
# Contrato P3 (mission MISSION-voice-calibration-extreme-audit-2026-05-06.md
# §7): bash escreve uma linha JSON por prompt; orchestrator faz tail do
# file a cada 500 ms e empurra cada linha pra state.extras.current_prompt
# pra que o frontend renderize <CapturePrompt> em tempo real.
#
# Args:
#   type     — closed enum {speak, silence}
#   phrase   — texto a falar (NULL quando type=silence)
#   seconds  — duração de silêncio (NULL quando type=speak)
#
# Shell-injection nota: usa bash heredoc + assume que phrase tem only
# os caracteres bounded-set escolhidos pelo diag (NÃO operator-set).
# JSON-escaping mínimo: backslashes + double-quotes via expansão padrão
# de bash (sed inline). Para expansão futura considerar python3 -c json
# se phrase virar operator-set.
prompt_emit_structured() {
    local type="$1" phrase="$2" seconds="${3:-}"
    if [[ -z "${SOVYX_DIAG_PROMPTS_FILE:-}" ]]; then
        return 0
    fi
    # Escape backslash + double-quote (mínimo necessário para JSON).
    local phrase_escaped
    phrase_escaped="${phrase//\\/\\\\}"
    phrase_escaped="${phrase_escaped//\"/\\\"}"
    local seconds_field
    if [[ -z "$seconds" ]]; then
        seconds_field="null"
    else
        seconds_field="$seconds"
    fi
    local utc_now mono_now
    utc_now=$(now_utc_ns) || return 0
    mono_now=$(now_monotonic_ns) || mono_now="null"
    local json
    json=$(printf '{"type":"%s","phrase":"%s","seconds":%s,"emitted_at_utc":"%s","emitted_at_mono_ns":%s}' \
        "$type" "$phrase_escaped" "$seconds_field" "$utc_now" "$mono_now")
    # Atomic single-line append; bash open(O_APPEND) is atomic for writes
    # smaller than PIPE_BUF (~4 KB on Linux), which our payload always is.
    echo "$json" >> "$SOVYX_DIAG_PROMPTS_FILE" 2>/dev/null || true
}

# prompt_yn <msg> → 0 = sim, 1 = não. Respeita --yes (retorna 0 sempre).
prompt_yn() {
    local msg="$1"
    if [[ "$SOVYX_DIAG_FLAG_YES" = "1" ]]; then
        log_info "auto-yes: $msg"
        return 0
    fi
    if [[ ! -t 0 ]]; then
        log_error "no TTY and no --yes. Abort."
        exit 2
    fi
    printf '\n\033[1;33m??? %s (y/N)\033[0m ' "$msg" >&2
    local answer
    if ! read -r -t 60 answer; then
        log_warn "confirmation timed out — treating as no"
        return 1
    fi
    [[ "$answer" =~ ^[yY]([eE][sS])?$ ]]
}

# prompt_did_hear — usado em playback tests da camada K.
# Retorna: "y" | "n" | "timeout" via stdout.
prompt_did_hear() {
    local label="$1"
    if [[ "$SOVYX_DIAG_FLAG_NON_INTERACTIVE" = "1" || ! -t 0 ]]; then
        echo "skip"
        return 0
    fi
    printf '\n\033[1;36m>>> %s\033[0m\n' "$label" >&2
    printf '    Você OUVIU o som? (y/n/ENTER=skip, timeout 30s): ' >&2
    local answer
    if ! read -r -t 30 answer; then
        echo "timeout"; return 0
    fi
    case "$answer" in
        [yY]*) echo "y" ;;
        [nN]*) echo "n" ;;
        *)     echo "skip" ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────
# Followers (journalctl / dmesg / sovyx.log) — §4.5
# ─────────────────────────────────────────────────────────────────────────

# Inicia followers em background. Gravados em _diagnostics/.
# Populates SOVYX_DIAG_FOLLOWER_PIDS.
start_followers() {
    local diag_dir="$SOVYX_DIAG_OUTDIR/_diagnostics"
    mkdir -p "$diag_dir"

    # 1. journalctl --user -f
    if tool_has journalctl >/dev/null; then
        journalctl --user --since now --output=short-iso-precise -f \
            > "$diag_dir/journalctl_user_follow.log" 2>&1 &
        SOVYX_DIAG_FOLLOWER_PIDS+=($!)
        log_info "follower: journalctl --user -f (pid ${SOVYX_DIAG_FOLLOWER_PIDS[-1]})"

        # 2. journalctl -k -f (sempre ok unprivileged via journald)
        journalctl -k --since now --output=short-iso-precise -f \
            > "$diag_dir/journalctl_kernel_follow.log" 2>&1 &
        SOVYX_DIAG_FOLLOWER_PIDS+=($!)
        log_info "follower: journalctl -k -f (pid ${SOVYX_DIAG_FOLLOWER_PIDS[-1]})"
    fi

    # 3. dmesg -w se dmesg_restrict==0 ou temos sudo
    local restrict=1
    [[ -r /proc/sys/kernel/dmesg_restrict ]] && \
        restrict=$(cat /proc/sys/kernel/dmesg_restrict 2>/dev/null || echo 1)

    if [[ "$restrict" = "0" ]] && command -v dmesg >/dev/null 2>&1; then
        dmesg --follow --time-format=iso \
            > "$diag_dir/dmesg_follow.log" 2>&1 &
        SOVYX_DIAG_FOLLOWER_PIDS+=($!)
        log_info "follower: dmesg -w (pid ${SOVYX_DIAG_FOLLOWER_PIDS[-1]})"
    else
        log_info "dmesg_restrict=$restrict — skipping dmesg follower (journalctl -k covers it)"
    fi

    # 4. tail -F sovyx.log se arquivo existe
    local sovyx_log="$HOME/.sovyx/logs/sovyx.log"
    if [[ -r "$sovyx_log" ]]; then
        tail -n 0 -F "$sovyx_log" \
            > "$diag_dir/sovyx_log_follow.txt" 2>&1 &
        SOVYX_DIAG_FOLLOWER_PIDS+=($!)
        log_info "follower: tail -F sovyx.log (pid ${SOVYX_DIAG_FOLLOWER_PIDS[-1]})"
    fi
}

# Mata todos os followers graciosamente.
stop_followers() {
    local pid
    for pid in "${SOVYX_DIAG_FOLLOWER_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    # Grace period
    sleep 1
    for pid in "${SOVYX_DIAG_FOLLOWER_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
    SOVYX_DIAG_FOLLOWER_PIDS=()
}

# ─────────────────────────────────────────────────────────────────────────
# Cleanup / trap EXIT
# ─────────────────────────────────────────────────────────────────────────

_cleanup_ran=0
# V4 Track D finding: SIGTERM during an idle moment leaves $?=0 (last cmd
# succeeded just before the signal). Trap saw "exit 0" and labeled the
# run "complete" even though the user killed it. Fix: a global sentinel
# set only at the very end of a normal run. If trap fires while sentinel
# is 0, force a non-zero exit code so status becomes "partial".
SOVYX_DIAG_RUN_COMPLETED=0
_cleanup() {
    # V4 Track B smoke finding: capture $? FIRST. The prior version had
    # the `_cleanup_ran=1` assignment before `local exit_code=$?`, which
    # reset $? to 0 via the assignment. Result: every run reported exit
    # code 0 even when the script had `exit 3`'d from a hard failure
    # (e.g., selftest abort). Label was wrong; SUMMARY.json said
    # status=complete on a broken run. MUST be the literal first line.
    local exit_code=$?
    [[ "$_cleanup_ran" = "1" ]] && return 0
    _cleanup_ran=1

    # V4 Track D fix: if the script never reached its normal completion
    # sentinel, force partial status even if $? happens to be 0 (e.g.
    # SIGTERM arrived mid-sleep, last cmd was a successful no-op).
    if [[ "$SOVYX_DIAG_RUN_COMPLETED" != "1" && $exit_code -eq 0 ]]; then
        log_warn "cleanup invoked before run completion sentinel; marking partial"
        exit_code=130   # POSIX convention: 128 + SIGINT (interruption-like)
    fi

    log_info "cleanup starting (exit code: $exit_code)"

    stop_followers

    # Restaura áudio se intrusive-restart foi usado e não foi restaurado.
    if [[ "$SOVYX_DIAG_AUDIO_RESTART_PENDING" = "1" ]]; then
        log_warn "restoring pipewire/wireplumber after intrusive restart"
        systemctl --user start pipewire pipewire-pulse wireplumber 2>/dev/null || true
        SOVYX_DIAG_AUDIO_RESTART_PENDING=0
    fi

    # Restaura estado inicial do Sovyx.
    if [[ -n "$SOVYX_DIAG_OUTDIR" ]]; then
        case "$SOVYX_DIAG_INITIAL_SOVYX_RUNNING" in
            yes)
                if ! sovyx_is_running; then
                    log_info "restoring: sovyx start"
                    sovyx start >/dev/null 2>&1 &
                fi
                if [[ "$SOVYX_DIAG_INITIAL_VOICE_ENABLED" = "yes" && -n "$SOVYX_DIAG_TOKEN" ]]; then
                    # Aguarda daemon subir, depois re-enable voice.
                    ( sleep 10 && \
                      curl -sS --max-time 30 \
                        -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
                        -X POST "http://127.0.0.1:7777/api/voice/enable" \
                        -H 'Content-Type: application/json' \
                        -d '{}' >/dev/null 2>&1 || true ) &
                fi
                ;;
            no)
                if sovyx_is_running; then
                    log_info "restoring: sovyx stop"
                    sovyx stop >/dev/null 2>&1 || true
                fi
                ;;
            *)
                log_warn "initial sovyx state unknown — not restoring"
                ;;
        esac

        # Gera checksums, manifest consolidado e tarball — mesmo em saída parcial.
        # Os scripts de T11 fazem isso; aqui só garantimos que a função existe.
        if declare -F finalize_package >/dev/null 2>&1; then
            local partial_suffix=""
            [[ "$exit_code" -ne 0 ]] && partial_suffix="_PARTIAL"
            finalize_package "$partial_suffix" "$exit_code" || true
        fi
    fi

    log_info "cleanup done (exit code: $exit_code)"
    exit "$exit_code"
}

# Instala o trap. Chamado uma vez pelo main após _init_common.
install_trap() {
    trap _cleanup EXIT INT TERM HUP
}

# ─────────────────────────────────────────────────────────────────────────
# Inicialização
# ─────────────────────────────────────────────────────────────────────────

# AUDIT v3 — hard-dep assertions. The toolkit has several transitive
# Python + GNU-date dependencies that, when absent, produce silent
# data corruption (see common.sh:now_monotonic_ns docstring). Assert
# once, aloud, at init so the operator gets a clear error instead of
# mysteriously-empty artifacts.
_assert_audit_preconditions() {
    # python3 is required by: now_monotonic_ns, now_pair_json,
    # alert_append, timeline_append, redact_stream, _finalize_summary,
    # analyze_wav.py, silero_probe.py, sd_capture.py. Without it, every
    # path degrades silently. Abort the whole run.
    if ! command -v python3 >/dev/null 2>&1; then
        printf '[ERROR] python3 not found on PATH — forensic toolkit requires python3\n' >&2
        printf '[ERROR] install python3 and re-run; refusing to continue with silent clock failures\n' >&2
        return 1
    fi

    # GNU date is required for `date +%N` nanosecond output. BusyBox
    # and macOS `date` emit literal `%N`, corrupting every timestamp
    # header silently. Probe once.
    local probe
    probe=$(date -u +%N 2>/dev/null || echo "")
    if [[ ! "$probe" =~ ^[0-9]+$ ]]; then
        printf '[ERROR] date(1) does not support %%N (GNU date required); got %q\n' "$probe" >&2
        printf '[ERROR] non-GNU date corrupts all timestamp headers — refusing to continue\n' >&2
        return 1
    fi

    # Validate that now_pair_json actually produces valid JSON with
    # both fields populated — catches any environment where python3
    # is present but broken (e.g. stripped-down container images).
    local pair
    pair=$(now_pair_json 2>/dev/null) || {
        printf '[ERROR] now_pair_json failed; python3 time module unavailable\n' >&2
        return 1
    }
    if ! printf '%s' "$pair" | python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
assert "utc_iso_ns" in d and "monotonic_ns" in d, "missing keys"
assert isinstance(d["monotonic_ns"], int), "monotonic_ns not int"
' 2>/dev/null; then
        printf '[ERROR] now_pair_json produced invalid JSON: %q\n' "$pair" >&2
        return 1
    fi

    return 0
}

# _init_common <outdir> — cria árvore, inicia logs, resolve paths.
_init_common() {
    # AUDIT v3 — run hard-dep assertion FIRST, before any time or
    # filesystem operations. If the environment is inadequate, fail
    # loudly at step 0, not silently at step N.
    if ! _assert_audit_preconditions; then
        return 1
    fi

    SOVYX_DIAG_OUTDIR="$1"
    mkdir -p \
        "$SOVYX_DIAG_OUTDIR"/{initial_evidence,states,_diagnostics,A_hardware,B_kernel,C_alsa/captures,D_pipewire/captures,D_pipewire/configs,E_portaudio/captures,F_session,G_sovyx,H_pipeline_live,I_network,J_latent,K_output} \
        "$SOVYX_DIAG_OUTDIR/states/_diffs" \
        "$SOVYX_DIAG_OUTDIR/_diagnostics/manifest.d"

    SOVYX_DIAG_RUNLOG="$SOVYX_DIAG_OUTDIR/RUNLOG.txt"
    SOVYX_DIAG_TIMELINE="$SOVYX_DIAG_OUTDIR/_diagnostics/timeline.csv"
    SOVYX_DIAG_MANIFEST_DIR="$SOVYX_DIAG_OUTDIR/_diagnostics/manifest.d"
    SOVYX_DIAG_ENV_MATRIX="$SOVYX_DIAG_OUTDIR/_diagnostics/environment_matrix.md"
    SOVYX_DIAG_SUMMARY_JSON="$SOVYX_DIAG_OUTDIR/SUMMARY.json"

    SOVYX_DIAG_START_UTC_NS=$(now_utc_ns)
    SOVYX_DIAG_START_MONO_NS=$(now_monotonic_ns)

    # Init RUNLOG.
    {
        echo "# sovyx-voice-diag RUNLOG"
        echo "# version: $SOVYX_DIAG_VERSION"
        echo "# started_utc: $SOVYX_DIAG_START_UTC_NS"
        echo "# monotonic_ns_start: $SOVYX_DIAG_START_MONO_NS"
        echo "# hostname: $(hostname 2>/dev/null || echo unknown)"
        echo "# user: $(id -un 2>/dev/null || echo unknown)"
        echo "# ---"
    } > "$SOVYX_DIAG_RUNLOG"

    # Init timeline.csv.
    _init_timeline

    # Init environment_matrix (TSV: status\tcmd\tversion).
    {
        echo "# environment_matrix — presença de ferramentas externas"
        echo "# format: status<TAB>cmd<TAB>version_line"
        echo "# generated: $SOVYX_DIAG_START_UTC_NS"
        echo "# ---"
    } > "$SOVYX_DIAG_ENV_MATRIX"

    # Init alerts.
    : > "$SOVYX_DIAG_OUTDIR/_diagnostics/alerts.jsonl"

    log_info "outdir initialized: $SOVYX_DIAG_OUTDIR"
}

# ─────────────────────────────────────────────────────────────────────────
# T2 — Per-capture Sovyx context snapshotter (AUDIT v3 enhancement).
# ─────────────────────────────────────────────────────────────────────────

# _snapshot_sovyx_context <capture_id> <start_monotonic_ns> <end_monotonic_ns> <output_base_dir>
#
# Captura 3 evidências sincronizadas com o momento EXATO de uma captura
# de áudio (W10-W13, H rounds). Fecha o gap "o que o Sovyx estava vendo
# enquanto eu falava?" — sem isso, a análise depende de correlação
# pós-hoc ambígua entre timeline.csv e log slices.
#
# Produz em <output_base_dir>/sovyx_context/:
#   1. voice_status_during_<cid>.json          — GET /api/voice/status
#   2. capture_diagnostics_during_<cid>.json   — GET /api/voice/capture-diagnostics
#   3. sovyx_log_slice_during_<cid>.txt        — linhas do sovyx.log dentro
#                                                 da janela [start-1s, end+2s]
#   4. context_meta.json                       — timestamps + pointers
#
# Sem efeitos colaterais (read-only). Seguro para invocar em qualquer
# estado onde o daemon Sovyx esteja responsivo.
_snapshot_sovyx_context() {
    local cid="$1" start_mono="$2" end_mono="$3" base_dir="$4"

    local ctx_dir="$base_dir/sovyx_context"
    mkdir -p "$ctx_dir"

    local ctx_meta="$ctx_dir/context_meta.json"
    local status_out="$ctx_dir/voice_status_during_${cid}.json"
    local diag_out="$ctx_dir/capture_diagnostics_during_${cid}.json"
    local log_out="$ctx_dir/sovyx_log_slice_during_${cid}.txt"

    local snap_utc snap_mono
    snap_utc=$(now_utc_ns)
    # V4 Track H: if monotonic clock fails, the entire context snapshot is
    # invalid (all time math uses snap_mono as anchor). Refuse to fabricate.
    snap_mono=$(now_monotonic_ns)
    if [[ -z "$snap_mono" ]] || ! [[ "$snap_mono" =~ ^[0-9]+$ ]]; then
        log_error "_snapshot_sovyx_context: monotonic clock failed for cid=$cid; context will be marked invalid"
        snap_mono=""  # explicit empty; context_meta will record clock_failed=true
    fi

    # 1. voice/status — instant snapshot do pipeline.
    # V4 Track H: capture curl rc explicitly so "empty response" is
    # distinguishable from "curl failed with rc=X".
    local curl_rc_status curl_rc_diag
    if [[ -n "${SOVYX_DIAG_TOKEN:-}" ]]; then
        curl -sS --max-time 5 \
             -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
             "http://127.0.0.1:7777/api/voice/status" \
             > "$status_out" 2>"${status_out}.err"
        curl_rc_status=$?
        if [[ $curl_rc_status -ne 0 ]] || [[ ! -s "$status_out" ]]; then
            printf '{"error":"curl_failed_or_empty","curl_rc":%d,"stderr_path":"%s"}\n' \
                "$curl_rc_status" "$(basename "$status_out").err" > "$status_out"
        fi

        # 2. capture-diagnostics — APO chain + capture endpoints.
        curl -sS --max-time 8 \
             -H "Authorization: Bearer $SOVYX_DIAG_TOKEN" \
             "http://127.0.0.1:7777/api/voice/capture-diagnostics" \
             > "$diag_out" 2>"${diag_out}.err"
        curl_rc_diag=$?
        if [[ $curl_rc_diag -ne 0 ]] || [[ ! -s "$diag_out" ]]; then
            printf '{"error":"curl_failed_or_empty","curl_rc":%d,"stderr_path":"%s"}\n' \
                "$curl_rc_diag" "$(basename "$diag_out").err" > "$diag_out"
        fi
    else
        curl_rc_status=-1
        curl_rc_diag=-1
        echo '{"error":"no_token"}' > "$status_out"
        echo '{"error":"no_token"}' > "$diag_out"
    fi

    # 3. sovyx.log slice in window [start_mono-1s, end_mono+2s].
    local sovyx_log="$HOME/.sovyx/logs/sovyx.log"
    if [[ -r "$sovyx_log" ]] && [[ -n "$SOVYX_DIAG_PYTHON" ]]; then
        # Convert monotonic window bounds to UTC epoch SECONDS (what
        # sovyx.log records use). The bound is broad enough (-1s..+2s)
        # to catch events in the capture window.
        local win_start_s win_end_s
        win_start_s=$(awk -v m="$start_mono" -v snap_m="$snap_mono" -v snap_s="$snap_utc" '
            BEGIN {
                # Convert monotonic ns delta from snap into wall-clock offset.
                # snap_utc is ISO string — strip to get epoch via python would
                # be cleaner. Use a simple offset in seconds based on
                # monotonic_ns delta. Upstream consumer only needs rough
                # window bounds.
                gap_s = (snap_m - m) / 1e9
                # epoch snapshot is approximated via current wall (passed in)
                # — we ask python below to do the precise conversion.
                print gap_s
            }')
        # Defer precise window extraction to python for reliability.
        "$SOVYX_DIAG_PYTHON" - "$sovyx_log" "$start_mono" "$end_mono" "$snap_mono" \
            > "$log_out" 2>"${log_out%.txt}.err" <<'PYEOF' || true
import json, re, sys, time
import datetime as _dt

path = sys.argv[1]
start_mono = int(sys.argv[2])
end_mono = int(sys.argv[3])
snap_mono = int(sys.argv[4])

# Use snap_mono as the reference point to convert monotonic_ns deltas
# into wall-clock epoch. snap_mono was captured at approximately now =
# time.time(); so delta_mono_ns / 1e9 gives wall-clock delta.
now = time.time()
def mono_to_wall(m):
    delta_s = (snap_mono - m) / 1e9
    return now - delta_s

win_start_wall = mono_to_wall(start_mono) - 1.0
win_end_wall = mono_to_wall(end_mono) + 2.0

print(f"# context_extractor_status: started")
print(f"# window_start_epoch: {win_start_wall:.3f}")
print(f"# window_end_epoch: {win_end_wall:.3f}")
print(f"# source: {path}")

matched = parsed = 0
try:
    with open(path, errors="replace") as f:
        for line in f:
            parsed += 1
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = rec.get("timestamp") or rec.get("ts")
            if ts is None:
                continue
            try:
                ts_epoch = float(ts)
            except (TypeError, ValueError):
                try:
                    s = str(ts).replace("Z", "+00:00")
                    dt = _dt.datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_dt.timezone.utc)
                    ts_epoch = dt.timestamp()
                except Exception:
                    continue
            if win_start_wall <= ts_epoch <= win_end_wall:
                sys.stdout.write(line)
                matched += 1
except Exception as exc:
    sys.stderr.write(f"extractor_error: {type(exc).__name__}: {exc}\n")
    sys.exit(1)

print(f"# context_extractor_status: complete parsed={parsed} matched={matched}")
PYEOF
    else
        {
            echo "# context_extractor_status: skipped"
            echo "# reason: $([[ -r $sovyx_log ]] && echo 'no_python' || echo 'sovyx_log_unreadable')"
        } > "$log_out"
    fi

    # 4. context_meta — timestamps + pointers for the cross-correlation index.
    # V4 Track H: emit valid JSON even when snap_mono is empty (clock failed).
    # Use `null` for the JSON value in that case — downstream consumers can
    # distinguish "clock invalid" from "clock = 0" (which would otherwise
    # collapse into valid-looking but meaningless value).
    local snap_mono_json="${snap_mono:-null}"
    local clock_valid="true"
    [[ -z "$snap_mono" ]] && clock_valid="false"
    {
        printf '{\n'
        printf '  "capture_id": "%s",\n' "$cid"
        printf '  "start_monotonic_ns": %s,\n' "$start_mono"
        printf '  "end_monotonic_ns": %s,\n' "$end_mono"
        printf '  "snapshotted_at_utc_ns": "%s",\n' "$snap_utc"
        printf '  "snapshotted_at_monotonic_ns": %s,\n' "$snap_mono_json"
        printf '  "clock_valid": %s,\n' "$clock_valid"
        printf '  "curl_rc_voice_status": %s,\n' "${curl_rc_status:-null}"
        printf '  "curl_rc_capture_diagnostics": %s,\n' "${curl_rc_diag:-null}"
        printf '  "artifacts": {\n'
        printf '    "voice_status": "sovyx_context/%s",\n' "$(basename "$status_out")"
        printf '    "capture_diagnostics": "sovyx_context/%s",\n' "$(basename "$diag_out")"
        printf '    "sovyx_log_slice": "sovyx_context/%s"\n' "$(basename "$log_out")"
        printf '  }\n'
        printf '}\n'
    } > "$ctx_meta"
}
