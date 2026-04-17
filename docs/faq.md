# FAQ

Common questions about Sovyx. If your question is not here, open a GitHub
Discussion or check the other docs in this directory.

## What is Sovyx?

Sovyx is a persistent AI companion that runs on your own machine. It ships as
a Python library, a CLI daemon, and a React dashboard. It has a real cognitive
loop (Perceive → Attend → Think → Act → Reflect), a graph-based long-term
memory (concepts, episodes, relations), a hybrid lexical + vector retrieval
layer, and a plugin system with a five-layer sandbox.

Practically, it is the bundle you get when you combine: an LLM router, a
durable SQLite-backed memory, a safety stack, voice I/O, a dashboard, and a
way to expose the whole thing over REST and WebSocket.

## How is Sovyx different from LangChain, LlamaIndex, or AutoGen?

Those are frameworks. You compose primitives and ship the resulting app
yourself. Sovyx is an application: it runs as a daemon, owns its database,
persists memory across restarts, has its own dashboard, and enforces a
cognitive loop with safety checkpoints. Plugins extend it; you do not wire it
together every time.

If you need a primitives library to build something new, use LangChain /
LlamaIndex. If you want a companion that remembers yesterday and boots
tomorrow, Sovyx is the application layer you would otherwise spend months
writing.

## How is Sovyx different from Siri, Alexa, Google Assistant, or Gemini?

Those are cloud assistants with short-lived context. Your conversation history
is controlled by the vendor. You cannot inspect, export, or run them
offline.

Sovyx runs locally by default, stores its memory in SQLite files on your
disk, supports multiple LLM providers (including local Ollama), and exposes
every piece of its state through a documented API. There is no opaque cloud
brain in the middle.

## Is Sovyx cloud or local?

Local by default. The daemon, the dashboard, and the database all run on
your machine. The LLM call is the only step that typically leaves the box,
and you choose the provider. A cloud tier exists for optional add-ons —
encrypted backups, a mobile relay, and a plugin marketplace — available as
part of Sovyx Cloud (separate commercial offering). Every cloud feature is
opt-in; the open-source daemon is fully functional without it.

## Does it work offline?

Yes, if you also run an offline LLM (for example Ollama with Llama 3.1 or
Qwen 2.5). The cognitive loop, the brain graph, persistence, the dashboard,
voice I/O, and plugins all operate without network access. Only features that
call third-party services (cloud LLMs, Telegram, calendar sync) need the
internet.

## Which LLM providers are supported?

The router supports ten providers: **Anthropic**, **OpenAI**, **Google** (Gemini), **Ollama**, **xAI** (Grok), **DeepSeek**, **Mistral**, **Together AI**, **Groq**, and **Fireworks AI**. All ten support both batch and streaming generation. Complexity-based routing (simple, moderate, complex) sends each request to an appropriate tier, and you can override per-mind or per-request. Adding a new OpenAI-compatible provider means ~30 LOC of configuration on top of the shared base class.

## Do I need a GPU?

For the LLM, it depends on the provider. Cloud providers need none. Local
Ollama runs on CPU for the smallest models but is far more pleasant with a
GPU. For voice, the default STT/TTS/VAD stack ships ONNX models that run on
CPU; a GPU improves latency but is never required.

## What data leaves my machine?

Only what you explicitly configure:

- **LLM requests** — if you use a cloud provider, the prompt and the
  retrieved context are sent to that provider. Use Ollama to keep everything
  local.
- **Bridge channels** — if you connect Telegram or Signal, inbound/outbound
  messages traverse those services.
- **Cloud backups** (opt-in) — ciphertext only, encrypted with Argon2id +
  AES-256-GCM before upload. The provider cannot decrypt them.

Nothing else. There is no telemetry call on startup, no background upload, no
training feedback loop.

## How is my data protected?

