#!/usr/bin/env bash
# voice-recover-mic.sh — restore mic to known-good state + verify
#
# Triagem para o cenário: arecord -D plughw:1,0 retornou RMS=8792 antes
# mas agora retorna RMS=1 (regressão a nível kernel/codec, não Sovyx).
# Reaplica o setup conhecido + verifica se mic volta. Captura
# state COMPLETO do codec + amixer pra forensic offline.
#
# Uso:
#   curl -fsSL https://raw.githubusercontent.com/sovyx-ai/sovyx/main/scripts/voice-recover-mic.sh \
#     -o /tmp/voice-recover.sh && bash /tmp/voice-recover.sh
#
# Pré-requisito: sovyx PARADO.

set -uo pipefail

readonly OUT_DIR="/tmp/sovyx-recover"
mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR"/*

if [[ -t 1 ]]; then
    C_HDR='\033[1;36m'; C_OK='\033[1;32m'; C_WARN='\033[1;31m'; C_END='\033[0m'
else
    C_HDR=''; C_OK=''; C_WARN=''; C_END=''
fi
readonly C_HDR C_OK C_WARN C_END

print_header() {
    echo ""
    echo -e "${C_HDR}═══════════════════════════════════════════════════════════════${C_END}"
    echo -e "${C_HDR} $* ${C_END}"
    echo -e "${C_HDR}═══════════════════════════════════════════════════════════════${C_END}"
}

# ─── PRÉ-VOO ───────────────────────────────────────────────────────
print_header "PRÉ-VOO"
echo "Confirmações antes de seguir:"
echo "  1. Sovyx PARADO (Ctrl+C no terminal dele)"
echo "  2. NENHUM headphone/headset plugado no jack 3.5mm"
echo "  3. Tecla F-mic-mute do laptop verificada (procure ícone de mic riscado)"
echo "     → No VAIO geralmente é Fn+F1, F4 ou tecla com símbolo de mic"
echo "     → Se tiver LED vermelho aceso, mic está mutado por hardware"
read -p "Pressione ENTER quando confirmar os 3 pontos acima..."

# ─── PASSO 1 — snapshot ATUAL antes de qualquer mexida ────────────
print_header "PASSO 1 — Snapshot ATUAL (antes da recovery)"
amixer -c1 > "$OUT_DIR/amixer-CURRENT.txt" 2>&1
echo "amixer atual → $OUT_DIR/amixer-CURRENT.txt ($(wc -l < "$OUT_DIR/amixer-CURRENT.txt") linhas)"
echo ""
echo "── Estado dos controles críticos AGORA: ──"
amixer -c1 sget 'Capture'           2>&1 | grep -E "^Simple|Front|Mono|\[on\]|\[off\]|Limits"
amixer -c1 sget 'Capture' 2>&1 | head -8
echo ""
amixer -c1 sget 'Internal Mic Boost Volume' 2>&1 | head -8
echo ""
amixer -c1 sget 'Mic Boost Volume' 2>&1 | head -8
echo ""
amixer -c1 sget 'Auto-Mute Mode' 2>&1 | head -8

# ─── PASSO 2 — RECOVERY: aplicar setup conhecido bom ──────────────
print_header "PASSO 2 — RECOVERY (re-aplicar setup conhecido)"
echo "Aplicando..."

# Capture switch ON + 80%
amixer -c1 sset 'Capture' cap          2>&1 | tail -3
amixer -c1 sset 'Capture' 80%          2>&1 | tail -3

# Mic boost interno 67%
amixer -c1 sset 'Internal Mic Boost Volume' 67%  2>&1 | tail -3

# Mic boost externo também 67% (caso PipeWire selecionou esse path por engano)
amixer -c1 sset 'Mic Boost Volume' 67% 2>&1 | tail -3

# Auto-Mute desabilita (pode estar mutando o mic interno se headphone "fantasma" está detectado)
amixer -c1 sset 'Auto-Mute Mode' Disabled 2>&1 | tail -3 || \
    echo "  (Auto-Mute Mode não aceitou 'Disabled', tentando outras opções...)"
amixer -c1 sset 'Auto-Mute Mode' off 2>&1 | tail -3 || true

