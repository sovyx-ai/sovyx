#!/usr/bin/env bash
# voice-recover-mic-v2.sh — fix dos bugs de naming do v1
#
# v1 (voice-recover-mic.sh) usou nomes errados em simple-control mode
# do amixer: "Internal Mic Boost Volume" e "Mic Boost Volume" e
# "Master Playback Switch" não existem em simple mode (são nomes do
# kernel iface=MIXER, não simple controls).
#
# v2 corrige usando nomes validados pela própria sessão do operador:
#   amixer sset 'Internal Mic Boost' 67%   (sem "Volume")
#   amixer sset 'Mic Boost' 67%             (sem "Volume")
#   amixer sset 'Master' on                 (sem "Playback Switch")
#
# Adiciona também: lista TODOS os simple controls disponíveis ANTES
# de tentar setar, valida cada sset com check pós-aplicação, persiste
# state correto via alsactl store ao final.
#
# Uso:
#   curl -fsSL https://raw.githubusercontent.com/sovyx-ai/sovyx/main/scripts/voice-recover-mic-v2.sh \
#     -o /tmp/voice-recover-v2.sh && bash /tmp/voice-recover-v2.sh

set -uo pipefail

readonly OUT_DIR="/tmp/sovyx-recover-v2"
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

# Aplica sset com validação. Retorna 0 se OK, 1 se controle não existe.
sset_with_validation() {
    local control="$1"; shift
    local value="$1"
    if ! amixer -c1 sget "$control" >/dev/null 2>&1; then
        echo -e "${C_WARN}  ⊘ Simple control '$control' não existe — pulando${C_END}"
        return 1
    fi
    local before
    before=$(amixer -c1 sget "$control" 2>/dev/null | grep -oE "\[[0-9]+%\]" | head -1)
    amixer -c1 sset "$control" "$value" >/dev/null 2>&1
    local after
    after=$(amixer -c1 sget "$control" 2>/dev/null | grep -oE "\[[0-9]+%\]" | head -1)
    echo -e "${C_OK}  ✓ '$control' '$value' → ${before:-N/A} → ${after:-N/A}${C_END}"
    return 0
}

# ─── PRÉ-VOO ───────────────────────────────────────────────────────
print_header "PRÉ-VOO"
echo "Sovyx PARADO + nenhum headphone plugado + tecla F-mic-mute OK."
read -p "ENTER quando confirmar..."

# ─── PASSO 1 — listar TODOS os simple controls (ground truth) ─────
print_header "PASSO 1 — Listar todos os simple controls disponíveis (ground truth)"
amixer -c1 scontrols > "$OUT_DIR/scontrols.txt" 2>&1
echo "Salvo em $OUT_DIR/scontrols.txt"
echo ""
echo "── Simple controls que importam para captura: ──"
amixer -c1 scontrols | grep -iE "capture|mic|master|auto" || echo "(nenhum match)"

# ─── PASSO 2 — snapshot ANTES ────────────────────────────────────
print_header "PASSO 2 — Estado ATUAL dos controles críticos"
for ctrl in 'Capture' 'Internal Mic Boost' 'Mic Boost' 'Auto-Mute Mode' 'Master'; do
    echo ""
    echo "── '$ctrl': ──"
    amixer -c1 sget "$ctrl" 2>&1 | head -7 || echo "  (não existe)"
done

amixer -c1 > "$OUT_DIR/amixer-BEFORE.txt"

# ─── PASSO 3 — RECOVERY (nomes corretos) ──────────────────────────
print_header "PASSO 3 — RECOVERY com nomes amixer validados"
echo ""
sset_with_validation 'Capture' 'cap'      # garantir switch ON
sset_with_validation 'Capture' '80%'
sset_with_validation 'Internal Mic Boost' '100%'   # MAX boost (não 67% — queremos máximo)
sset_with_validation 'Mic Boost' '100%'             # mesmo para externo (caso routing)
sset_with_validation 'Auto-Mute Mode' 'Disabled'
sset_with_validation 'Master' 'on'

# ─── PASSO 4 — snapshot DEPOIS + diff ─────────────────────────────
print_header "PASSO 4 — Estado pós-recovery + diff"
amixer -c1 > "$OUT_DIR/amixer-AFTER.txt"
diff "$OUT_DIR/amixer-BEFORE.txt" "$OUT_DIR/amixer-AFTER.txt" > "$OUT_DIR/amixer-DIFF.txt" 2>&1
if [[ -s "$OUT_DIR/amixer-DIFF.txt" ]]; then
    echo -e "${C_HDR}── Mudanças aplicadas: ──${C_END}"
    cat "$OUT_DIR/amixer-DIFF.txt"
else
    echo -e "${C_OK}Nenhuma mudança — controles já estavam no setup correto.${C_END}"
fi
echo ""
echo "── Internal Mic Boost APÓS recovery: ──"
amixer -c1 sget 'Internal Mic Boost' 2>&1 | head -7

# ─── PASSO 5 — TEST arecord direto ────────────────────────────────
print_header "PASSO 5 — TEST arecord -D plughw:1,0"
echo -e "${C_OK}>>> FALE 'um dois três quatro' por 3 segundos quando começar <<<${C_END}"
sleep 1
arecord -D plughw:1,0 -d 3 -f S16_LE -r 16000 -c 1 "$OUT_DIR/test.wav" 2>&1 | tail -3

if [[ ! -f "$OUT_DIR/test.wav" ]]; then
    echo -e "${C_WARN}❌ arecord falhou${C_END}"
else
    python3 - "$OUT_DIR/test.wav" <<'PY'
import sys, wave, audioop
with wave.open(sys.argv[1], 'rb') as w:
    data = w.readframes(w.getnframes())
    sw = w.getsampwidth()
    rms = audioop.rms(data, sw) if data else 0
print(f"")
print(f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print(f"  RMS = {rms}")
print(f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
if rms >= 5000:
    print(f"  ✅ EXCELENTE — boost surtiu efeito (era 781 antes)")
elif rms >= 2000:
    print(f"  ✅ BOM — funciona pra Sovyx")
elif rms >= 500:
    print(f"  ⚠️ MARGINAL — ainda funciona mas baixo")
else:
    print(f"  ❌ AINDA MUDO — problema mais profundo")
PY
fi

# ─── PASSO 6 — persistir state ────────────────────────────────────
print_header "PASSO 6 — Persistir via alsactl store"
sudo alsactl store 1 2>&1
echo -e "${C_OK}State salvo em /var/lib/alsa/asound.state.${C_END}"
echo "Sobreviverá ao próximo boot."

# ─── RESUMO ───────────────────────────────────────────────────────
print_header "RESUMO"
ls -la "$OUT_DIR/"
echo ""
echo -e "${C_OK}Cole o output completo no chat.${C_END}"
