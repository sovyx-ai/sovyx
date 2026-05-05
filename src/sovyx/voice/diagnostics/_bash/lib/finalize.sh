#!/usr/bin/env bash
# lib/finalize.sh — pós-processamento: MANIFEST consolidado, CHECKSUMS,
# SUMMARY final, tarball reprodutível.
#
# Chamado pelo trap EXIT de common.sh (via finalize_package). Idempotente:
# pode ser chamado múltiplas vezes sem corromper saída.

_assemble_manifest() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local manifest="$outdir/MANIFEST.md"
    local frag_dir="$SOVYX_DIAG_MANIFEST_DIR"

    # Lê step_ids na ordem do timeline.csv (ordem real de execução).
    # Dedupe preservando ordem via awk; strip aspas CSV.
    local ordered_steps
    ordered_steps=$(
        tail -n +2 "$SOVYX_DIAG_TIMELINE" 2>/dev/null \
          | awk -F',' '{gsub(/^"|"$/, "", $1); print $1}' \
          | awk '!seen[$0]++'
    )

    {
        printf '# sovyx-voice-diag — MANIFEST\n\n'
        printf '**Gerado:** %s  \n' "$(now_utc_ns)"
        printf '**Script:** v%s  \n' "$SOVYX_DIAG_VERSION"
        printf '**Host:** %s  \n' "$(hostname 2>/dev/null || echo unknown)"
        printf '**User:** %s  \n' "$(id -un 2>/dev/null || echo unknown)"
        printf '**Outdir:** `%s`\n\n' "$outdir"

        printf '## Contadores de execução\n\n'
        printf -- '- Steps totais: %s\n' "$SOVYX_DIAG_STEPS_TOTAL"
        printf -- '- OK: %s\n' "$SOVYX_DIAG_STEPS_OK"
        printf -- '- WARN: %s\n' "$SOVYX_DIAG_STEPS_WARN"
        printf -- '- FAIL: %s\n' "$SOVYX_DIAG_STEPS_FAIL"
        printf -- '- TIMEOUT: %s\n\n' "$SOVYX_DIAG_STEPS_TIMEOUT"

        printf '## == ALERTAS == (preenchidos por T12)\n\n'
        if [[ -s "$outdir/_diagnostics/alerts.jsonl" ]]; then
            python3 - "$outdir/_diagnostics/alerts.jsonl" <<'PYEOF' || echo '- (failed to render alerts)'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
by_sev = {"error": [], "warn": [], "info": []}
for line in path.read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        a = json.loads(line)
    except Exception:
        continue
    sev = a.get("severity", "info")
    by_sev.setdefault(sev, []).append(a)
for sev in ("error", "warn", "info"):
    items = by_sev.get(sev, [])
    if not items:
        continue
    emoji = {"error": "\U0001F534", "warn": "\U0001F7E1", "info": "\U0001F535"}.get(sev, "-")
    print(f"### {emoji} {sev.upper()}")
    for a in items:
        print(f"- [{a.get('state','?')}] {a.get('message','(no message)')}")
    print()
PYEOF
        else
            printf -- '- (nenhum alerta gerado)\n\n'
        fi

        printf '## Leitura sugerida (entry points)\n\n'
        cat <<'GUIDE'
1. **`SUMMARY.json`** — contadores, estado inicial Sovyx, drift de clock.
2. **`RUNLOG.txt`** — cada comando + retcode + duração.
3. **`_diagnostics/timeline.csv`** — mesma info ordenada por tempo (jq/pandas).
4. **`_diagnostics/environment_matrix.md`** — quais ferramentas existem.
5. **`_diagnostics/alerts.jsonl`** — alertas auto-gerados (§9 do plano).
6. **`states/_diffs/summary.md`** — achados automáticos de vazamento inter-estado.
7. **`C_alsa/captures/W{1,2,3,4,4b,14}/analysis.json`** — verdade nua do mic (sem PipeWire).
8. **`D_pipewire/captures/W{5,6,7,8,9,14b}/analysis.json`** — through PipeWire.
9. **`E_portaudio/captures/W{10,11,12,13,14c}/analysis.json`** — caminho do Sovyx.
10. **`G_sovyx/api_voice_capture_diagnostics.json`** — veredito interno.
11. **`H_pipeline_live/live_pipeline_log_slice_round*.txt`** — log do Sovyx durante fala.
12. **`K_output/playback_results.json`** + `K_kokoro_analysis.json` — cadeia de saída.

Matriz de inferência (§3 do plano v2) — lido após a tabela:
- W1–W4 com `max_prob > 0.5` + rolloff > 3 kHz → hardware vivo.
- W5–W9 baixos enquanto W1–W4 vivos → raiz PipeWire/filtro.
- W10–W13 baixos enquanto W5–W9 vivos → raiz PortAudio.
- W10–W13 vivos mas Sovyx produtivo morto (H) → raiz capture_task Sovyx.
- Rolloff ≤ 500 Hz em TODAS → raiz hardware/codec/kernel.
- Playback K1–K4 inaudível com RMS > -30 dB → raiz sink/volume/mute.
- Kokoro K_kokoro_analysis.json RMS=0 → raiz TTS runtime.

GUIDE

        printf '## Artefatos (por step_id na ordem de execução)\n\n'
        # Consolida fragmentos em ordem de timeline. Se faltar fragmento para
        # um step_id, ainda assim listamos o arquivo do timeline.
        if [[ -d "$frag_dir" ]]; then
            local fragments_listed=0
            while IFS= read -r step_id; do
                [[ -z "$step_id" ]] && continue
                local frag="$frag_dir/${step_id}.md"
                if [[ -r "$frag" ]]; then
                    cat "$frag"
                    printf '\n'
                    fragments_listed=$((fragments_listed + 1))
                fi
            done <<<"$ordered_steps"
            # Fragmentos não referenciados pelo timeline (ex.: camadas inteiras).
            local all_frags
            all_frags=$(find "$frag_dir" -maxdepth 1 -name '*.md' 2>/dev/null | sort)
            while IFS= read -r frag; do
                [[ -z "$frag" ]] && continue
                local fname
                fname=$(basename "$frag" .md)
                # Se já foi processado acima (por timeline), pula.
                if ! grep -qE "^\- \*\*${fname}\*\*" "$manifest.part" 2>/dev/null; then
                    cat "$frag"
                    printf '\n'
                fi
            done <<<"$all_frags" >> "$manifest.part" 2>/dev/null
        fi

        printf '## Outros artefatos (sem anotação dedicada)\n\n'
        printf '_Arquivos gerados pelo script que não têm fragment de MANIFEST dedicado. '
        printf 'Inclui logs de follower, CHECKSUMS, SUMMARY, environment_matrix, etc._\n\n'
        # Lista arquivos sob outdir que não são o próprio MANIFEST/tarball,
        # agrupados por diretório.
        python3 - "$outdir" "$frag_dir" <<'PYEOF' 2>/dev/null || printf -- '- (failed to enumerate files)\n'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
frag_dir = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None
# Files already referenced in fragments (by path component match).
referenced = set()
if frag_dir and frag_dir.exists():
    for frag in frag_dir.iterdir():
        try:
            text = frag.read_text()
        except Exception:
            continue
        for line in text.splitlines():
            # Extract backtick-quoted paths.
            import re
            for m in re.findall(r"`([^`]+)`", line):
                referenced.add(m.strip("/"))
# Enumerate files (excluding MANIFEST/CHECKSUMS/tarball/manifest.d).
excluded = {"MANIFEST.md", "CHECKSUMS.sha256"}
buckets: dict[str, list[str]] = {}
for p in sorted(root.rglob("*")):
    if not p.is_file():
        continue
    rel = p.relative_to(root).as_posix()
    if rel in excluded or rel.startswith("_diagnostics/manifest.d/"):
        continue
    if rel.endswith(".tar.gz"):
        continue
    # Skip if referenced by any fragment (prefix or exact match).
    if any(rel == r or rel.startswith(r.rstrip("/") + "/") for r in referenced):
        continue
    top = rel.split("/", 1)[0] if "/" in rel else "."
    buckets.setdefault(top, []).append(rel)
for top in sorted(buckets):
    print(f"### `{top}/`")
    for rel in sorted(buckets[top])[:30]:
        print(f"- `{rel}`")
    if len(buckets[top]) > 30:
        print(f"- ... e mais {len(buckets[top]) - 30} arquivos em `{top}/`")
    print()
PYEOF

        printf '## Fora de escopo\n\n'
        cat <<'OOS'
- Upgrades de pacote (`apt install`, `pipx upgrade sovyx`).
- Reinstalação de drivers.
- Edição de `~/.sovyx/config.yaml` / dotfiles.
- Reset de estado.
- Qualquer inferência/veredito dentro do script — coleta-se, conclui-se na Fase 3.

Lacunas declaradas (não fechadas por design): L1 (suspend/resume — opt-in),
L2 (reinício PipeWire — opt-in), L3 (instrumentação dentro do processo),
L4 (regressão 0.21.1 → 0.21.2 — script documenta, não faz upgrade).
OOS

    } > "$manifest"
    rm -f "$manifest.part"
}