# Master playback unmute (caso reflita pra capture)
amixer -c1 sset 'Master Playback Switch' on 2>&1 | tail -3 || true

echo "Recovery aplicado."

# ─── PASSO 3 — verifica state DEPOIS da recovery ──────────────────
print_header "PASSO 3 — Snapshot DEPOIS da recovery"
amixer -c1 > "$OUT_DIR/amixer-AFTER-RECOVERY.txt" 2>&1
echo "amixer pós-recovery → $OUT_DIR/amixer-AFTER-RECOVERY.txt"
echo ""
echo "── Estado dos controles críticos APÓS recovery: ──"
amixer -c1 sget 'Capture' | grep -E "Front|Mono|\[on\]|\[off\]|Limits" | head -5
echo ""
amixer -c1 sget 'Internal Mic Boost Volume' | grep -E "Front|Mono|Limits" | head -5
echo ""
amixer -c1 sget 'Auto-Mute Mode' | grep "Item0" | head -3

# ─── PASSO 4 — TEST: arecord direto ───────────────────────────────
print_header "PASSO 4 — TEST arecord -D plughw:1,0 (verificação se mic voltou)"
echo ""
echo -e "${C_OK}>>> FALE 'um dois três quatro' por 3 segundos quando começar <<<${C_END}"
sleep 1
arecord -D plughw:1,0 -d 3 -f S16_LE -r 16000 -c 1 "$OUT_DIR/post-recovery.wav" 2>&1 | tail -3

if [[ ! -f "$OUT_DIR/post-recovery.wav" ]]; then
    echo -e "${C_WARN}❌ arecord falhou, WAV não criado.${C_END}"
else
    python3 - "$OUT_DIR/post-recovery.wav" <<'PY'
import sys, wave, audioop
with wave.open(sys.argv[1], 'rb') as w:
    data = w.readframes(w.getnframes())
    sw = w.getsampwidth()
    rms = audioop.rms(data, sw) if data else 0
    print(f"")
    print(f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  RMS = {rms}")
    print(f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if rms >= 2000:
        print(f"  ✅ MIC VOLTOU. Recovery funcionou.")
    elif rms >= 500:
        print(f"  ⚠️ Mic marginal. Funciona mas baixo.")
    else:
        print(f"  ❌ MIC AINDA MUDO. Recovery não resolveu.")
        print(f"     Causa NÃO é mixer. Verificar:")
        print(f"     - Tecla F-mic-mute do laptop (LED vermelho)")
        print(f"     - Headphone/headset plugado")
        print(f"     - Hardware do mic")
PY
fi

# ─── PASSO 5 — captura forense COMPLETA ───────────────────────────
print_header "PASSO 5 — Captura forense full"
cat /proc/asound/card1/codec#0 > "$OUT_DIR/codec-FULL.txt" 2>/dev/null
echo "Codec dump (FULL) → $OUT_DIR/codec-FULL.txt ($(wc -l < "$OUT_DIR/codec-FULL.txt") linhas)"

# Diff entre estado original e pós-recovery (mostra exatamente o que mudou)
diff "$OUT_DIR/amixer-CURRENT.txt" "$OUT_DIR/amixer-AFTER-RECOVERY.txt" > "$OUT_DIR/amixer-DIFF.txt" 2>&1
if [[ -s "$OUT_DIR/amixer-DIFF.txt" ]]; then
    echo ""
    echo -e "${C_HDR}── Diferenças que a recovery introduziu: ──${C_END}"
    cat "$OUT_DIR/amixer-DIFF.txt"
else
    echo ""
    echo -e "${C_WARN}!! Recovery NÃO mudou nada no amixer.${C_END}"
    echo "   Significa que os controles JÁ ESTAVAM no estado correto antes."
    echo "   Causa do mic mudo NÃO é mixer state."
fi

# ─── RESUMO ───────────────────────────────────────────────────────
print_header "RESUMO"
ls -la "$OUT_DIR/"
echo ""
echo -e "${C_OK}Cole o output COMPLETO no chat.${C_END}"
echo "Se mic NÃO voltou, vou pedir info adicional sobre estado físico do laptop."
