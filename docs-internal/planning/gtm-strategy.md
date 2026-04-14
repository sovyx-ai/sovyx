# GTM Strategy — Go-to-Market

> **Scope**: posicionamento, audiência-alvo, canais de distribuição, plano de launch, pricing tiers, experimentos pendentes, e playbook de community building pré-launch.
>
> **Research base**: `SOVYX-VR-065-GTM-STRATEGY.md` (master synthesis), `SOVYX-VR-082-COMMUNITY-BUILDING-PRELAUNCH.md`, `SOVYX-VR-018..031` (canais), `SOVYX-VR-054..057` (counter-positioning), `SOVYX-BKD-PRD-002-PRICING-MONETIZATION.md`, `SOVYX-BKD-PLN-001-LAUNCH-PLAN.md`, `distribution-channels.md`, `sovyx-gtm-index.md`.

---

## 1. Positioning

### 1.1 Tagline e anti-pitch

De `sovyx-60-seconds.md`:

- **Tagline principal**: "Build a mind, not a bot."
- **Anti-pitch**: Não é chatbot, não é Alexa, não é Replika, não é coding agent.
- **Core**: "AI companion with real memory, local-first."

### 1.2 Counter-positioning contra incumbents

Aplicando o padrão Supabase (`VR-004`): nomear o incumbent frustrado + "Open Source X" = comprehension instantânea. Ver `docs/research/competitive-analysis.md` §1.

Candidatos testáveis (de `VR-010 §7 P1`):

- "Your brain, self-hosted"
- "AI that actually knows you"
- "Open Source Jarvis"
- "The AI companion that remembers"

Escolha final depende de tests de CTR em landing page — marcado `[PENDING — pre-launch experiment]`.

### 1.3 Diferenciais (priority order)

De `competitive-landscape.md`:

1. Memory (persistent episodic + semantic)
2. Privacy (100% local)
3. Open Source (AGPL-3.0)
4. Cost (free forever pessoal)
5. Control (no forced updates)
6. Voice (knows who's speaking — roadmap v0.6)
7. Universal (any hardware, any LLM)

---

## 2. Target audience

De `sovyx-60-seconds.md` §Pra Quem:

### 2.1 Personas primárias

- **Sovereign Technologist** — dev/crypto/devops que quer IA sem dependência de Big Tech. Persona #1.
- **Privacy-Conscious Family** — famílias que não querem filhos falando com cloud AI.
- **Small Business** — empresa que precisa IA local com compliance (LGPD/GDPR).

### 2.2 Intersection de comunidades self-hosted

`VR-010 §P5` identifica a intersection:

- r/selfhosted — 600K+ members.
- r/LocalLLaMA — 800K+ members.
- r/homeassistant — 600K+ members.

**Ninguém serve essa intersection hoje.** Sovyx é feito pra ela. Ver `distribution-channels.md` §Ollama ecosystem + §Home Assistant.

---

## 3. Distribution channels

Consolidado de `distribution-channels.md` (fonte: VR-048..053) + `VR-018..031` (canais online).

### 3.1 Plataformas de lançamento

Em ordem de launch day impact:

- **Hacker News** (VR-018) — "Show HN: Sovyx — Open Source Jarvis, runs on Pi 5". Alvo: top 3 front-page por 4+ horas. Supabase fez launch HN com 1,120 upvotes (2º mais popular da história).
- **Product Hunt** (VR-019) — PH launch no mesmo dia ou D+1. Alvo: top 3 do dia.
- **GitHub trending** (VR-017) — playbook de README-as-product (VR-033) pra conversão de visitors em stars.
- **Twitter/X** (VR-021) — thread com demo GIF, marcando influencers relevantes. Lista curada em `VR-025-TWITTER-INFLUENCERS.md`.
- **Reddit** (VR-020) — r/selfhosted, r/LocalLLaMA, r/homeassistant, r/privacy, r/opensource (com cuidado anti-spam — postar valor, não pitch).
- **YouTube tech channels** (VR-022, VR-026) — DMs pra ~20 channels selecionados 1 semana antes do launch. Pré-embargo em 5 influencers (VR-051: mandar 5 Pis com Sovyx pré-instalado = $300, highest ROI).
- **Newsletters/blogs** (VR-027) — Pragmatic Engineer, Changelog, SwissDev, etc.
- **Podcasts** (VR-028) — target de 3-5 appearances nos 60 dias pós-launch.

### 3.2 Package managers & distribuição passiva

Timeline (de `distribution-channels.md` §timeline):

```
D-14   DigitalOcean marketplace submit (2-4 wk review)
       OpenClaw ClaHub skill publish (parasitar)
D-Day  Docker Hub (sovyx/sovyx) + GHCR
       PyPI (pip install sovyx)
       Railway deploy button
       Render deploy button
       HACS (Home Assistant)
D+7    AUR (Arch)
       Hosted demo sovyx.ai/demo
       awesome-home-assistant PR
D+14   Snap (Ubuntu)
       Homebrew (macOS)
       Hetzner Apps (EU)
D+30   HA Add-on oficial
       Vultr marketplace
       Full OpenClaw integration guide
D+90+  HA core integration PR (requires proven track)
```

Cada package = página Google-indexed permanente = discovery passiva.

### 3.3 Hardware strategy

**No hardware Year 1** (anti-Mycroft — `VR-016 §AP-01`). Em vez disso:

- Pre-installed Pi image (`.img` flashable) — "flash and talk".
- 5 Pis pra influencers = $300, maior ROI marketing (VR-051).
- "Works Great on Pi 5" badge após testing comunitário.
- Lista Amazon de setup completo (~$130 total).
- Year 2+: Raspberry Pi Imager integration, hardware partnerships.

---

## 4. Launch Weeks pattern

Invenção da Supabase (VR-004), copiada por Vercel, Resend, Cal.com. Formato:

- **Build 3 meses → ship 5 features em 5 dias consecutivos.**
- Cada dia = blog post + demo video + tweet + HN/Reddit post.
- 5× o buzz de um big launch.
- Se 1 feature não pega, outra pega.
- Community antecipa, comenta, compartilha cada dia.

### 4.1 Sovyx "Genesis Week" — proposta (VR-010 §P6)

```
D1  Brain        — "A memória que lembra" (brain graph visualization demo)
D2  Voice        — "Conversa natural" (wake word + response em <2s)
D3  Home         — "Sovyx + Home Assistant" (integration showcase)
D4  Plugins      — "Ecossistema aberto" (SDK + marketplace preview)
D5  Full demo    — "Jarvis moment" (tudo junto em 10min video)
```

**Status**: `[PLANNED — v0.6 or v1.0 launch]`. Depende de Voice (v0.5 shipped), HA bridge (v0.6), Marketplace (v1.0).

---

## 5. Pricing — 6 tiers

De `src/sovyx/cloud/billing.py` + `sovyx-60-seconds.md` §Pricing + `SOVYX-BKD-PRD-002-PRICING-MONETIZATION.md`:

| Tier | Preço | O que inclui |
|---|---|---|
| **Free** | $0 forever | Engine + brain + 1 mind + Telegram + local LLM |
| **Sync** | $3.99/mo | Cloud backup + multi-device sync |
| **BYOK+** | $5.99/mo | Bring Your Own Key + advanced features |
| **Cloud** | $9.99/mo | LLM incluso (sem API key) + voice cloud |
| **Business** | $99/mo | 25 seats + SSO + audit + knowledge base |
| **Enterprise** | Custom | Per-seat, volume tiers, SLA |

Tiers reais no código (extraído de `src/sovyx/cloud/billing.py`):

```python
class SubscriptionTier(StrEnum):
    FREE = "free"
    SYNC = "sync"
    BYOK_PLUS = "byok_plus"
    CLOUD = "cloud"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"

TIER_PRICES: dict[SubscriptionTier, int] = {  # em cents/mês
    SubscriptionTier.FREE: 0,
    SubscriptionTier.SYNC: 399,
    SubscriptionTier.BYOK_PLUS: 599,
    SubscriptionTier.CLOUD: 999,
    SubscriptionTier.BUSINESS: 9900,
    # ENTERPRISE: custom (sem entry, `create_checkout` retorna contact-sales)
}
```

### 5.1 Princípios (de PRD-002 §1.1)

- **FREE MUST BE REAL** — Full companion, no artificial limits, no crippleware.
- **PAID MUST BE WORTH IT** — Cloud services add genuine value.
- **NO LOCK-IN** — Paid users can always downgrade to free.
- **TRANSPARENT COSTS** — Users see what they pay for (LLM tokens).
- **COMMUNITY FIRST** — Revenue funds development, not shareholders.
- **BYOK-FIRST** — User's own keys = zero LLM cost to Sovyx.
- **NEVER KILL BYOK** — Cursor killed BYOK em 2025 → backlash massivo (VR-094, VR-097). Sovyx monetiza **value layer** (routing, caching, analytics), não LLM access.

### 5.2 BYOK economics (insight central)

Do PRD-002 §1.2:

> Tradicional SaaS bundles model access ($20/mo). Sovyx inverte: user brings own key (BYOK) → marginal cost per Free user = $0.

Sovyx pode ter Free tier generoso sustentável porque não carrega custo de LLM.

---

## 6. Pricing experiments — `[NOT IMPLEMENTED]`

Gap reconhecido em `gap-analysis.md` §top gaps #5 + `IMPL-SUP-006-PRICING-PQL`. Nada disso existe no código ainda; marcado `[NOT IMPLEMENTED — v0.6]`.

### 6.1 Van Westendorp (4-question WTP)

Price Sensitivity Meter clássico (Van Westendorp 1976):

1. "At what price would this be too expensive you wouldn't consider buying?"
2. "At what price would it be too cheap you'd doubt the quality?"
3. "At what price would you start to think it's getting expensive?"
4. "At what price would you consider it a bargain?"

4 curvas se cruzam em 4 pontos: **Optimal Price Point (OPP)**, **Indifference Price Point (IPP)**, **Point of Marginal Cheapness (PMC)**, **Point of Marginal Expensiveness (PME)**.

### 6.2 Gabor-Granger (WTP direto)

Pergunta "buy at $X?" com preços decrescentes até encontrar o preço máximo que o respondente ainda aceita. Dá uma **demand curve** diretamente.

### 6.3 PQL (Product-Qualified Lead) scoring

Scoring automático baseado em comportamento:

- Frequência de uso.
- Features avançadas tocadas.
- Chegou ao "one key event" (ver §7).
- Heurística triggering upgrade modal no momento certo.

Também pendente: **FunnelTracker** pra medir conversão em cada step do trial.

**Cross-ref**: PRD-002 §VR-093 (Conversion Funnel), §VR-095 (Price Validation Framework).

---

## 7. "One Key Event" — activation

De `VR-004 §3` (Supabase playbook):

> Craft Ventures documented: the event that matters is creating the first database. Everything else flows from there.

Para Sovyx, candidatos de **one key event**:

- Primeira conversa por voz onde o assistente lembra algo pessoal.
- Brain graph atingindo 10+ concepts conectados.
- Primeiro momento "como ele sabe isso?" (retrieval mostrando surprise factor).

**Status**: `[EXPERIMENT PENDING]`. Métrica a adicionar ao dashboard e trackar em `cloud/analytics` pós-v0.6.

---

## 8. Community building — D-90 a D+30 playbook

De `VR-082-COMMUNITY-BUILDING-PRELAUNCH.md` (stub node). Fases:

### 8.1 D-90 a D-60 — Foundation

- Setup: Discord server + Twitter account + GitHub org + landing page.
- Recrutar 5-10 **beta testers** do círculo próximo (Guipe network).
- Publicar artigos técnicos no dev.to / hashnode que não mencionam Sovyx ainda — build reputação do autor.
- Começar a aparecer em subreddits relevantes **respondendo perguntas**, não auto-promovendo.

### 8.2 D-60 a D-30 — Content

- Serie de blog posts sobre topics adjacentes (voice assistants, local LLMs, memory systems).
- 3-5 thread-worthy tweets por semana sobre problemas que o projeto resolve.
- Começar a build público: tweet do progresso de código semanal.

### 8.3 D-30 a D-7 — Beta

- Convidar 20-50 beta testers da waitlist pra Discord privado.
- Ciclo de feedback → fix → next beta.
- Escrever documentação final.
- Preparar assets de launch: demo video 60s, screenshots, logo variants.

### 8.4 D-7 a D-1 — Prep

- Review de checklist (VR-060-PRELAUNCH-CHECKLIST).
- Finalizar landing page + preços.
- Agendar tweet threads.
- Coordenar com influencers pré-briefados.

### 8.5 D-Day

- HN post em janela de 7-9am PT (US working hours).
- Twitter thread 1h depois.
- Reddit posts escalonados (1 sub por hora).
- Product Hunt submission 12:01am PT.
- Ativar rede beta testers pra upvotes orgânicos (nunca comprar).

### 8.6 D+1 a D+30 — Amplification

Ver `VR-063-POST-LAUNCH-AMPLIFICATION.md`. Responder cada comentário, agradecer cada PR, shippar fixes rápido (Steinberger patcheou CVE em 24h — VR-001).

---

## 9. Anti-patterns a evitar

De `VR-016-FAILURE-SYNTHESIS.md` aplicado a GTM:

- **No fake stars** (AP-09). Star-farming = associação com malware. Otimizar métricas impossíveis de fakear.
- **No overpromise** (AP-10). README só mostra o que funciona hoje. Mycroft prometeu "Jarvis!" e entregou "less than Google Home".
- **No viral hype sem produto** — OpenClaw caso extremo (`VR-001`). Viralizou com CVE 1-click-RCE aberto. Build credibility, not just hype.
- **Community não é afterthought** (AP-11). Discord day 1. Issues <24h response. PRs <48h review.
- **No identity pivot** (AP-05). Consumer-first forever. Enterprise vira add-on, não pivot.

---

## 10. Success metrics (1st year post-launch)

Targets orientativos de `VR-059-METRICS-ANALYTICS` + `SOVYX-BKD-VR-098-UNIT-ECONOMICS`:

- **Stars**: 15K em 6 meses (compound growth Ollama-style, não spike OpenClaw-style).
- **PyPI downloads**: 10K+/mês.
- **Docker pulls**: 50K+ total.
- **GitHub contributors**: 25+.
- **Discord members**: 1K+ active.
- **Paid conversion**: 2-5% dos active users (industry benchmark open-core).
- **MRR**: target $10K em 12 meses (conservador — Pocketbase-level, não Supabase-level).
- **LTV/CAC**: >3 (unit economics saudável).
- **Churn**: <5%/mês (bom pra SaaS B2C).

---

## 11. Referências

Research base:

- `vps-brain-dump/memory/confidential/sovyx-bible/research/SOVYX-VR-065-GTM-STRATEGY.md` (master synthesis)
- `vps-brain-dump/memory/confidential/sovyx-bible/research/SOVYX-VR-082-COMMUNITY-BUILDING-PRELAUNCH.md`
- VR-017 GitHub Trending, VR-018 Hacker News, VR-019 Product Hunt, VR-020 Reddit, VR-021 Twitter/X
- VR-022 YouTube Tech, VR-026 YouTube Channels, VR-025 Twitter Influencers, VR-027 Newsletters/Blogs, VR-028 Podcasts
- VR-048..053 Distribution channels (HA, Ollama, OpenClaw, hardware, packages, cloud marketplace)
- VR-054..057 Counter-positioning (Siri/Alexa/Google/playbook)

Pricing specs:

- `vps-brain-dump/memory/confidential/sovyx-bible/backend/product/SOVYX-BKD-PRD-002-PRICING-MONETIZATION.md`
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-IMPL-SUP-006-PRICING-PQL.md`
- `src/sovyx/cloud/billing.py`

Knowledge graph nodes:

- `vps-brain-dump/memory/nodes/sovyx-gtm-index.md`
- `vps-brain-dump/memory/nodes/sovyx-60-seconds.md`
- `vps-brain-dump/memory/nodes/distribution-channels.md`
- `vps-brain-dump/memory/nodes/competitive-landscape.md`
- `vps-brain-dump/memory/nodes/sovyx-cloud-platform.md`

Cross-refs internas:

- `docs/research/competitive-analysis.md` §1, §3, §5
- `docs/research/llm-landscape.md` §3.3 (BYOK economics)
- `docs/planning/roadmap.md` §v0.6 (features that unlock GTM)
- `docs/planning/milestones.md`

---

_Última revisão: 2026-04-14._