_generate_checksums() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local checksums="$outdir/CHECKSUMS.sha256"
    local errors="$outdir/_diagnostics/checksums_errors.txt"
    log_info "generating granular checksums..."
    # AUDIT v3 — THREE fixes on the hot path:
    #
    # (1) ``xargs -r`` ensures that on an empty outdir (zero matching
    #     files), xargs does NOT run sha256sum with no args (which
    #     would block on stdin forever). Previous version could hang
    #     a forensic run indefinitely.
    #
    # (2) Capture sha256sum stderr to a dedicated error file so
    #     "permission denied" on a single file surfaces as a
    #     forensic artifact instead of being swallowed by
    #     ``2>/dev/null``. An unreadable file dropping from CHECKSUMS
    #     without a note is an integrity hole.
    #
    # (3) Write to a ``.tmp`` and os-atomic-rename so a crashed run
    #     can't leave a half-written CHECKSUMS.
    (
        cd "$outdir" || return 1
        find . -type f \
            ! -name 'CHECKSUMS.sha256' \
            ! -name 'CHECKSUMS.sha256.tmp' \
            ! -name '*.tar.gz' \
            -print0 \
            | sort -z \
            | xargs -0 -r sha256sum 2>"$errors" \
            > "$checksums.tmp"
    )
    if [[ -s "$checksums.tmp" ]]; then
        mv -f "$checksums.tmp" "$checksums"
    else
        rm -f "$checksums.tmp"
        log_warn "checksums file empty — outdir had no files to hash"
        : > "$checksums"
    fi
    if [[ -s "$errors" ]]; then
        log_warn "sha256sum emitted errors on $(wc -l <"$errors") file(s); see checksums_errors.txt"
    fi
}

_finalize_summary() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local summary="$SOVYX_DIAG_SUMMARY_JSON"
    local status="${1:-complete}"
    local exit_code="${2:-0}"

    local end_utc end_mono duration_ms
    end_utc=$(now_utc_ns)
    end_mono=$(now_monotonic_ns) || {
        log_error "_finalize_summary: monotonic clock unavailable — SUMMARY.json may be stuck at initial state"
        return 1
    }
    duration_ms=$(( (end_mono - SOVYX_DIAG_START_MONO_NS) / 1000000 ))

    # AUDIT v3+ T6 — self-describing tarball enrichment.
    # Compute script sha256 so an external auditor can verify version.
    local script_sha=""
    if command -v sha256sum >/dev/null 2>&1; then
        script_sha=$(sha256sum "$SOVYX_DIAG_SCRIPT_DIR/sovyx-voice-diag.sh" 2>/dev/null | awk '{print $1}')
    fi
    # V4 Track B fix: fall back to system python3 if SOVYX_DIAG_PYTHON empty.
    local summary_py="${SOVYX_DIAG_PYTHON:-$(command -v python3 || true)}"
    local selftest_status="unknown"
    if [[ -r "$outdir/_diagnostics/analyzer_selftest.json" && -n "$summary_py" ]]; then
        selftest_status=$("$summary_py" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get("status", "unknown"))
