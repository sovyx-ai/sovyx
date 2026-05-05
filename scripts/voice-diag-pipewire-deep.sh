#!/usr/bin/env bash
# voice-diag-pipewire-deep.sh — deep diagnostic v2 for Linux+PipeWire silent-mic
#
# v1 (voice-diag-pipewire.sh) provou que o PipeWire entrega silêncio (RMS=50)
# enquanto ALSA direto entrega RMS=8792. v2 isola ONDE no PipeWire isso
# acontece, com restart limpo + ordering correto pra evitar device-busy +
# captura forense do codec state.
#
# Uso:
#   curl -fsSL https://raw.githubusercontent.com/sovyx-ai/sovyx/main/scripts/voice-diag-pipewire-deep.sh \
#     -o /tmp/voice-deep.sh && bash /tmp/voice-deep.sh
#
# Pré-requisito: sovyx PARADO (Ctrl+C no terminal do `sovyx start`).

set -uo pipefail

readonly OUT_DIR="/tmp/sovyx-deep"
mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR"/*.wav "$OUT_DIR"/*.txt

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

analyze_mono() {
    local wav="$1"
    if [[ ! -f "$wav" ]]; then
        echo -e "${C_WARN}  ❌ Arquivo não criado: $wav${C_END}"
        return
    fi
    python3 - "$wav" <<'PY'
import sys, wave, audioop
path = sys.argv[1]
try:
    with wave.open(path, 'rb') as w:
        data = w.readframes(w.getnframes())
        sw = w.getsampwidth()
        rms = audioop.rms(data, sw) if data else 0
        dur = w.getnframes()/w.getframerate() if w.getframerate() else 0
        print(f"  RMS = {rms}  duration={dur:.2f}s  ch={w.getnchannels()}  rate={w.getframerate()}Hz")
except Exception as e:
    print(f"  ❌ Análise falhou: {e}")
PY
}

analyze_stereo() {
    local wav="$1"
    if [[ ! -f "$wav" ]]; then
        echo -e "${C_WARN}  ❌ Arquivo não criado: $wav${C_END}"
        return
    fi
    python3 - "$wav" <<'PY'
import sys, wave, audioop
path = sys.argv[1]
try:
    with wave.open(path, 'rb') as w:
        data = w.readframes(w.getnframes())
        sw = w.getsampwidth()
        if not data:
            print("  (sem dados)")
        else:
            left  = audioop.tomono(data, sw, 1.0, 0.0)
            right = audioop.tomono(data, sw, 0.0, 1.0)
            mono  = audioop.tomono(data, sw, 0.5, 0.5)
            print(f"  Left  RMS = {audioop.rms(left, sw)}")
            print(f"  Right RMS = {audioop.rms(right, sw)}")
            print(f"  Mono  RMS = {audioop.rms(mono, sw)}")
except Exception as e:
    print(f"  ❌ Análise falhou: {e}")
PY
}

# ─── PRÉ-VOO ───────────────────────────────────────────────────────
print_header "PRÉ-VOO"
echo "REQUISITO: daemon sovyx PARADO. Se rodando, dê Ctrl+C nele AGORA."
echo "Output dir: $OUT_DIR"
read -p "Pressione ENTER quando sovyx estiver parado e pronto pra começar..."

# ─── PASSO 1 — restart PipeWire pra estado limpo ──────────────────
print_header "PASSO 1 — Restart PipeWire/WirePlumber (estado limpo)"
echo "systemctl --user restart wireplumber pipewire pipewire-pulse"
systemctl --user restart wireplumber pipewire pipewire-pulse
echo "Aguardando 4s pro WirePlumber estabilizar..."
sleep 4
echo -e "${C_OK}OK. PipeWire reiniciado.${C_END}"

# ─── PASSO 2 — snapshot config ANTES dos tests ────────────────────
print_header "PASSO 2 — Snapshot da config de áudio (pre-test)"
amixer -c1 > "$OUT_DIR/amixer-c1-BEFORE.txt" 2>&1
echo "amixer pre → $OUT_DIR/amixer-c1-BEFORE.txt ($(wc -l < "$OUT_DIR/amixer-c1-BEFORE.txt") linhas)"
wpctl status > "$OUT_DIR/wpctl-BEFORE.txt" 2>&1
echo "wpctl status pre → $OUT_DIR/wpctl-BEFORE.txt"
pactl list sources > "$OUT_DIR/pactl-sources-BEFORE.txt" 2>&1
echo "pactl sources pre → $OUT_DIR/pactl-sources-BEFORE.txt"

