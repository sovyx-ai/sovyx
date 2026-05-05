#!/usr/bin/env bash
# voice-diag-pipewire.sh — diferencial diagnostic for Linux+PipeWire silent-mic
#
# Roda 3 testes de captação de áudio em PATHS DIFERENTES para isolar
# onde a atenuação acontece entre o codec ALSA hardware e o caminho
# PortAudio/PipeWire que o Sovyx usa.
#
# - Test 1: arecord -D default     → SAME path Sovyx uses (alsa-pulse → PipeWire)
# - Test 2: arecord -D default 2ch → analisa L/R separadamente para detectar
#                                    cancelamento de fase ou canal silente
# - Test 3: arecord -D plughw:1,0  → controle, ALSA direto (sabemos que funciona)
#
# Compara os RMS e identifica em qual camada o sinal é perdido.
#
# Uso:
#   curl -fsSL https://raw.githubusercontent.com/sovyx-ai/sovyx/main/scripts/voice-diag-pipewire.sh \
#     -o /tmp/voice-diag.sh && bash /tmp/voice-diag.sh
#
# Pré-requisito: pare o sovyx antes de rodar (Ctrl+C no terminal do `sovyx start`)
# para não competir pelo device.

set -uo pipefail

readonly OUT_DIR="/tmp/sovyx-voicediag"
mkdir -p "$OUT_DIR"

# Colors for terminal output (graceful degrade if no tty)
if [[ -t 1 ]]; then
    readonly C_HEADER='\033[1;36m'
    readonly C_PROMPT='\033[1;33m'
    readonly C_OK='\033[1;32m'
    readonly C_WARN='\033[1;31m'
    readonly C_END='\033[0m'
else
    readonly C_HEADER='' C_PROMPT='' C_OK='' C_WARN='' C_END=''
fi

print_header() {
    echo ""
    echo -e "${C_HEADER}═══════════════════════════════════════════════════════════════${C_END}"
    echo -e "${C_HEADER} $* ${C_END}"
    echo -e "${C_HEADER}═══════════════════════════════════════════════════════════════${C_END}"
}

print_prompt() {
    echo -e "${C_PROMPT}>>> $* <<<${C_END}"
}

# Captura áudio + retorna RMS via Python audioop. Aceita o WAV path como arg.
analyze_mono_wav() {
    local wav="$1"
    python3 - "$wav" <<'PY'
import sys, wave, audioop
path = sys.argv[1]
with wave.open(path, 'rb') as w:
    data = w.readframes(w.getnframes())
    sw = w.getsampwidth()
    nframes = w.getnframes()
    rate = w.getframerate()
    nch = w.getnchannels()
    rms = audioop.rms(data, sw) if data else 0
    print(f"  duration_s = {nframes/rate:.2f}  rate={rate}Hz  channels={nch}  sampwidth={sw}B")
    print(f"  RMS = {rms}")
PY
}

# Análise de stereo: L, R, mono mix, e diff (cancelamento)
analyze_stereo_wav() {
    local wav="$1"
    python3 - "$wav" <<'PY'
import sys, wave, audioop
path = sys.argv[1]
with wave.open(path, 'rb') as w:
    data = w.readframes(w.getnframes())
    sw = w.getsampwidth()
    nframes = w.getnframes()
    rate = w.getframerate()
    nch = w.getnchannels()
    print(f"  duration_s = {nframes/rate:.2f}  rate={rate}Hz  channels={nch}  sampwidth={sw}B")
if not data:
    print("  (nenhum dado capturado)")
    sys.exit(0)
left  = audioop.tomono(data, sw, 1.0, 0.0)
right = audioop.tomono(data, sw, 0.0, 1.0)
mono_sum  = audioop.tomono(data, sw, 0.5, 0.5)
# audioop.add para subtração: subtrai sample-a-sample
mono_diff_bytes = audioop.add(left, audioop.mul(right, sw, -1.0), sw)
print(f"  Left  RMS         = {audioop.rms(left,  sw)}")
print(f"  Right RMS         = {audioop.rms(right, sw)}")
print(f"  Mono (L+R)/2 RMS  = {audioop.rms(mono_sum, sw)}   (downmix por soma)")
print(f"  Mono (L-R)   RMS  = {audioop.rms(mono_diff_bytes, sw)}   (cancelamento test)")
PY
}