except Exception:
    print("unreadable")
' "$outdir/_diagnostics/analyzer_selftest.json" 2>/dev/null || echo "unreadable")
    fi
    local guardian_status="unknown"
    if [[ -r "$outdir/_diagnostics/guardian/guardian_status.json" && -n "$summary_py" ]]; then
        guardian_status=$("$summary_py" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get("status", "unknown"))
except Exception:
    print("unreadable")
' "$outdir/_diagnostics/guardian/guardian_status.json" 2>/dev/null || echo "unreadable")
    fi
    local op_prompts_status="unknown"
    if [[ -r "$outdir/_diagnostics/operator_responses.json" && -n "$summary_py" ]]; then
        op_prompts_status=$("$summary_py" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(f"{d.get(\"status\", \"unknown\")}:responded={d.get(\"responded_count\", 0)}/{d.get(\"catalog_prompt_count\", 0)}")
except Exception:
    print("unreadable")
' "$outdir/_diagnostics/operator_responses.json" 2>/dev/null || echo "unreadable")
    fi

    # AUDIT v3 — CRITICAL fixes:
    #
    # (1) Previous version used ``|| true`` to swallow Python errors.
    #     On a broken python3 or corrupt initial SUMMARY.json, the
    #     final write never happened and SUMMARY.json stayed at
    #     ``"status": "running"`` even after a complete run. The
    #     tarball was labeled COMPLETE but SUMMARY said RUNNING —
    #     forensic-critical inconsistency. Now: detect python failure
    #     and log a loud error; ALSO write a shell-side fallback
    #     SUMMARY so the operator has SOME signal.
    #
    # (2) Write is now atomic: python writes to ``SUMMARY.json.tmp``
    #     and returns 0 only after a clean ``os.replace`` into place.
    #     A crashed python leaves only the ``.tmp`` file.
    python3 - "$summary" "$status" "$exit_code" "$end_utc" "$end_mono" "$duration_ms" \
            "$SOVYX_DIAG_STEPS_TOTAL" "$SOVYX_DIAG_STEPS_OK" "$SOVYX_DIAG_STEPS_WARN" \
            "$SOVYX_DIAG_STEPS_FAIL" "$SOVYX_DIAG_STEPS_TIMEOUT" \
            "$script_sha" "$selftest_status" "$guardian_status" "$op_prompts_status" \
            "$SOVYX_DIAG_VERSION" <<'PYEOF'
import json, os, pathlib, sys, tempfile

(p, status, rc, end_utc, end_mono, dur,
 total, ok, warn, fail, tout,
 script_sha, selftest_status, guardian_status, op_prompts_status,
 script_version) = sys.argv[1:]
path = pathlib.Path(p)
try:
    data = json.loads(path.read_text())
except Exception:
    data = {}

data["status"] = status
data["final_exit_code"] = int(rc)
data["ended_utc_ns"] = end_utc
data["ended_monotonic_ns"] = int(end_mono)
data["total_duration_ms"] = int(dur)
data["steps"] = {
    "total": int(total),
    "ok": int(ok),
    "warn": int(warn),
    "fail": int(fail),
    "timeout": int(tout),
}

# AUDIT v3+ T6 — self-description fields. An external auditor receiving
# ONLY the tarball should be able to understand the script version,
# calibration state, and coverage extent WITHOUT access to the source.
data["audit_version"] = "v3+"
data["script_version"] = script_version
data["script_sha256"] = script_sha or "unavailable"
data["calibration"] = {
    "analyzer_selftest_status": selftest_status,
    "guardian_status": guardian_status,
    "operator_prompts_status": op_prompts_status,
}
data["hypothesis_list"] = sorted({
    "A1", "A2", "A3", "A4", "A5", "A6",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7",
    "C1", "C2", "C3", "C4", "C5", "C6",
    "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9",
    "E_P1", "E_P2", "E_P3", "E_P4", "E_P5",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
    "G1", "G2", "G3", "G4", "G5", "G6", "G7",
    "H1", "H2",
    "I1", "I2", "I3",
    "K1", "K2", "K3", "K4",
})
data["blind_spots_known"] = [
    # Structural — cannot be closed by this script alone.
    "kernel_regression_baseline_requires_reference_machine",
    # Contingent — closed when guardian is active (T1); open if --skip-guardian.
    "suspend_resume_if_guardian_disabled_and_no_inotifywait",
    # Temporal — events before guardian started (boot-time races) are not
    # captured by guardian; only visible in journalctl `--since boot` which
    # we do NOT run by default (too noisy). H_pipeline_live covers live
    # races; boot races pre-script require --since-boot journal dump.
    "boot_time_race_before_guardian_active",
    # Coverage-contingent — operator SKIP reduces perceptual evidence.
    "operator_skipped_p1_prompt_reduces_perceptual_coverage",
    # Selftest scope — calibration uses 440 Hz tone; regressions that only
    # manifest on voice content (DSP nonlinearity, AGC dynamics) wouldn't
    # be caught by the calibration tone alone.
    "selftest_calibrates_linearity_not_voice_content_specific_dsp",
]
# NOTE: the following former blind spots are NOW CLOSED in v3+:
# - kernel_mid_run_crash        → closed by T1 dmesg_watch + journal_watch
# - usb_hub_glitch              → closed by T1 udev_watch (subsystem=usb)
# - bluetooth_profile_switch    → closed by T1 udev_watch (subsystem=bluetooth)
# - bios_audio_feature_flags    → closed by T3 prompt bios_mic_flag (operator)
# - pipewire_lua_custom_policy  → closed by T3 prompt pipewire_custom_configs
data["analysis_playbook_ref"] = "AUDIT-V3-FINDINGS.md"
data["analyst_navigation"] = {
    "primary_entry": "VERDICT_CHECKLIST.md",
    "correlation_index": "_diagnostics/cross_correlation.json",
    "manifest": "MANIFEST.md",
    "alerts": "_diagnostics/alerts.jsonl",
    "timeline": "_diagnostics/timeline.csv",
    "operator_responses": "_diagnostics/operator_responses.json",
    "guardian_followers": "_diagnostics/guardian/",
}

# V4 Track G finding: without an at-a-glance host_capability_summary,
# an auditor reading the tarball can't distinguish "test discovered a
# real bug" from "host lacks the tool needed to run the test". Derive
# it from environment_matrix + uname + sovyx binary probe so the
# first-glance answer is in SUMMARY.json itself.
import pathlib as _pl
_env_matrix = _pl.Path(p).parent / "_diagnostics" / "environment_matrix.md"
_uname_file = _pl.Path(p).parent / "B_kernel" / "uname.txt"
host_cap = {"kernel_line": None, "tools_present": [], "tools_absent": [],
            "audio_stack_present": False, "sovyx_cli_present": False,
            "likely_wsl": False}
try:
    if _env_matrix.exists():
        for line in _env_matrix.read_text(errors="replace").splitlines():
            if "\t" not in line or line.startswith("#"):
                continue
            parts = line.split("\t")
            status, tool = parts[0], parts[1]
            if status == "present":
                host_cap["tools_present"].append(tool)
            elif status == "absent":
                host_cap["tools_absent"].append(tool)
    # Dedupe (same tool may be probed by main + multiple layers).
    host_cap["tools_present"] = sorted(set(host_cap["tools_present"]))
    host_cap["tools_absent"] = sorted(set(host_cap["tools_absent"]))
    audio_tools = {"arecord", "aplay", "pactl", "pw-dump", "wpctl"}
    host_cap["audio_stack_present"] = bool(
        audio_tools.intersection(set(host_cap["tools_present"]))
    )
    host_cap["sovyx_cli_present"] = "sovyx" in host_cap["tools_present"] or \
        any(t.startswith("sovyx") for t in host_cap["tools_present"])
    if _uname_file.exists():
        for raw_line in _uname_file.read_text(errors="replace").splitlines():
            if raw_line.startswith("#") or not raw_line.strip():
                continue
            host_cap["kernel_line"] = raw_line.strip()
            host_cap["likely_wsl"] = "microsoft" in raw_line.lower() or \
                "wsl" in raw_line.lower()
            break
except Exception:
    # Host capability derivation is best-effort — never fatal for the run.
    pass
data["host_capability_summary"] = host_cap

# Atomic write: tempfile + os.replace so a crash mid-write never
# produces a truncated SUMMARY.json.
path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(
    mode="w",
    delete=False,
    dir=str(path.parent),
    prefix=f".{path.name}.",
    suffix=".tmp",
    encoding="utf-8",
) as tmp:
    tmp.write(json.dumps(data, indent=2))
    tmp.flush()
    os.fsync(tmp.fileno())
    tmp_path = tmp.name
os.replace(tmp_path, str(path))
PYEOF
    local py_rc=$?
    if [[ $py_rc -ne 0 ]]; then
        log_error "_finalize_summary: python3 write failed rc=$py_rc — SUMMARY.json may be stale"
        # Minimal shell-side fallback so the operator at least has
        # some record of final state.
        {
            printf '{"status": "%s", "final_exit_code": %s, "note": "fallback_written_by_bash_due_to_python_failure"}\n' \
                "$status" "$exit_code"
        } > "$summary.fallback.json" 2>/dev/null || true
        return 1
    fi
    return 0
}

