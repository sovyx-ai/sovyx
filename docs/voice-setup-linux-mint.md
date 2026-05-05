# Sovyx — Instalação e Configuração de Voz no Linux Mint

Procedimento completo para uma instalação do zero do Sovyx no Linux Mint 22 com configuração de voz funcionando end-to-end. Assume que você está partindo de um sistema Mint recém-instalado, sem Sovyx prévio.

---

## Sumário

1. [Pré-requisitos do sistema](#1-pré-requisitos-do-sistema)
2. [Instalar Sovyx (primeira vez)](#2-instalar-sovyx-primeira-vez)
3. [Preparar o áudio do sistema](#3-preparar-o-áudio-do-sistema)
4. [Inicializar a mente](#4-inicializar-a-mente)
5. [Iniciar o daemon](#5-iniciar-o-daemon)
6. [Abrir o dashboard](#6-abrir-o-dashboard)
7. [Completar o onboarding](#7-completar-o-onboarding)
8. [Decisão sobre palavra de ativação](#8-decisão-sobre-palavra-de-ativação)
9. [Validar voz end-to-end](#9-validar-voz-end-to-end)
10. [Checklist final](#10-checklist-final)
11. [Troubleshooting](#11-troubleshooting)
12. [Como falar com a IA](#12-como-falar-com-a-ia)
13. [Referência rápida](#13-referência-rápida)

---

## 1. Pré-requisitos do sistema

### 1.1 — Atualizar pacotes

```bash
sudo apt-get update
```

### 1.2 — Verificar Python 3.12

```bash
python3 --version
```

Esperado: `Python 3.12.x`. Se aparecer 3.10 ou 3.11, instale 3.12:

```bash
sudo apt-get install -y python3.12 python3.12-venv
```

### 1.3 — Instalar pipx

`pipx` é o gerenciador de aplicações Python isoladas. Sovyx instala via pipx (não via pip direto).

```bash
sudo apt-get install -y pipx
pipx ensurepath
```

Reabra o terminal (ou rode `source ~/.bashrc`) para que o `~/.local/bin` entre no PATH.

Confirme:
```bash
pipx --version
```

### 1.4 — Instalar dependências de áudio

```bash
sudo apt-get install -y libportaudio2 espeak-ng
```

- **libportaudio2** — biblioteca de áudio cross-platform que o Sovyx usa para abrir streams de captura/reprodução
- **espeak-ng** — engine de síntese de voz (fallback caso o Kokoro/Piper não estejam disponíveis)

### 1.5 — Verificar PipeWire

Mint 22 já vem com PipeWire por padrão. Confirme que está rodando:

```bash
pactl info | grep "Server Name"
```

Esperado: `Server Name: PulseAudio (on PipeWire X.Y.Z)` — note "on PipeWire", confirmando que é PipeWire e não PulseAudio puro.

Se aparecer só `PulseAudio` sem mencionar PipeWire, instale:

```bash
sudo apt-get install -y pipewire pipewire-pulse wireplumber
systemctl --user enable --now pipewire pipewire-pulse wireplumber
```

---

## 2. Instalar Sovyx (primeira vez)

```bash
pipx install "sovyx[voice,voice-quality,otel,plugins,search]"
```

Os extras entre colchetes habilitam:
- **voice** — Moonshine STT, Kokoro TTS, Silero VAD
- **voice-quality** — AGC2, AEC, noise suppression
- **otel** — observabilidade OpenTelemetry
- **plugins** — sistema de plugins
- **search** — busca semântica no cérebro

Confirme:

```bash
sovyx --version
```

Esperado: `sovyx 0.30.13` (ou superior).

---

## 3. Preparar o áudio do sistema

Antes de subir o daemon, valide que o microfone realmente capta voz no nível ALSA puro. Isso evita perder tempo debugando o Sovyx quando o problema está no driver.

### 3.1 — Identificar a card do mic

```bash
arecord -l
```

Você verá algo assim:
```
**** List of CAPTURE Hardware Devices ****
card 1: Generic [HD-Audio Generic], device 0: ALC256 Analog [ALC256 Analog]
```

Anote o **número da card** (no exemplo, `card 1`). Em laptops modernos quase sempre é card 1 (card 0 é HDMI).

### 3.2 — Testar captação direta

Substitua `1` pelo número da sua card no comando abaixo. **Fale normalmente durante os 3 segundos**:

```bash
arecord -D plughw:1,0 -d 3 -f S16_LE -r 16000 -c 1 /tmp/sovyx-mic-test.wav && \
  python3 -c "import wave, audioop; w=wave.open('/tmp/sovyx-mic-test.wav','rb'); d=w.readframes(w.getnframes()); print('RMS=', audioop.rms(d,2))"
```

Resultado esperado:

| RMS | Significado | Próximo passo |
|---|---|---|
| **≥ 2000** | Mic saudável | Vai para **Passo 4** |
| 500–2000 | Marginal mas funcional | Vai para **Passo 4** (opcionalmente faça 3.3 antes) |
| **< 500** | Mic atenuado ou em mute | Faça **Passo 3.3** abaixo |

### 3.3 — (Condicional) Resetar gain do mixer ALSA + persistir

Se RMS deu baixo, o mixer ALSA está atenuado. Substitua `1` pela sua card:

```bash
amixer -c1 sset 'Capture' cap
amixer -c1 sset 'Capture' 80%
amixer -c1 sset 'Internal Mic Boost' 67%
sudo alsactl store 1
```

O `sudo alsactl store 1` salva o estado do mixer em `/var/lib/alsa/asound.state` para sobreviver ao reboot. Sem isso, no próximo boot o gain reverte e o mic volta a ficar silencioso.

Re-rode o teste do passo 3.2 — agora deve dar RMS ≥ 2000.

### 3.4 — Verificar roteamento PipeWire (preventivo)

```bash
pactl get-default-source
```

O resultado **NÃO** deve terminar em `.monitor`. Se terminar (ex: `alsa_output...analog-stereo.monitor`), o PipeWire está roteando o "default" para o monitor da saída em vez do mic. Veja qual é o source real do mic:

```bash
wpctl status
```

Procure a seção `Sources:` — você verá uma lista numerada. O mic real é a entrada `Family ... Analog` ou similar (não o `.monitor`). Anote o ID (número à esquerda).

Setar como default:

```bash
wpctl set-default <ID-do-mic-real>
pactl set-source-mute @DEFAULT_SOURCE@ 0
pactl set-source-volume @DEFAULT_SOURCE@ 80%
```

---

## 4. Inicializar a mente

Cada Sovyx hospeda uma ou mais "mentes". Crie a primeira:

```bash
sovyx init Jonny
```

Substitua `Jonny` pelo nome que preferir para sua IA. Esse comando cria:

- `~/.sovyx/system.yaml` — config do engine
- `~/.sovyx/jonny/mind.yaml` — config da mente (uma pasta por mente)
- `~/.sovyx/logs/` — diretório de logs

> **Não edite esses arquivos manualmente.** O wizard do dashboard vai populá-los corretamente.

---

## 5. Iniciar o daemon

```bash
sovyx start
```

O console exibe uma caixa parecida com:

```
╔══════════════════════════════════════════════╗
║              Sovyx — Mind Engine             ║
╠══════════════════════════════════════════════╣
║  Dashboard:  http://127.0.0.1:7777            ║
║  Token:      <string-de-43-caracteres>        ║
╠══════════════════════════════════════════════╣
║  Paste the token in the dashboard login.     ║
╚══════════════════════════════════════════════╝
```

**Copie o token** (clique-direito-copiar no terminal). Você vai precisar dele no próximo passo.

> Se precisar recuperar o token depois: em outro terminal, `sovyx token`.

---

## 6. Abrir o dashboard

No navegador (Chrome, Firefox, etc.):

```
http://127.0.0.1:7777
```

Cole o token na tela de login.

---

## 7. Completar o onboarding

Como é primeira execução, o dashboard te leva direto para `/onboarding`. São 5 passos sequenciais. **Faça em ordem, não pule passos.**

### Passo 7.1 — "Escolha seu Cérebro" (provider de LLM)

Sem provider de LLM configurado, a IA não consegue responder a nada. Escolha UM dos providers:

| Provider | Custo | Latência | Quando usar |
|---|---|---|---|
| **Anthropic (Claude)** | API paga | ~1s | Recomendado — melhor qualidade |
| **OpenAI (GPT)** | API paga | ~2s | Alternativa robusta |
| **Google (Gemini)** | API paga | ~2s | Alternativa |
| **Ollama (local)** | Zero | ~3-5s | 100% local, sem nuvem |

#### Para cloud (Anthropic / OpenAI / Google):

1. Selecione o provider na UI
2. Cole sua API key no campo
3. Clique em **"Testar conexão"** — Sovyx faz uma chamada real ao provider para validar
4. Submit

#### Para Ollama local:

Em outro terminal:
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:3b
```

Depois selecione "Ollama" na UI, deixe API key vazia, submit.

**O que acontece quando você submete:**
- A chave é gravada com permissão restrita (chmod 600)
- O provider é registrado em runtime sem precisar reiniciar daemon
- A escolha de provider/modelo é gravada no `mind.yaml`

### Passo 7.2 — "Conheça {{name}}" (personalidade + idioma)

- **Nome da mente:** confirme ou troque (já vem populado com o nome que você usou no `sovyx init`)
- **Idioma:** **CRÍTICO — escolha "English (en)"**

> ⚠️ **Por que NÃO escolher "Português (pt-BR)":**
>
> O motor de transcrição (Speech-to-Text) padrão **não suporta português**. Os idiomas suportados são: árabe, inglês, espanhol, japonês, coreano, ucraniano, vietnamita e chinês.
>
> Se você escolher pt-BR, o sistema automaticamente cai para inglês com um aviso no log. Funcionalmente é o mesmo que escolher "en" diretamente, mas com ruído nos logs.
>
> **Alternativa parcial:** escolha **"Español (es)"** — o motor aceita espanhol e consegue interpretar ~70% de português brasileiro **se você falar pausadamente e bem articulado**. Falha em palavras com sons exclusivos do PT (`ã`, `õ`, `lh`, `nh`).
>
> **A síntese de voz (Text-to-Speech) suporta português** mesmo se você fala em inglês — você pode receber respostas em PT mesmo falando EN. Configure a voz `pf_dora` mais à frente se quiser TTS em português.

### Passo 7.3 — "Conectar Canais" (Telegram, etc.)

**Pule este passo** (clique em "Skip" / "Pular"). Telegram não é necessário para validar voz.

### Passo 7.4 — "Configurar Voz"

Aqui está o assistente de voz. 4 sub-passos internos:

#### 7.4.A — Selecionar microfone

A UI mostra a lista de devices disponíveis. Você verá tipicamente:
- `default` (passa pelo PipeWire) — **escolha esse**
- `pipewire` (alternativo)
- `HD-Audio Generic: ... (hw:1,0)` — mic físico direto. **Provavelmente vai falhar** com erro "Device unavailable" porque o PipeWire reservou exclusivamente

Clique em **"default"**.

#### 7.4.B — "Iniciar gravação de 3 segundos"

Clique no botão e fale normalmente durante os 3 segundos.

Resultados possíveis:

| Diagnóstico | Significado | Ação |
|---|---|---|
| ✅ "Recording looks good." (verde) | Mic captou bem | Avance para 7.4.C |
| ⚠️ "Sinal muito baixo" | Volume baixo | Fale mais alto OU verifique se mic está coberto. "Try again" |
| ❌ "Nenhum áudio capturado" | Mixer está mudo OU PipeWire roteou errado | Aparece um hint com 3 comandos. Execute em outro terminal, depois "Try again" |
| ❌ "Falha de gravação" | Você selecionou device físico bloqueado | "Try again" e selecione "default" |

#### 7.4.C — Resultados

Você vê RMS, peak, SNR. Click em **"Salvar seleção"**.

#### 7.4.D — "Concluído" / "Habilitar Voz"

Click no botão final. O pipeline de voz inicia.

### Passo 7.5 — "Diga Olá" (primeiro chat)

O dashboard te leva para uma tela de chat **textual**. Ainda NÃO é teste de voz. Apenas digite "Hello" para confirmar que o LLM responde corretamente.

Onboarding concluído.

---

## 8. Decisão sobre palavra de ativação

Por padrão, o Sovyx fica escutando direto sem palavra de ativação. Você decide se quer manter assim ou usar wake word estilo Siri/Alexa.

### Opção A — Sem palavra de ativação (recomendado para primeiro teste)

**Como funciona:**
1. Você fala em qualquer momento
2. O detector de atividade de voz (VAD) detecta o início da fala
3. O sistema grava até você parar de falar
4. Transcreve, manda para o LLM, recebe resposta, sintetiza voz
5. Toca a resposta no alto-falante

**Como falar:** apenas abre a boca e fala normalmente, igual conversa.

**Risco:** ruídos do ambiente (TV, conversa paralela, latido) podem ativar o pipeline.

**Quando usar:** ambiente silencioso, primeiro teste, casa.

### Opção B — Com palavra de ativação ("Hey Jonny")

Edite o mind.yaml para ativar:

```bash
nano ~/.sovyx/jonny/mind.yaml
```

Procure a seção `voice:` e ajuste:

```yaml
voice:
  enabled: true
  language: en
  wake_word_enabled: true
  wake_word: "hey jonny"
```

Salve (Ctrl+O, Enter, Ctrl+X). Reinicie o daemon:
- Ctrl+C no terminal onde rodou `sovyx start`
- `sovyx start` de novo

**Como falar:**
1. Diga **"Hey Jonny"** em voz clara
2. Espere o *beep* de confirmação
3. Fale o pedido: **"Que horas são?"**
4. Aguarde a resposta

> Para criar uma palavra de ativação treinada na sua voz específica (não pré-treinada): no dashboard → aba Voz → botão **"Treinar palavra de ativação"**. Demora ~5 minutos.

**Recomendação para esta primeira validação:** **Opção A** (sem wake word). Mais simples para confirmar que o pipeline inteiro funciona. Depois pode flipar para B se quiser produção.

---

## 9. Validar voz end-to-end

Em um SEGUNDO terminal (deixe o daemon rodando no primeiro), monitore os eventos críticos em tempo real:

```bash
tail -f ~/.sovyx/logs/sovyx.log | grep -E "voice\.vad\.frame|voice_pipeline_heartbeat|voice\.stt\.|voice\.llm\.|voice\.tts\."
```

Volte ao dashboard, vá para a aba **Voz**.

**Diga em voz normal (em inglês, porque `language: en`):**

> "What time is it?"

A sequência esperada de logs (em ordem):

```
voice.vad.frame voice.probability=0.7 voice.state=SPEECH      ← VAD detectou voz
voice_pipeline_heartbeat state=RECORDING mind_id=jonny         ← gravando
[3 segundos falando]
voice_pipeline_heartbeat state=THINKING                        ← parou de falar, processando
voice.stt.request                                               ← STT iniciou
voice.stt.response text="What time is it"                      ← transcrição correta!
voice.llm.request                                               ← chamada para o LLM
voice.llm.response                                              ← resposta recebida
voice.tts.synthesis                                             ← TTS gerou áudio
[você ouve a resposta no alto-falante]
```

**Latência total esperada** (do fim da sua frase até começar a resposta falada):
- Anthropic: ~1-2 segundos
- OpenAI: ~2-3 segundos
- Ollama local: ~3-5 segundos

---

## 10. Checklist final

Marque cada item antes de declarar "funcionando":

- [ ] `sovyx --version` retorna a versão esperada
- [ ] `arecord -D plughw:N,0 -d 3 ...` retorna RMS ≥ 2000 falando normalmente
- [ ] Onboarding completou os 5 passos sem erro
- [ ] No passo 7.4.B "Test microphone" o diagnóstico foi "ok"
- [ ] No painel Voz do dashboard, MICROFONE mostra dB **entre -30 e -10** quando você fala (NÃO -83)
- [ ] No painel Voz, PIPELINE mostra `mind_id=<seu-mind>` (NÃO `default`)
- [ ] Ao falar "What time is it?" você ouve a resposta em ≤ 5 segundos
- [ ] Os logs mostram a cadeia completa VAD → STT → LLM → TTS sem erros

Se TODOS os itens estão ✅, o sistema está **impecável**.

---

## 11. Troubleshooting

| Sintoma | Diagnóstico provável | Como resolver |
|---|---|---|
| Mic continua em -83 dB no dashboard | Mixer ALSA não foi corrigido OU não persistiu | Refaça **passo 3.3** com `sudo alsactl store 1` |
| `arecord` direto funciona mas dashboard mostra silêncio | PipeWire roteou "default" errado | Refaça **passo 3.4** — `pactl get-default-source` + `wpctl set-default` |
| "Test microphone" trava por 30s e dá timeout | PipeWire ou WirePlumber congelou | `systemctl --user restart wireplumber.service` |
| Voz funciona mas STT transcreve coisas erradas | Você está falando português com `language: en` | Mude `language: es` no mind.yaml ou fale em inglês |
| LLM retorna erro 401 ou 403 | API key inválida ou expirada | Reabra dashboard → onboarding step 1 e cole nova chave |
| Resposta aparece como texto mas não toca áudio | TTS Kokoro não inicializou | Reinstale com extras corretos: `pipx install --force "sovyx[voice,voice-quality,otel,plugins,search]"` |
| `no_llm_provider_detected` no log de boot | Você não completou o passo 7.1 do onboarding | Abra o dashboard e configure o provider de LLM |
| Wizard mostra device 4 com erro "Device unavailable" | Device físico está reservado pelo PipeWire (esperado) | Selecione "default" em vez do device físico |

### Desativar bypasses se algo der errado

Se observar comportamento estranho, você pode reverter para um modo mais conservador via variáveis de ambiente. Pare o daemon (Ctrl+C) e reinicie assim:

```bash
SOVYX_TUNING__VOICE__LINUX_WIREPLUMBER_DEFAULT_SOURCE_BYPASS_ENABLED=false \
SOVYX_TUNING__VOICE__LINUX_ALSA_CAPTURE_SWITCH_BYPASS_ENABLED=false \
SOVYX_TUNING__VOICE__RUNTIME_FAILOVER_ON_QUARANTINE_ENABLED=false \
sovyx start
```

Isso desativa as estratégias automáticas que podem estar causando problemas. Você volta a precisar fazer os fixes do **passo 3** manualmente.

---

## 12. Como falar com a IA

Resumo das duas configurações possíveis:

### Sem palavra de ativação (`wake_word_enabled: false`)

Apenas abra a boca e fale normalmente. O sistema escuta o tempo todo e ativa quando detecta voz.

**Exemplo:**
> "What time is it?"

A IA responde em 2-5 segundos.

### Com palavra de ativação (`wake_word_enabled: true`)

Diga a wake word configurada (default `"hey jonny"` se você seguiu este guia), espere o beep, depois fale o pedido.

**Exemplo:**
> "Hey Jonny" *[beep]* "What time is it?"

### Sobre o idioma

| Configuração `language` | O que você fala | Qualidade |
|---|---|---|
| `en` (recomendado) | Inglês | 100% — transcrição perfeita |
| `es` | Português brasileiro pausado e articulado | ~70% — falha em ã/õ/lh/nh |
| `es` | Espanhol | 100% |
| `pt-br` | Qualquer | Funcional como `en` (sistema converte automaticamente com aviso) |

### Sobre a voz que a IA usa para responder

A IA pode responder com voz em português mesmo se você fala em inglês. Para configurar voz em português brasileiro, edite `mind.yaml`:

```bash
nano ~/.sovyx/jonny/mind.yaml
```

Adicione/ajuste:
```yaml
voice:
  voice_id: "pf_dora"
```

Salve, reinicie o daemon. Agora a IA responde com voz brasileira mesmo recebendo input em inglês.

---

## 13. Referência rápida

Sequência completa para copy/paste em ambiente novo:

```bash
# 1. Pré-requisitos
sudo apt-get update
sudo apt-get install -y pipx libportaudio2 espeak-ng
pipx ensurepath
# Reabra o terminal

# 2. Instalar Sovyx
pipx install "sovyx[voice,voice-quality,otel,plugins,search]"
sovyx --version

# 3. Validar mic — fale durante 3s, esperado RMS >= 2000
arecord -l                                    # descobre número da card
arecord -D plughw:1,0 -d 3 -f S16_LE -r 16000 -c 1 /tmp/t.wav && \
  python3 -c "import wave,audioop; w=wave.open('/tmp/t.wav','rb'); d=w.readframes(w.getnframes()); print('RMS=', audioop.rms(d,2))"

# 3.3 — (CONDICIONAL) se RMS < 2000:
amixer -c1 sset 'Capture' cap
amixer -c1 sset 'Capture' 80%
amixer -c1 sset 'Internal Mic Boost' 67%
sudo alsactl store 1

# 3.4 — (PREVENTIVO) verificar PipeWire default source não é monitor
pactl get-default-source                      # se terminar em .monitor → fixe abaixo
wpctl status                                  # descobre ID do mic real
wpctl set-default <ID>                        # substitui pelo ID do mic
pactl set-source-mute @DEFAULT_SOURCE@ 0
pactl set-source-volume @DEFAULT_SOURCE@ 80%

# 4. Inicializar mente
sovyx init Jonny

# 5. Iniciar daemon
sovyx start                                   # copie o token que aparece

# 6. Browser → http://127.0.0.1:7777 → cole o token

# 7. Onboarding wizard EM ORDEM (no dashboard):
#    Passo 1 → escolha LLM provider + cole API key + Testar conexão + Submit
#    Passo 2 → nome "Jonny" + IDIOMA "English (en)"  ← CRÍTICO
#    Passo 3 → SKIP (Telegram)
#    Passo 4 → Test microphone (selecione "default") → Test speakers → Habilitar
#    Passo 5 → digite "Hello" e veja LLM responder

# 8. (Opcional) Configurar wake word — edite mind.yaml e reinicie daemon

# 9. Em SEGUNDO terminal:
tail -f ~/.sovyx/logs/sovyx.log | grep -E "voice\.vad\.frame|voice\.stt\.|voice\.tts\."

# 10. Volte ao dashboard, aba Voz, FALE EM INGLÊS:
#     "What time is it?"

# 11. Esperar 2-5s pela resposta falada. Se ouvir = funcionando.
```

---

**Pronto.** Se passou pelo checklist do passo 10 com tudo verde, o sistema de voz está funcionando impecavelmente. Para alterar comportamento (mudar provider de LLM, trocar voz da IA, ativar wake word, etc.), use o dashboard ou edite o `mind.yaml` da sua mente.