# Reset do mic ao iniciar, garante estado conhecido
echo ""
echo -e "${C_HEADER}Sovyx voice diagnostic — Linux+PipeWire signal path${C_END}"
echo "Output dir: $OUT_DIR"
echo ""
echo "PRÉ-REQUISITO: o daemon sovyx deve estar PARADO (não competir pelo device)."
echo "Se ainda estiver rodando, dê Ctrl+C no terminal dele antes de continuar."
echo ""
read -p "Pressione ENTER quando estiver pronto para começar os 3 testes (vai pedir pra falar 3x)..."

# ─── Teste 1: arecord -D default (PipeWire pulse-shim, MONO 16k) ───
print_header "TESTE 1/3 — arecord -D default (caminho PipeWire/pulse-shim, MONO 16k)"
echo "Esse é o MESMO caminho que o Sovyx usa via PortAudio (alsa-pulse → PipeWire)."
echo ""
print_prompt "FALE durante 3 segundos quando o arecord começar..."
sleep 1
arecord -D default -d 3 -f S16_LE -r 16000 -c 1 "$OUT_DIR/test1-default-mono.wav" 2>&1 | tail -3
analyze_mono_wav "$OUT_DIR/test1-default-mono.wav"

# ─── Teste 2: arecord -D default STEREO 48k ───
print_header "TESTE 2/3 — arecord -D default STEREO 48k (analise L vs R)"
echo "Mesmo caminho do Test 1, mas captura 2 canais para detectar canal silente"
echo "ou cancelamento de fase no downmix stereo→mono."
echo ""
print_prompt "FALE durante 3 segundos quando o arecord começar..."
sleep 1
arecord -D default -d 3 -f S16_LE -r 48000 -c 2 "$OUT_DIR/test2-default-stereo.wav" 2>&1 | tail -3
analyze_stereo_wav "$OUT_DIR/test2-default-stereo.wav"

# ─── Teste 3: arecord -D plughw:1,0 (controle, ALSA direto) ───
print_header "TESTE 3/3 — arecord -D plughw:1,0 (controle, ALSA direto MONO 16k)"
echo "Esse é o caminho HARDWARE direto, bypassa PipeWire totalmente."
echo "Você já sabe que esse caminho funciona (RMS=8792 antes)."
echo "Refazendo agora para baseline comparável com Tests 1 e 2."
echo ""
print_prompt "FALE durante 3 segundos quando o arecord começar..."
sleep 1
arecord -D plughw:1,0 -d 3 -f S16_LE -r 16000 -c 1 "$OUT_DIR/test3-plughw-mono.wav" 2>&1 | tail -3
analyze_mono_wav "$OUT_DIR/test3-plughw-mono.wav"

# ─── Resumo ───
print_header "RESUMO"
echo "Os 3 WAVs ficam salvos em: $OUT_DIR/"
echo ""
echo "Cole o output completo deste script no chat para análise."
echo ""
echo "Interpretação:"
echo "  • Test 1 BAIXO + Test 3 ALTO → PipeWire pulse-shim atenuando (problema config)"
echo "  • Test 1 ALTO + Test 3 ALTO → PipeWire OK, problema é Sovyx-específico"
echo "  • Test 2 Left=alto, Right=baixo (ou vice-versa) → mic mono em só um canal stereo"
echo "  • Test 2 Mono(L+R) baixo MAS Mono(L-R) alto → cancelamento de fase no downmix"
echo "  • Todos baixos → algo na camada PipeWire/codec que ainda não isolamos"
echo ""
echo -e "${C_OK}Diagnóstico completo. Cole o output todo no chat.${C_END}"