At rest, the SQLite files live under your data directory and inherit its
filesystem permissions. Dashboard and CLI tokens are stored with `0o600` on
disk. Backups are zero-knowledge: Argon2id for the KDF, AES-256-GCM for the
cipher, wire format `salt(16) || nonce(12) || ciphertext || tag(16)`.
Deleting the salt crypto-shreds the backup — which is how we satisfy GDPR
Article 17 cryptographically. License tokens are Ed25519 JWTs validated
locally with an embedded public key.

See `security.md` for the full posture.

## How do I back up my brain?

Two supported ways:

1. **Manual export** via the dashboard's `GET /api/export` endpoint or the
   CLI `sovyx export`. Produces a Sovyx Mind Format (SMF) archive that
   contains every concept, episode, relation, and configuration. You can
   restore it into a new mind with `sovyx import`.
2. **Encrypted cloud backups** on the cloud tier. Scheduled GFS retention
   (7 daily, 4 weekly, 12 monthly) with zero-knowledge encryption.

Both are driven by the same underlying serializer, so restoring from either
produces an identical mind.

## Can I run multiple minds?

Yes. Each mind has its own SQLite database, its own memory, its own
personality, and its own credentials. One daemon hosts one mind today. Full
multi-mind hosting under a single daemon with strong isolation is planned for
v1.0.

## Can I migrate from ChatGPT, Claude, or Gemini?

Yes — first-class importers exist for ChatGPT, Claude, Gemini, and Grok.
Drop the export file at the dashboard's import endpoint or the API:

```bash
curl -X POST -H "Authorization: Bearer $(sovyx token)" \
     -F platform=chatgpt -F file=@conversations.json \
     http://localhost:7777/api/import/conversations
# → 202 Accepted with a job_id; poll /api/import/{job_id}/progress
```

Each conversation is summary-encoded into one `Episode` plus extracted
`Concept` rows; re-importing the same archive is deduplicated via a SHA-256
key on the `conversation_imports` table. Obsidian vault import reads
Markdown files with YAML frontmatter, wiki links, and nested tags. For
anything else, you can still wrap raw text into the SMF schema and use
`sovyx mind import`.

## Can I export everything?

Yes. The Sovyx Mind Format archive produced by `GET /api/export` contains
every concept, episode, relation, conversation, personality setting, and
plugin manifest reference for the active mind. You own the data and can
move it between installations. Encrypted backups on the cloud tier use the
same format.

## Is Sovyx production-ready?

It depends on the definition of production. The cognitive core, brain,
persistence, observability, and dashboard are at version **0.16** and have
~8,780 tests behind them (~7,960 backend, ~820 frontend). We run Sovyx
ourselves every day. The public API surface is stable and SemVer'd. What is
labelled "planned" in `roadmap.md` is not yet implemented — treat it
accordingly.

The v1.0 cut is the general-availability milestone: it includes a third-party
security audit, a fuller sandbox, and a 99.9% cloud SLA.

## What is the license?

The library, the daemon, the dashboard, and all official plugins are licensed
under **AGPL-3.0-or-later**. Commercial services (Sovyx Cloud) are
proprietary and distributed as a separate package. Choosing Sovyx never
locks you into a hosted service; the core works standalone forever.

If AGPL is incompatible with your use case, contact us about a commercial
license.

## How do I fund the project?

Three paths:

1. **Subscribe to Sovyx Cloud** — it pays for the development of the
   open-source core too. See [sovyx.ai](https://sovyx.ai).
2. **Contribute code** — time and expertise are the scarcest resources.
   See `contributing.md`.

## Where do I report bugs?

GitHub Issues. Include a minimal reproduction, the expected and actual
behaviour, your Python version, and the output of `sovyx doctor`. The `sovyx
doctor` command also prints the last request id from the dashboard, which is
enough to correlate every log line for that request.

## Where do I get help?

- **Docs** — this directory. Start with `getting-started.md`,
  `architecture.md`, `api-reference.md`.
- **GitHub Discussions** — for design questions and open-ended conversations.
- **GitHub Issues** — for bug reports and concrete feature requests.
- **Community Discord** — the `#help` channel is linked from
  [sovyx.ai](https://sovyx.ai).
- **Security** — email `security@sovyx.ai`; do not open a public issue.