_build_tarball() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local suffix="${1:-}"
    local parent base
    parent=$(dirname "$outdir")
    base=$(basename "$outdir")
    local tarball="$parent/${base}${suffix}.tar.gz"

    log_info "building tarball: $tarball"
    # AUDIT v3 — capture tar's rc DIRECTLY (not through a pipe, which
    # masks the exit code depending on pipefail setting). Previous
    # version used ``tar ... 2>&1 | tee -a "$RUNLOG" >/dev/null || ...``
    # and could mark a corrupt tarball as success because tar's rc
    # was lost in the pipe. Now tar writes its stderr directly to a
    # dedicated log file; the rc is checked explicitly.
    local tar_log="$outdir/_diagnostics/tar.log"
    mkdir -p "$(dirname "$tar_log")"
    tar \
        --numeric-owner --owner=0 --group=0 \
        --sort=name \
        -C "$parent" \
        -czf "$tarball" \
        "$base" 2> "$tar_log"
    local tar_rc=$?
    if [[ $tar_rc -ne 0 ]]; then
        log_error "tar failed rc=$tar_rc — see $tar_log (partial tarball may exist on disk)"
        alert_append "error" "tarball build failed rc=$tar_rc; see tar.log"
        # Remove a possibly-corrupt partial tarball so nobody
        # inadvertently trusts it.
        rm -f "$tarball"
        return 1
    fi
    if [[ -s "$tar_log" ]]; then
        # tar emitted warnings despite rc=0 — surface them.
        log_warn "tar completed with warnings; see $tar_log"
    fi

    if [[ ! -f "$tarball" ]]; then
        log_error "tarball $tarball does not exist after tar returned rc=0 — contradictory state"
        return 1
    fi

    # Sanity-check the tarball via ``file`` (must be gzip'd tar).
    if command -v file >/dev/null 2>&1; then
        local ftype
        ftype=$(file --brief "$tarball" 2>/dev/null || echo "")
        if [[ "$ftype" != *"gzip compressed"* ]]; then
            log_error "tarball $tarball is not a gzip archive (file reports: $ftype)"
            alert_append "error" "tarball is not valid gzip: $ftype"
            return 1
        fi
    fi

    # SHA do tarball final — único arquivo fora do tarball com informação de integridade.
    # V4 Track H: capture sha256sum rc separately so a pipe failure cannot
    # silently yield empty output. PIPESTATUS[0] preserves the rc of the
    # first pipe segment even through `| awk`.
    local tarball_sha tarball_sha_rc
    tarball_sha=$(sha256sum "$tarball" | awk '{print $1}')
    tarball_sha_rc="${PIPESTATUS[0]}"
    if [[ "$tarball_sha_rc" -ne 0 ]]; then
        log_error "sha256sum of tarball failed rc=$tarball_sha_rc"
        alert_append "error" "tarball sha256sum failed rc=$tarball_sha_rc — integrity hash absent"
        return 1
    fi
    if [[ -z "$tarball_sha" || ! "$tarball_sha" =~ ^[0-9a-f]{64}$ ]]; then
        log_error "sha256sum of tarball produced invalid output: $tarball_sha"
        alert_append "error" "tarball sha256sum produced non-hex output — integrity hash invalid"
        return 1
    fi
    printf '%s  %s\n' "$tarball_sha" "$(basename "$tarball")" > "${tarball}.sha256"
    log_info "tarball sha256: $tarball_sha"
    log_info "tarball path:   $tarball"
    printf '%s\n' "$tarball"
    return 0
}

