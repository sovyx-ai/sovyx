#!/usr/bin/env bash
# lib/O_prompts.sh — Operator prompts orchestrator.
#
# Fecha os 3 gaps perceptuais/subjetivos que o script não consegue
# medir automaticamente:
#
#   (1) Percepção auditiva — o WAV tem chiado? clipping audível?
#       distorção? artefatos de DSP? O analisador reporta rolloff/RMS
#       mas NÃO "soa natural". Operador preenche o gap escutando.
#
#   (2) Experiência subjetiva durante a captura — operador notou click,
#       LED do mic acendeu, feedback de alto-falante, ruído ambiente.
#
#   (3) Evidência out-of-band — BIOS settings, screenshot do dashboard,
#       arquivos de config que exigem inspect humano.
#
# Invocado pelo orchestrator APÓS todas as camadas e ANTES de
# finalize_package. Prompts organizados em 3 priorities:
#   P1 (MUST answer): core audio perception, crítico para verdict.
#   P2 (VALUABLE): contexto ambiental e subjetivo.
#   P3 (OPTIONAL): BIOS, external screenshots.
#
# Respostas persistidas atomicamente em
# ``_diagnostics/operator_responses.json`` como primeiro-classe
# forensic artifact (checksummed, manifest-listed).

run_operator_prompts() {
    local diag_dir="$SOVYX_DIAG_OUTDIR/_diagnostics"
    local out_json="$diag_dir/operator_responses.json"
    local attach_dir="$diag_dir/operator_attachments"
    mkdir -p "$attach_dir"

    if [[ "${SOVYX_DIAG_FLAG_SKIP_OPERATOR_PROMPTS:-0}" = "1" ]] \
        || [[ "${SOVYX_DIAG_FLAG_NON_INTERACTIVE:-0}" = "1" ]]; then
        log_info "Operator prompts: skipped (--skip-operator-prompts or --non-interactive)"
        # Still emit a structured "skipped" record so the analyst sees
        # it was intentional (not forgotten).
        cat > "$out_json" <<EOF
{
  "schema_version": 1,
  "status": "skipped",
  "skip_reason": "flag_non_interactive_or_skip_operator_prompts",
  "responses": []
}
EOF
        manifest_append "O_prompts" "_diagnostics/operator_responses.json" \
            "Operator prompts skipped — unattended mode. Analyst coverage reduced for perceptual/subjective hypotheses." \
            "perceptual/subjective"
        return 0
    fi

    if [[ -z "$SOVYX_DIAG_PYTHON" ]]; then
        log_warn "Operator prompts: no python — skipped"
        cat > "$out_json" <<EOF
{
  "schema_version": 1,
  "status": "skipped",
  "skip_reason": "no_python",
  "responses": []
}
EOF
        return 1
    fi

    log_info "=== Operator prompts — fechando gaps perceptuais ==="
    printf '\n' >&2
    printf '  ╔══════════════════════════════════════════════════════════════════╗\n' >&2
    printf '  ║  ETAPA FINAL — PROMPTS AO OPERADOR                                ║\n' >&2
    printf '  ║  Tempo estimado: 5-10 min.                                        ║\n' >&2
    printf '  ║  Respostas críticas para completar a análise forense.             ║\n' >&2
    printf '  ║  Digite SKIP + razão em qualquer prompt se não puder responder.   ║\n' >&2
    printf '  ╚══════════════════════════════════════════════════════════════════╝\n' >&2
    printf '\n' >&2

    # Launch the python interactive runner. It reads the prompt catalog
    # from an embedded JSON (below), guides the operator through each
    # prompt, and writes the responses atomically.
    "$SOVYX_DIAG_PYTHON" "$SOVYX_DIAG_LIB_DIR/py/prompt_runner.py" \
        --outdir "$SOVYX_DIAG_OUTDIR" \
        --catalog "$SOVYX_DIAG_LIB_DIR/prompts_catalog.json" \
        --attachments-dir "$attach_dir" \
        --output "$out_json"

    local pr_rc=$?

    if [[ $pr_rc -ne 0 ]]; then
        log_warn "Operator prompts runner exited with rc=$pr_rc (some prompts may be unanswered)"
        alert_append "warn" "operator_prompts completed with rc=$pr_rc; partial coverage"
    fi

    manifest_append "O_prompts" "_diagnostics/operator_responses.json _diagnostics/operator_attachments/" \
        "Respostas do operador: percepção auditiva dos WAVs, recall subjetivo durante capturas, screenshots externos. Fecha gaps que o script não mede (audio listen, BIOS flags, experience recall)." \
        "perceptual/subjective/external"

    return 0
}