# Codec dump — a fonte forense definitiva
if [[ -r /proc/asound/card1/codec#0 ]]; then
    cat "/proc/asound/card1/codec#0" > "$OUT_DIR/codec-card1.txt"
    echo "codec dump → $OUT_DIR/codec-card1.txt ($(wc -l < "$OUT_DIR/codec-card1.txt") linhas)"
else
    echo -e "${C_WARN}!! /proc/asound/card1/codec#0 não legível (verificar permissões)${C_END}"
fi

# ─── PASSO 3 — TEST A: plughw direto FIRST (antes PipeWire travar) ──
print_header "PASSO 3 — TEST A: arecord -D plughw:1,0 (ALSA HW direto)"
echo "Esse é o caminho hardware-direto. Tem que dar RMS alto."
echo "Se BUSY aqui, PipeWire não soltou o device — preciso de info adicional."
echo ""
echo -e "${C_OK}>>> FALE 'um dois três quatro' por 3 segundos quando começar <<<${C_END}"
sleep 1
arecord -D plughw:1,0 -d 3 -f S16_LE -r 16000 -c 1 "$OUT_DIR/A-plughw-mono16k.wav" 2>&1 | tail -3
analyze_mono "$OUT_DIR/A-plughw-mono16k.wav"

# ─── PASSO 4 — restart PipeWire de novo (libera device se travou) ─
print_header "PASSO 4 — Restart PipeWire (release device pré-TEST B)"
systemctl --user restart wireplumber pipewire pipewire-pulse
sleep 4

# ─── PASSO 5 — TEST B: arecord -D default (PipeWire) ──────────────
print_header "PASSO 5 — TEST B: arecord -D default (via PipeWire)"
echo "Esse é o MESMÍSSIMO caminho que Sovyx usa."
echo ""
echo -e "${C_OK}>>> FALE 'um dois três quatro' por 3 segundos quando começar <<<${C_END}"
sleep 1
arecord -D default -d 3 -f S16_LE -r 16000 -c 1 "$OUT_DIR/B-default-mono16k.wav" 2>&1 | tail -3
analyze_mono "$OUT_DIR/B-default-mono16k.wav"

# ─── PASSO 6 — TEST C: stereo native via PipeWire ─────────────────
print_header "PASSO 6 — TEST C: arecord -D default STEREO 48k (native rate)"
echo "Captura stereo no rate native do codec (48k) sem nenhuma conversão."
echo ""
echo -e "${C_OK}>>> FALE 'um dois três quatro' por 3 segundos quando começar <<<${C_END}"
sleep 1
arecord -D default -d 3 -f S16_LE -r 48000 -c 2 "$OUT_DIR/C-default-stereo48k.wav" 2>&1 | tail -3
analyze_stereo "$OUT_DIR/C-default-stereo48k.wav"

# ─── PASSO 7 — snapshot config DEPOIS dos tests + diff ────────────
print_header "PASSO 7 — Snapshot pós-test + diff"
amixer -c1 > "$OUT_DIR/amixer-c1-AFTER.txt" 2>&1
diff "$OUT_DIR/amixer-c1-BEFORE.txt" "$OUT_DIR/amixer-c1-AFTER.txt" > "$OUT_DIR/amixer-DIFF.txt" 2>&1
if [[ -s "$OUT_DIR/amixer-DIFF.txt" ]]; then
    echo -e "${C_WARN}!! amixer state MUDOU durante os tests:${C_END}"
    cat "$OUT_DIR/amixer-DIFF.txt"
else
    echo -e "${C_OK}amixer state IGUAL antes/depois (PipeWire não mexeu nos controles).${C_END}"
fi

# ─── PASSO 8 — extrair info forense crítica do codec ──────────────
print_header "PASSO 8 — Codec state crítico (capture pins + connection mux)"
if [[ -r "$OUT_DIR/codec-card1.txt" ]]; then
    echo ""
    echo "── Pin Defaults relevantes para CAPTURE: ──"
    grep -B1 "Mic\|input\|Capture" "$OUT_DIR/codec-card1.txt" | grep -E "Pin Default|Pincap|Pin-ctls" | head -30
    echo ""
    echo "── Connection list para nodes ADC (capture): ──"
    grep -B2 -A1 "Connection:" "$OUT_DIR/codec-card1.txt" | head -40
    echo ""
    echo "── Amp-Caps Input (signal flow do mic): ──"
    grep "Amp-In caps\|Amp-In vals" "$OUT_DIR/codec-card1.txt" | head -10
fi

# ─── RESUMO FINAL ──────────────────────────────────────────────────
print_header "RESUMO"
echo "Artefatos em: $OUT_DIR"
ls -la "$OUT_DIR/"
echo ""
echo "Os WAVs estão lá pra inspeção visual se precisar (Audacity, etc.)."
echo ""
echo -e "${C_OK}Cole o output completo deste script no chat.${C_END}"
echo "Vou identificar o ponto exato onde o sinal morre."