# ─────────────────────────────────────────────────────────────────────
# AUDIT v3+ T4 — Cross-correlation index
# ─────────────────────────────────────────────────────────────────────

_build_cross_correlation_index() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local out_json="$outdir/_diagnostics/cross_correlation.json"

    log_info "cross-correlation: building index..."

    # V4 Track B fix: fall back to system python3 if SOVYX_DIAG_PYTHON is
    # empty (common when Sovyx not installed on the diag host). Running
    # empty cmd yielded rc=127 and an empty cross_correlation.json, which
    # broke verdict_checklist downstream.
    local py="${SOVYX_DIAG_PYTHON:-$(command -v python3 || true)}"
    if [[ -z "$py" ]]; then
        log_warn "cross-correlation: no python3 available; skipping"
        printf '{"error":"no_python3","status":"skipped"}\n' > "$out_json"
        return 1
    fi

    "$py" - "$outdir" "$out_json" \
        "$SOVYX_DIAG_START_UTC_NS" "$SOVYX_DIAG_START_MONO_NS" \
        2>"$outdir/_diagnostics/cross_correlation.err" <<'PYEOF'
import csv
import json
import os
import pathlib
import re
import sys
import tempfile

outdir, out_path, start_utc_ns, start_mono_ns = sys.argv[1:]
outdir = pathlib.Path(outdir)
out = pathlib.Path(out_path)
t0_mono = int(start_mono_ns)

index = {
    "schema_version": 1,
    "t0_utc_ns": start_utc_ns,
    "t0_monotonic_ns": t0_mono,
    "captures": {},          # capture_id -> {artifacts, metrics, hypothesis_refs}
    "hypotheses": {},        # hyp_id -> {evidence_paths, alerts, operator_responses}
    "timeline_summary": [],  # sorted events: (mono_ns, offset_s, kind, step_id, detail)
    "state_snapshots": {},   # state_name -> {path, timestamp_pair}
    "alert_count_by_severity": {},
    "operator_response_count": 0,
}

# ── 1. captures: walk all capture_meta.json / capture.meta.json
for cap_meta in outdir.rglob("capture*meta.json"):
    try:
        d = json.loads(cap_meta.read_text(errors="replace"))
    except Exception:
        continue
    cid = d.get("capture_id") or cap_meta.parent.name
    # Collect sibling artifacts.
    parent = cap_meta.parent
    arts = {
        "meta": str(cap_meta.relative_to(outdir)),
        "wav": None,
        "analysis": None,
        "silero": None,
        "sovyx_context_dir": None,
    }
    for candidate, key in (
        ("capture.wav", "wav"),
        ("analysis.json", "analysis"),
        ("silero.json", "silero"),
    ):
        p = parent / candidate
        if p.exists():
            arts[key] = str(p.relative_to(outdir))
    ctx = parent / "sovyx_context"
    if ctx.is_dir():
        arts["sovyx_context_dir"] = str(ctx.relative_to(outdir))

    metrics = {}
    if arts["analysis"]:
        try:
            ad = json.loads((outdir / arts["analysis"]).read_text(errors="replace"))
            metrics = {
                "rms_dbfs": ad.get("rms_dbfs"),
                "peak_dbfs": ad.get("peak_dbfs"),
                "classification": ad.get("classification"),
                "spectral_rolloff_85_hz": (ad.get("spectral") or {}).get("rolloff_85_hz"),
                "spectral_flatness": (ad.get("spectral") or {}).get("flatness"),
                "clipping_samples": ad.get("clipping_samples"),
            }
        except Exception:
            pass
    if arts["silero"]:
        try:
            sd = json.loads((outdir / arts["silero"]).read_text(errors="replace"))
            metrics["silero_max_prob"] = sd.get("max_prob")
            metrics["silero_mean_prob"] = sd.get("mean_prob")
            metrics["silero_available"] = sd.get("available")
        except Exception:
            pass

    index["captures"][cid] = {
        "layer": d.get("layer") or "unknown",
        "tool": d.get("tool") or "unknown",
        "sample_rate": d.get("sample_rate") or d.get("actual_rate"),
        "channels": d.get("channels"),
        "duration_s_requested": d.get("duration_s_requested"),
        "duration_s_actual": d.get("duration_s_actual"),
        "duration_s_from_audio": d.get("duration_s_from_audio"),
        "retcode": d.get("retcode"),
        "sanity_pass": d.get("sanity_pass"),
        "start_monotonic_ns": d.get("start_monotonic_ns") or d.get("monotonic_ns_start"),
        "offset_from_t0_s": (
            (int(d.get("start_monotonic_ns") or d.get("monotonic_ns_start") or t0_mono) - t0_mono) / 1e9
            if d.get("start_monotonic_ns") or d.get("monotonic_ns_start") else None
        ),
        "artifacts": arts,
        "metrics": metrics,
    }

