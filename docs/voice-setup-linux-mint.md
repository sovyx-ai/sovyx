# Voice Setup — Linux Mint (Sovyx ≥ v0.30.13)

Runbook operacional definitivo para colocar o sistema de voz da Sovyx funcionando em **Linux Mint 22 (zena, Ubuntu noble base)** com **PipeWire** — caso forense canônico: Sony VAIO + codec HD-Audio Generic SN6180.

> **Versão alvo:** v0.30.13 ou superior. Versões anteriores não têm os 5 fixes automáticos da silent-mic remediation mission.

---

## Sumário

1. [Pré-requisitos](#pré-requisitos)
2. [O que o Sovyx v0.30.13 faz automaticamente](#o-que-o-sovyx-v0-30-13-faz-automaticamente)
3. [Passo 1 — Upgrade pra v0.30.13](#passo-1--upgrade-pra-v0-30-13)
4. [Passo 2 — Validação baseline do mic](#passo-2--validação-baseline-do-mic)
5. [Passo 3 — Iniciar daemon](#passo-3--iniciar-daemon)
6. [Passo 4 — Dashboard + login](#passo-4--dashboard--login)
7. [Passo 5 — Onboarding wizard (5 sub-passos)](#passo-5--onboarding-wizard-5-sub-passos)
8. [Passo 6 — Configurar wake word (decisão UX)](#passo-6--configurar-wake-word-decisão-ux)
9. [Passo 7 — Validação end-to-end](#passo-7--validação-end-to-end)
10. [Passo 8 — Validação no painel Voz](#passo-8--validação-no-painel-voz)
11. [Como falar com a IA — resumo](#como-falar-com-a-ia--resumo)
12. [Checklist final](#checklist-final)
13. [Troubleshooting](#troubleshooting)
14. [Sequência rápida (TL;DR)](#sequência-rápida-tldr)

---

## Pré-requisitos

Sistema operacional + dependências:

| Item | Versão mínima | Como verificar |
|---|---|---|
| Linux Mint | 22 (zena) | `lsb_release -a` |
| Python | 3.11 ou 3.12 | `python3 --version` |
| pipx | qualquer | `pipx --version` |
| libportaudio2 | 19.6.0+ | `dpkg -l libportaudio2` |
| espeak-ng | 1.51+ | `dpkg -l espeak-ng` |
| PipeWire | 1.0+ | `pactl info \| grep "Server Name"` |

Instalar dependências do sistema (idempotente):

```bash
sudo apt-get install -y libportaudio2 espeak-ng
```

---

## O que o Sovyx v0.30.13 faz automaticamente

A partir de v0.30.13 o daemon resolve **sem intervenção manual** os 5 problemas canônicos do Linux+PipeWire silent-mic:

| Problema | Comportamento em v0.30.13 |
|---|---|
| Mic com Capture switch ALSA `[off]` | `LinuxALSACaptureSwitchBypass` auto-engaja switch + lifta Internal Mic Boost |
| WirePlumber default-source apontando para `.monitor`/muted/<5%vol | `LinuxWirePlumberDefaultSourceBypass` auto-reroteia |
| Endpoint quarentenado sem auto-recovery | `runtime_failover_on_quarantine` muda para próximo device |
| Wizard hint genérico ("check OS settings") | Hint Linux-específico com comandos `amixer` + `wpctl` exatos |
| Pipeline com `mind_id="default"` (phantom) | `resolve_active_mind_id_for_request` propaga mind_id real |

**Você precisa de muito menos intervenção manual em v0.30.13 do que em versões anteriores.**

---

## Passo 1 — Upgrade pra v0.30.13

```bash
pipx upgrade sovyx
sovyx --version
```

Esperado: `sovyx 0.30.13` (ou superior).

Se aparecer versão anterior, force a reinstalação com extras corretos:

```bash
pipx install --force "sovyx[voice,voice-quality,otel,plugins,search]"
```

---

## Passo 2 — Validação baseline do mic

Antes de mexer no Sovyx, confirme que o mic captura no nível ALSA puro. **Fale normalmente nos 3 segundos:**

```bash
arecord -D plughw:1,0 -d 3 -f S16_LE -r 16000 -c 1 /tmp/baseline.wav && \
  python3 -c "import wave,audioop; w=wave.open('/tmp/baseline.wav','rb'); d=w.readframes(w.getnframes()); print('RMS=', audioop.rms(d,2))"
```

> **Nota:** o `plughw:1,0` é específico para hosts onde o mic está na card 1 (típico do Sony VAIO + codecs HD-Audio Generic). Em outros hardwares pode ser `plughw:0,0`. Confirme com `arecord -l`.

| RMS retornado | Diagnóstico | Ação |
|---|---|---|
| **≥ 2000** | ✅ Mic saudável | Pula direto para [Passo 3](#passo-3--iniciar-daemon) |
| 500-2000 | ⚠️ Marginal mas funcional | Sovyx vai conseguir trabalhar; opcionalmente faça Passo 2.1 |
| **< 500** | ❌ Mic atenuado / mudo | **Faça Passo 2.1 antes de continuar** |

### Passo 2.1 (condicional) — Resetar gain ALSA + persistir

Se o RMS deu baixo, o mixer ALSA está atenuado:

```bash
amixer -c1 sset 'Capture' cap
amixer -c1 sset 'Capture' 80%
amixer -c1 sset 'Internal Mic Boost' 67%
sudo alsactl store 1
```

Re-rode o teste do Passo 2 — agora deve dar RMS ≥ 2000.

> **Por que `sudo alsactl store 1` é importante mesmo com `LinuxALSACaptureSwitchBypass` ativo:** o bypass conserta em runtime cada vez que detecta o problema. Mas a cada boot o mixer ALSA reseta antes do Sovyx subir, e o bypass só dispara quando a integrity probe detecta `driver_silent`. O `alsactl store` salva o estado correto em `/var/lib/alsa/asound.state` para o boot restaurar automaticamente, evitando o ciclo "reset → bypass conserta → reset → bypass conserta".

---

## Passo 3 — Iniciar daemon

```bash
sovyx start
```

O console exibe:

```
╔══════════════════════════════════════════════╗
║              Sovyx — Mind Engine             ║
╠══════════════════════════════════════════════╣
║  Dashboard:  http://127.0.0.1:7777            ║
║  Token:      <string-de-43-chars>             ║
╚══════════════════════════════════════════════╝
```

**Copie o token.** Você vai precisar para login no dashboard.

Se quiser recuperar o token depois: `sovyx token`.

---

## Passo 4 — Dashboard + login

Navegador → `http://127.0.0.1:7777` → cole o token na tela de login.

---

## Passo 5 — Onboarding wizard (5 sub-passos)

> ⚠️ **NUNCA edite `~/.sovyx/secrets.env` ou `mind.yaml` manualmente.** O wizard faz tudo certo: validação de chave LLM, persistência com `chmod 600`, hot-register no router em runtime, atualização do mind.yaml. Editar manualmente é fallback para servidor headless, não path operacional.

### 5.1 — "Escolha seu Cérebro" (LLM provider)

Selecione UM provider:

| Provider | Quando usar | Custo | Latência |
|---|---|---|---|
| **Anthropic (Claude)** | Recomendado — melhor qualidade | API paga | ~1s |
| **OpenAI (GPT)** | Alternativa boa | API paga | ~2s |
| **Google (Gemini)** | Alternativa | API paga | ~2s |
| **Ollama (local)** | 100% local, sem custo | Zero | ~3-5s |

**Para cloud (Anthropic/OpenAI/Google):**
1. Cole sua API key no campo
2. Clique em **"Testar conexão"** (Sovyx faz uma chamada real para validar)
3. Submit

**Para Ollama (local):**

Em outro terminal:
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:3b
```

Depois selecione "Ollama" no wizard, deixe API key vazia, submit.

✅ **Quando passa:** o backend escreve `~/.sovyx/secrets.env` automaticamente (chmod 600), atualiza `~/.sovyx/<mind-id>/mind.yaml` com `llm.default_provider` + `default_model` selecionados, e hot-registra o provider no router em memória sem reiniciar daemon.

### 5.2 — "Conheça {{name}}" (Personalidade)

- **Nome da mente:** ex.: "Jonny" (escolha qualquer nome)
- **Idioma:** **CRÍTICO — escolha "English (en)"**

> ❗ **Por que NÃO escolher "Português (pt-BR)":** o STT padrão (Moonshine v2) **não suporta português**. Suporta apenas `ar/en/es/ja/ko/uk/vi/zh`. Em v0.30.13 o sistema loga `voice.factory.stt_language_unsupported` e cai pra inglês automaticamente — funcionalmente é o mesmo que escolher "en" diretamente, mas com WARN no log.
>
> **Alternativa parcial:** escolha **"Español (es)"** — Moonshine fala espanhol e aceita ~70% de português brasileiro com sotaque carregado. Útil se você fala português pausadamente, mas sujeito a falhas em palavras com sons exclusivos do PT (`ã`, `õ`, `lh`, `nh`).
>
> **TTS pode ser português** mesmo se você fala inglês — o Kokoro tem voz `pf_dora` que fala português brasileiro. Configure separadamente em `mind.yaml` se quiser:
> ```yaml
> voice:
>   voice_id: "pf_dora"
> ```

### 5.3 — "Conectar Canais" (Telegram, etc.)

Pule este passo (clique em **"Skip"** / **"Pular"**). Telegram não importa para validar voz.

### 5.4 — "Configurar Voz" (o assistente que falhou em v0.30.8)

Aqui está o `VoiceSetupWizard`. 4 sub-passos internos:

#### Sub-passo A — Selecionar microfone

A lista mostra cada device PortAudio:

- `default` (passa pelo PipeWire) — **escolha esse**
- `pipewire` (alternativo direto)
- `HD-Audio Generic: SN6180 Analog (hw:1,0)` — mic físico direto. **Provavelmente vai falhar** com `PaErrorCode -9985` porque o PipeWire reservou exclusivamente

Clique em **"default"**.

#### Sub-passo B — "Iniciar gravação de 3 segundos"

Clica e fala normalmente nos 3 segundos.

Cenários possíveis:

| Resultado | O que significa | Ação |
|---|---|---|
| ✅ "Recording looks good." (verde) com peak ≥ -30 dBFS | Tudo certo | Prossiga para sub-passo C |
| ⚠️ "Sinal muito baixo" (`low_signal`) | Fale mais alto OU verifique mic não coberto | "Try again" |
| ❌ "Nenhum áudio capturado" (`no_audio`) | Aparece o hint Linux-específico (T2.7) com 3 comandos | Execute eles em outro terminal, depois "Try again" |
| ❌ "Falha de gravação" (`device_error`) | Provavelmente selecionou device 4 (físico reservado por PipeWire) | "Try again" e selecione "default" |

#### Sub-passo C — Resultados

Você vê RMS, peak, SNR e diagnóstico final. Click em **"Salvar seleção"**.

#### Sub-passo D — "Concluído" / "Habilitar Voz"

Click no botão final.

### 5.5 — "Diga Olá" (Primeiro chat)

O wizard te coloca numa tela de chat **textual**. Ainda NÃO é teste de voz. Apenas digite "Hello" para confirmar que o LLM responde corretamente.

---

## Passo 6 — Configurar wake word (decisão UX)

Por padrão **wake word está desativado** (`MindConfig.wake_word_enabled = False` por backward compat com v0.27.x).

### Opção A — Sem wake word (recomendado para primeiro teste)

**Como funciona:**
1. Você fala em qualquer momento
2. VAD detecta onset de fala
3. Sovyx grava → STT → LLM → TTS responde

**Como falar:** abre a boca e fala normalmente, igual conversa.

**Risco:** ruídos do ambiente (TV, conversa paralela, latido de cachorro) podem ativar.

**Quando usar:** ambiente silencioso, primeiro teste, casa.

### Opção B — Com wake word (estilo Siri/Alexa)

Edite `~/.sovyx/<mind-id>/mind.yaml`:

```bash
nano ~/.sovyx/jonny/mind.yaml
```

Adicione/ajuste a seção `voice:`:

```yaml
voice:
  enabled: true
  language: en
  wake_word_enabled: true
  wake_word: "hey jonny"
```

Salve. Reinicie o daemon:
- Ctrl+C no terminal do `sovyx start`
- `sovyx start` de novo

**Como falar:**
1. **"Hey Jonny"** (em voz clara)
2. *beep* de confirmação
3. **"Que horas são?"** (ou seu pedido)

> Para criar uma palavra de ativação personalizada (treinar modelo ONNX para sua voz específica): dashboard → Voz → botão **"Treinar palavra de ativação"** (~5 minutos).

**Recomendação para primeira validação:** **Opção A** (sem wake word). Mais simples para confirmar que o pipeline inteiro funciona. Depois flipe para B se quiser produção.

---

## Passo 7 — Validação end-to-end

Em um segundo terminal, monitore os eventos críticos:

```bash
tail -f ~/.sovyx/logs/sovyx.log | grep -E "voice\.vad\.frame|voice_pipeline_heartbeat|audio_capture_heartbeat|voice\.stt\.|voice\.llm\.|voice\.tts\.|voice\.failover\.|voice\.bypass\."
```

No primeiro terminal o daemon está rodando. Volta para a UI do dashboard, vai para a aba **Voz**.

**Diga em voz normal (em inglês — `language: en`):**

> "What time is it?"

Sequência esperada nos logs (em segundos):

```
voice.vad.frame voice.probability=0.7 voice.state=SPEECH      ← VAD detectou
voice_pipeline_heartbeat state=RECORDING mind_id=jonny         ← gravando
[3-5s falando]
voice_pipeline_heartbeat state=THINKING                        ← parou de falar
voice.stt.request                                               ← STT processando
voice.stt.response text="What time is it"                      ← transcrito!
voice.llm.request                                               ← chamou LLM
voice.llm.response                                              ← LLM respondeu
voice.tts.synthesis                                             ← Kokoro gerou áudio
[ouve a resposta no alto-falante]
```

**Latência total esperada** do fim da sua frase até começar a resposta falada: **2-5 segundos** (Anthropic ~1s, OpenAI ~2s, Ollama local ~3-5s).

---

## Passo 8 — Validação no painel Voz

Vá para a aba **Voz** no dashboard e confirme:

| Painel | Estado esperado quando você fala |
|---|---|
| **MICROFONE** | "Em execução", dB **-30 a -10** (não -83!), `silent_frames` baixo |
| **PIPELINE** | "Em execução", `mind_id=jonny` (NÃO `default`) |
| **DETECÇÃO DE ATIVIDADE DE VOZ** | "Habilitado" |
| **PALAVRA DE ATIVAÇÃO** | Conforme Passo 6 (Habilitada/Desabilitada) |
| **FALA-PARA-TEXTO** | Engine MoonshineSTT, Estado **ready** |
| **TEXTO-PARA-FALA** | Engine KokoroTTS, Inicializado **Sim** |
| **Capture SNR distribution** | Sai de "Warming up" depois de ~10s de fala |

---

## Como falar com a IA — resumo

| Configuração | Como você fala |
|---|---|
| `wake_word_enabled: false` (default) | Apenas abre a boca e fala normalmente. *"What time is it?"* — IA responde |
| `wake_word_enabled: true` + `wake_word: "hey jonny"` | *"Hey Jonny"* → *beep* → *"What time is it?"* |

**Idioma:** **inglês** para 100% transcrição confiável. **Espanhol** para tentar português ~70%. Português puro NÃO funciona (Moonshine é English-family-only, sem português).

**TTS pode ser em português** mesmo se você fala inglês — voz `pf_dora` do Kokoro é português brasileiro. Configure em `mind.yaml`:

```yaml
voice:
  voice_id: "pf_dora"
```

---

## Checklist final

Marque cada item antes de declarar "funcionando":

- [ ] `sovyx --version` retorna `0.30.13` (ou superior)
- [ ] `arecord -D plughw:1,0 -d 3 ... RMS=` ≥ 2000 falando normal
- [ ] Onboarding wizard completou todos os 5 passos sem erro
- [ ] Test microphone passou com `diagnosis="ok"`
- [ ] Dashboard `/voice` mostra MICROFONE -30 a -10 dB quando você fala
- [ ] Dashboard `/voice` mostra `mind_id=<seu-mind>` (NÃO `default`)
- [ ] Falando "What time is it?" produz resposta falada em ≤ 5s
- [ ] Logs mostram a cadeia VAD → STT → LLM → TTS sem erros

Se TODOS os itens estão ✅, o sistema de voz está **impecável**.

---

## Troubleshooting

### Sintomas e fixes específicos para v0.30.13

| Sintoma | Diagnóstico | Fix |
|---|---|---|
| Mic ainda em -83 dB no dashboard | Bypass strategy não disparou (eligibility falhou) | Confirme tuning não foi sobrescrito: `env \| grep SOVYX_TUNING` deve estar vazio. Se tem env vars setadas, remova |
| Log mostra `voice.bypass.applied strategy=linux.alsa_capture_switch` mas mic continua silente | Bypass aplicou em runtime mas não persistiu para próximo boot | Rode Passo 2.1 (`sudo alsactl store 1`) para persistir |
| Log mostra `voice.failover.attempted` repetidamente | Pipeline está oscilando entre devices ruins | Todos os candidates podem estar quebrados. Use `wpctl status` + `wpctl set-default <ID-real-mic>` manualmente |
| Wizard "Test microphone" trava 30s + dá timeout | PipeWire/PulseAudio congelado | `systemctl --user restart wireplumber.service` |
| Voz funciona mas STT transcreve coisas erradas | Você está falando português com `language: en` | Mude para `es` em mind.yaml ou fale em inglês |
| LLM retorna erro 401/403 | API key inválida ou expirada | Refaça onboarding step 1 OU edite manualmente `~/.sovyx/secrets.env` |
| Resposta é texto mas não toca áudio | Kokoro não inicializou | Logs: `KokoroTTS initialized` deve aparecer no boot. Se ausente: `pipx install --force "sovyx[voice,voice-quality,otel,plugins,search]"` |
| `no_llm_provider_detected` no boot | Você não completou o onboarding step 1 | Abra `127.0.0.1:7777` e faça o passo "Escolha seu Cérebro" |

### Escape hatch — desativar todos os bypasses se algo der errado

Se observar comportamento estranho dos novos bypasses (T2.1, T2.2) ou do hot-failover (T2.6), reverta para v0.30.9 behavior via env vars:

```bash
SOVYX_TUNING__VOICE__LINUX_WIREPLUMBER_DEFAULT_SOURCE_BYPASS_ENABLED=false \
SOVYX_TUNING__VOICE__LINUX_ALSA_CAPTURE_SWITCH_BYPASS_ENABLED=false \
SOVYX_TUNING__VOICE__RUNTIME_FAILOVER_ON_QUARANTINE_ENABLED=false \
sovyx start
```

Ou manter strict mode mas só com telemetria (não muta):

```bash
SOVYX_TUNING__VOICE__LINUX_WIREPLUMBER_DEFAULT_SOURCE_BYPASS_LENIENT=true \
SOVYX_TUNING__VOICE__LINUX_ALSA_CAPTURE_SWITCH_BYPASS_LENIENT=true \
sovyx start
```

---

## Sequência rápida (TL;DR)

```bash
# 1. Upgrade
pipx upgrade sovyx
sovyx --version  # → 0.30.13

# 2. Validar mic (fale durante 3s)
arecord -D plughw:1,0 -d 3 -f S16_LE -r 16000 -c 1 /tmp/t.wav && \
  python3 -c "import wave,audioop; w=wave.open('/tmp/t.wav','rb'); d=w.readframes(w.getnframes()); print('RMS=', audioop.rms(d,2))"

# 3. (CONDICIONAL) Se RMS < 2000:
amixer -c1 sset 'Capture' cap
amixer -c1 sset 'Capture' 80%
amixer -c1 sset 'Internal Mic Boost' 67%
sudo alsactl store 1

# 4. Iniciar daemon
sovyx start

# 5. Browser → 127.0.0.1:7777 → cola token
# 6. Onboarding wizard EM ORDEM:
#    Passo 1 → LLM provider + API key
#    Passo 2 → Nome "Jonny" + idioma "English (en)"  ← CRÍTICO escolher en
#    Passo 3 → SKIP (Telegram)
#    Passo 4 → Test microphone (default device) → Test speakers → Habilitar
#    Passo 5 → Diga olá

# 7. Em outro terminal, monitorar:
tail -f ~/.sovyx/logs/sovyx.log | grep -E "voice\.vad\.frame|voice\.stt\.|voice\.tts\."

# 8. Voltar ao dashboard, abrir aba Voz, FALAR EM INGLÊS:
#    "What time is it?"

# 9. Esperar 2-5s pela resposta falada. Se ouvir = funcionando.
```

---

## Referências

- **Mission spec:** `docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md`
- **Operator debt entry:** D24 em `docs-internal/OPERATOR-DEBT-MASTER-2026-05-03.md`
- **CLAUDE.md anti-patterns relevantes:** #21 (Windows APO — analogia para Linux), #28 (cold-probe signal validation), #35 (cross-layer config sentinels)
- **Tags relacionadas:** v0.30.9 (Phase 1) → v0.30.10 (hot-failover) → v0.30.11 (wizard hint) → v0.30.12 (Linux bypass strategies) → v0.30.13 (strict-mode promotion)

---

**Última revisão:** 2026-05-05 — após Phase 3 ship em v0.30.13.
**Mantenedor:** issues + atualizações via repo Sovyx.