# ── 2. state snapshots
states_dir = outdir / "states"
if states_dir.is_dir():
    for state_dir in sorted(states_dir.iterdir()):
        if not state_dir.is_dir() or state_dir.name == "_diffs":
            continue
        ts_json = state_dir / "timestamp.json"
        ts_data = {}
        if ts_json.exists():
            try:
                ts_data = json.loads(ts_json.read_text(errors="replace"))
            except Exception:
                pass
        index["state_snapshots"][state_dir.name] = {
            "path": str(state_dir.relative_to(outdir)),
            "timestamp": ts_data,
        }

# ── 3. alerts
alerts_jsonl = outdir / "_diagnostics" / "alerts.jsonl"
if alerts_jsonl.exists():
    sev_count = {}
    for line in alerts_jsonl.read_text(errors="replace").splitlines():
        try:
            a = json.loads(line)
        except Exception:
            continue
        sev = a.get("severity", "info")
        sev_count[sev] = sev_count.get(sev, 0) + 1
    index["alert_count_by_severity"] = sev_count

# ── 4. operator responses
op_path = outdir / "_diagnostics" / "operator_responses.json"
if op_path.exists():
    try:
        op = json.loads(op_path.read_text(errors="replace"))
        responses = op.get("responses", [])
        index["operator_response_count"] = len(responses)
        # Map prompt_id → hypothesis for backlink.
        for r in responses:
            hyp = r.get("hypothesis", "")
            if hyp:
                for h in re.split(r"[/,]", hyp):
                    h = h.strip()
                    if not h:
                        continue
                    index["hypotheses"].setdefault(h, {
                        "evidence_paths": [],
                        "alerts": [],
                        "operator_responses": [],
                    })
                    index["hypotheses"][h]["operator_responses"].append(
                        r.get("prompt_id")
                    )
    except Exception:
        pass

# ── 5. timeline summary (first 200 events by mono_ns)
timeline_csv = outdir / "_diagnostics" / "timeline.csv"
if timeline_csv.exists():
    try:
        with timeline_csv.open(errors="replace") as f:
            reader = csv.DictReader(f)
            events = []
            for row in reader:
                mono = int(row.get("start_monotonic_ns") or 0)
                events.append({
                    "mono_ns": mono,
                    "offset_s": round((mono - t0_mono) / 1e9, 3) if mono else None,
                    "step_id": row.get("step_id"),
                    "state": row.get("state"),
                    "retcode": row.get("retcode"),
                    "notes": row.get("notes"),
                })
            events.sort(key=lambda e: e["mono_ns"])
            index["timeline_summary"] = events[:500]  # cap to keep file size bounded
    except Exception:
        pass

# ── 6. Hypotheses populated from capture + alerts (best-effort match by text)
# Rough linkage: iterate manifest fragments, extract hypothesis tags.
manifest_dir = outdir / "_diagnostics" / "manifest.d"
if manifest_dir.is_dir():
    hyp_re = re.compile(r"Hipótese:\s*([A-Z][0-9][A-Z0-9/\-_,\s]*)")
    for frag in manifest_dir.glob("*.md"):
        try:
            txt = frag.read_text(errors="replace")
        except Exception:
            continue
        m = hyp_re.search(txt)
        if not m:
            continue
        for h in re.split(r"[/,\s]+", m.group(1).strip()):
            h = h.strip("-_")
            if not h or not re.match(r"^[A-Z][0-9]", h):
                continue
            index["hypotheses"].setdefault(h, {
                "evidence_paths": [],
                "alerts": [],
                "operator_responses": [],
            })
            # Reference the artifact path from the fragment text (first
            # backtick-quoted path).
            path_m = re.search(r"`([^`]+)`", txt)
            if path_m:
                index["hypotheses"][h]["evidence_paths"].append(path_m.group(1))

# Atomic write.
out.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(mode="w", delete=False,
                                  dir=str(out.parent),
                                  prefix=f".{out.name}.",
                                  suffix=".tmp",
                                  encoding="utf-8") as tmp:
    tmp.write(json.dumps(index, indent=2, ensure_ascii=False))
    tmp.flush()
    os.fsync(tmp.fileno())
    tmp_path = tmp.name
os.replace(tmp_path, str(out))
PYEOF
    local ccx_rc=$?
    if [[ $ccx_rc -ne 0 ]]; then
        log_warn "cross-correlation index failed rc=$ccx_rc; see cross_correlation.err"
        return 1
    fi
    return 0
}

# ─────────────────────────────────────────────────────────────────────
# AUDIT v3+ T5 — VERDICT_CHECKLIST.md auto-generation
# ─────────────────────────────────────────────────────────────────────

_build_verdict_checklist() {
    local outdir="$SOVYX_DIAG_OUTDIR"
    local out_md="$outdir/VERDICT_CHECKLIST.md"
    local ccx="$outdir/_diagnostics/cross_correlation.json"

    log_info "verdict-checklist: building from cross-correlation index..."

    # V4 Track B fix: same python fallback as cross_correlation.
    local py="${SOVYX_DIAG_PYTHON:-$(command -v python3 || true)}"
    if [[ -z "$py" ]]; then
        log_warn "verdict-checklist: no python3 available; skipping"
        printf '# VERDICT CHECKLIST — not generated\n\nNo python3 available during finalize.\n' > "$out_md"
        return 1
    fi

    "$py" - "$outdir" "$ccx" "$out_md" \
        2>"$outdir/_diagnostics/verdict_checklist.err" <<'PYEOF'
import json
import pathlib
import sys

outdir = pathlib.Path(sys.argv[1])
ccx_path = pathlib.Path(sys.argv[2])
out_path = pathlib.Path(sys.argv[3])

try:
    ccx = json.loads(ccx_path.read_text(errors="replace"))
except Exception:
    ccx = {}

# Hypothesis catalog (extracted from layer source files — stable list).
HYPOTHESIS_CATALOG = {
    "A1": ("Switch físico de privacidade do mic (Fn+F1 em VAIOs)", "A_hardware/"),
    "A2": ("Mic interno com defeito", "A_hardware/ + W1 capture"),
    "A3": ("BIOS com mic desabilitado", "A_hardware/dmidecode.txt + operator BIOS screenshot"),
    "A4": ("Codec SN6180 em estado travado", "A_hardware/codec_*.txt"),
    "A5": ("Feature flags do BIOS inválidas", "A_hardware/dmidecode.txt"),
    "A6": ("Hijack por Bluetooth headset / USB mic / HDMI audio", "A_hardware/bluetooth.txt + lsusb"),
    "B1": ("Regressão no driver HDA para SN6180", "B_kernel/lsmod + modinfo + apt_history"),
    "B2": ("snd_hda_intel.power_save adormecendo o codec", "B_kernel/ + /sys power state"),
    "B3": ("Kernel taint ou erro em dmesg", "B_kernel/tainted.txt + dmesg_watch"),
    "B4": ("Runtime PM agressivo (power/control=auto)", "B_kernel/runtime_pm.txt"),
    "B5": ("PREEMPT_DYNAMIC / HZ baixo causando jitter", "B_kernel/kconfig_sound.txt"),
    "B6": ("DKMS quebrado → kernel tainted", "B_kernel/dkms_status.txt"),
    "B7": ("Update recente de kernel/audio → regressão", "B_kernel/apt_history + dpkg_log"),
    "C1": ("Controle Capture/Internal Mic Boost em 0", "C_alsa/amixer_*.txt"),
    "C2": ("Mixer switch (Capture Switch) desligado", "C_alsa/amixer_contents.txt"),
    "C3": ("Input source errado (Rear Mic em vez de Internal Mic)", "C_alsa/amixer + W1 capture"),
    "C4": ("Banda limitada no ADC (DSP do codec)", "C_alsa/captures/W1 + analysis.json:rolloff"),
    "C5": ("plughw healthy mas default/pipewire destroi", "compare W1 vs W5 analysis"),
    "C6": ("UCM carregou profile com DSP voicecall", "C_alsa/ucm_*.txt"),
    "D1": ("Filter chain (echo-cancel / rnnoise / webrtc)", "D_pipewire/pw_dump_filters.json + alerts"),
    "D2": ("WirePlumber policy roteando para virtual source", "D_pipewire/wpctl + pw_dump"),
    "D3": ("Quantum travado problemático", "D_pipewire/pw_metadata_settings.txt"),
    "D4": ("Sample rate conflito (graph ≠ default ≠ client)", "pactl info + pw_metadata + capture_meta actual_rate"),
    "D5": ("Outro processo segurou o source", "D_pipewire/pactl_source-outputs.txt + lsof_snd"),
    "D6": ("3 saltos, 3 resamples (pcm.default → alsa-pa → pipewire-pulse)", "compare W1 rate vs W5 rate vs W11 rate"),
    "D7": ("Script Lua customizado", "operator response: pipewire_custom_configs"),
    "D8": ("pw-metadata default_target apontando node errado", "D_pipewire/pw_metadata_full.txt"),
    "D9": ("XDG portal interferindo", "F_session + D_pipewire"),
    "E_P1": ("PortAudio resamplando 48→16k destrutivamente", "compare W1 vs W11 spectrum"),
    "E_P2": ("default com max_input_channels=64 forçando downmix", "E_portaudio/sounddevice_query.txt"),
    "E_P3": ("libportaudio com bug para PipeWire", "E_portaudio/ldd_*.txt + pip_show_sounddevice"),
    "E_P4": ("Latência/buffer 32ms incompatível com graph", "E_portaudio/captures/*/capture_meta.json:latency_actual_s"),
    "E_P5": ("Wheel sounddevice com libportaudio estática patch destoante", "E_portaudio/libportaudio_search.txt + ldd"),
    "F1": ("Usuário fora do grupo audio", "F_session/user_info + groups"),
    "F2": ("/dev/snd com permissões errôneas", "F_session + C_alsa/lsof_snd_pre"),
    "F3": ("Sessão systemd --user degradada", "F_session/loginctl + busctl"),
    "F4": ("AppArmor/SELinux bloqueando Sovyx", "F_session/apparmor + journal denied"),
    "F5": ("Shell diferente com env diferente", "F_session/env_redacted.txt"),
    "F6": ("Multiple daemon instances", "states/*/sovyx_pid_count.txt + alerts"),
    "F7": ("Sovyx em cgroup restritivo", "states/*/sovyx_proc_cgroup.txt"),
    "F8": ("Polkit rule bloqueando RealtimeKit1", "F_session/busctl + polkit"),
    "F9": ("XDG portal interferindo", "F_session/xdg_*"),
    "G1": ("SOVYX_TUNING__VOICE__* env/config overriding", "G_sovyx/env_SOVYX_redacted.txt + config_files_redacted"),
    "G2": ("combo_store healthy + pipeline dead (discrepância)", "G_sovyx/combo_store_dump.txt + capture_diagnostics"),
    "G3": ("0.21.1 vs 0.21.2 — VLX-001..008", "G_sovyx/sovyx_version.txt"),
    "G4": ("Divergência cascade winner ↔ opener runtime (race)", "G_sovyx/sovyx_log_tail + H round slice"),
    "G5": ("Moonshine English-only", "G_sovyx/doctor_voice*.json:mind language"),
    "G6": ("config.yaml mtime recente", "G_sovyx/sovyx_files_tree.txt"),
    "G7": ("API keys ausentes/expiradas", "G_sovyx/api_keys_presence.json"),
    "H1": ("meter vivo, pipeline produtivo morto (divergência)", "H_pipeline_live/voice_status + test_input probe"),
    "H2": ("Race Sovyx-vs-PipeWire na init", "H_pipeline_live + T_guardian dmesg_watch + journal_watch"),
    "I1": ("WebSocket desconectando", "I_network/websocket_probe.txt"),
    "I2": ("Porta 7777 bloqueada", "I_network/ss_listen_7777.txt + firewall"),
    "I3": ("Clock skew quebrando TLS", "I_network/clock_drift.txt"),
    "K1": ("Sink muted / volume 0 / role suspenso", "K_output/default_sink + volumes_mute + alerts"),
    "K2": ("PipeWire routing HDMI/BT sem alto-falantes", "K_output/default_sink + alerts:hdmi_as_default"),
    "K3": ("Kokoro gerou .wav mas playback não pedido", "K_output/kokoro_synth + playback_results"),
    "K4": ("Sink bloqueado por cliente exclusivo", "K_output/lsof_pcm_playback_pre.txt"),
}

hyps = ccx.get("hypotheses", {})
alerts_sev = ccx.get("alert_count_by_severity", {})
captures = ccx.get("captures", {})

md = []
md.append("# VERDICT CHECKLIST — sovyx-voice-diag")
md.append("")
md.append("Auto-gerado no finalize. Use este documento como checklist de análise.")
md.append("Para cada hipótese, registre verdict: **CONFIRMED / REFUTED / INCONCLUSIVE**")
md.append("+ justificativa (1-3 linhas citando `path:campo=valor`).")
md.append("")
md.append(f"**Alertas automáticos emitidos:** " + ", ".join(
    f"{sev}={cnt}" for sev, cnt in sorted(alerts_sev.items())) or "nenhum")
md.append(f"**Capturas coletadas:** {len(captures)}")
md.append(f"**Operator responses:** {ccx.get('operator_response_count', 0)}")
md.append("")
md.append("---")
md.append("")

# Group by layer prefix for readability.
grouped = {}
for hid, (desc, src) in HYPOTHESIS_CATALOG.items():
    prefix = hid[0] if hid[0].isalpha() else hid.split("_")[0][0]
    grouped.setdefault(prefix, []).append((hid, desc, src))

layer_names = {
    "A": "A — Hardware", "B": "B — Kernel", "C": "C — ALSA",
    "D": "D — PipeWire", "E": "E — PortAudio", "F": "F — Session",
    "G": "G — Sovyx", "H": "H — Pipeline Live",
    "I": "I — Network", "K": "K — Output",
}

for prefix in sorted(grouped):
    md.append(f"## {layer_names.get(prefix, prefix)}")
    md.append("")
    for hid, desc, src in grouped[prefix]:
        hx = hyps.get(hid, {})
        ev_paths = hx.get("evidence_paths", [])[:5]
        op_resps = hx.get("operator_responses", [])

        md.append(f"### {hid} — {desc}")
        md.append("")
        md.append(f"- **Fonte primária**: `{src}`")
        if ev_paths:
            md.append(f"- **Evidence paths**: " +
                      ", ".join(f"`{p}`" for p in ev_paths))
        if op_resps:
            md.append(f"- **Operator responses**: " +
                      ", ".join(f"`{r}`" for r in op_resps))
        md.append("")
        md.append("**Verdict**: [ ] CONFIRMED  [ ] REFUTED  [ ] INCONCLUSIVE")
        md.append("")
        md.append("**Evidência citada** (fill in):")
        md.append("```")
        md.append("")
        md.append("```")
        md.append("")

md.append("---")
md.append("")
md.append("## Composite Verdict")
md.append("")
md.append("After completing every hypothesis above, write the composite")
md.append("conclusion here. Root-cause candidates (confirmed hypotheses)")
md.append("+ corroborating evidence + recommended remediation.")
md.append("")
md.append("```")
md.append("")
md.append("```")
md.append("")

out_path.write_text("\n".join(md) + "\n", encoding="utf-8")
PYEOF
    local vc_rc=$?
    if [[ $vc_rc -ne 0 ]]; then
        log_warn "verdict checklist generation failed rc=$vc_rc; see verdict_checklist.err"
        return 1
    fi
    return 0
}

# Função pública chamada pelo trap EXIT (common.sh::_cleanup).
# Uso: finalize_package [suffix] [exit_code]
finalize_package() {
    local suffix="${1:-}"
    local exit_code="${2:-0}"
    local status="complete"
    [[ -n "$suffix" ]] && status="partial"

    log_info "finalize_package: status=$status exit_code=$exit_code"

    # 1. Stop Temporal Guardian (if started).
    if declare -F stop_guardian >/dev/null 2>&1; then
        stop_guardian || log_warn "stop_guardian failed"
    fi

    # 2. Gera alertas automáticos (§9 do plano).
    if declare -F generate_alerts >/dev/null 2>&1; then
        generate_alerts || log_warn "generate_alerts failed"
    fi

    # 3. Consolida MANIFEST.md.
    _assemble_manifest || log_warn "manifest assembly failed"

    # 4. AUDIT v3+ T4: cross-correlation index.
    _build_cross_correlation_index || log_warn "cross-correlation index failed"

    # 5. AUDIT v3+ T5: VERDICT_CHECKLIST.md autogenerated.
    _build_verdict_checklist || log_warn "verdict checklist generation failed"

    # 6. Atualiza SUMMARY.json.
    _finalize_summary "$status" "$exit_code"

    # 7. Gera CHECKSUMS.sha256 granular (runs AFTER all content above).
    _generate_checksums

    # 8. Cria tarball.
    _build_tarball "$suffix"
}
