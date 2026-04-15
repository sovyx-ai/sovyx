# Roadmap

This is a rolling roadmap for Sovyx. Dates are best-effort and may shift; the
scope of each version is stable and only widens with explicit notes in release
announcements.

The current stable line is **v0.5**. The next major cut is **v0.6 — "The Mind
That Connects"**, followed by **v1.0 — "The Mind That Remembers"**.

---

## v0.11 — current

The v0.11 line shipped the enterprise-hardening pass plus a streak of
feature additions that closed several gaps originally targeted for v0.6:

- **DREAM phase** (v0.11.6) — nightly pattern discovery from recent
  episodes, derived concepts at low confidence, cross-episode Hebbian.
- **Interactive CLI REPL** (v0.11.7) — `sovyx chat` over the existing
  Unix-socket RPC, with prompt_toolkit history and slash commands.
- **Home Assistant integration** (v0.11.8) — first-party plugin, 4
  domains, 8 tools, REST-only.
- **CalDAV integration** (v0.11.9) — first-party plugin, 6 read-only
  tools, supports Nextcloud / iCloud / Fastmail / Radicale / SOGo /
  Baikal (Google discontinued CalDAV in 2023).
- **Conversation importers** (v0.11.4–5) — ChatGPT, Claude, Gemini.
  Obsidian remains for v0.6.

Active work:

- Documentation alignment (this set of files reflects v0.11.9).
- Clarifying three dashboard stubs (Voice, Emotions, Productivity)
  targeted at v0.6.
- Closing residual polish tickets ahead of v0.6 preview.

Release criteria for v0.5.x patch releases:

- Test coverage ≥ 95% (currently just above 96%).
- Zero `ruff` errors, zero `mypy --strict` errors, zero `bandit` HIGH.
- Multi-arch Docker build (`linux/amd64`, `linux/arm64`) green.
- Dashboard `npx tsc -b` with zero errors.
- Full pytest + vitest suite passing.

---

## v0.6 — "The Mind That Connects"

v0.6 is the connectivity cut. The theme is moving Sovyx out of the single
laptop and into the real communication surfaces people use every day. It also
pays down the main architectural divergences accumulated during v0.5.

### Planned — bridges and channels

- **Audio relay over WebSocket.** A relay client with Opus encoding, a 60 ms
  audio ring buffer, 16 kHz ↔ 48 kHz resampling, an offline queue, and
  exponential backoff on reconnect. Unlocks the mobile companion.

### Already shipped in v0.11 (was originally v0.6 scope)

- **Home Assistant** — shipped as a plugin in v0.11.8 (4 domains, 8 tools,
  REST). Architectural change from the original spec: HA is a device API,
  not a conversational channel, so it lives in `plugins/` rather than
  `bridge/`. WebSocket subscriptions for real-time state push remain a
  next-PR follow-up.
- **CalDAV** — shipped as a plugin in v0.11.9 (6 read-only tools). Same
  architectural rationale as HA. Incremental sync via `ctag` + `etag` is
  deferred to a follow-up; the v0 plugin reissues a full REPORT per
  refresh window (cheap enough — ~50 KB per request).

### Planned — data portability and onboarding

- **Obsidian importer.** Markdown with wiki links and YAML frontmatter,
  the fourth and final conversation importer. ChatGPT (v0.11.4), Claude
  (v0.11.5), and Gemini (v0.11.5) have already shipped under
  `sovyx.upgrade.conv_import` with a summary-first encoder and SHA-256
  dedup table on `brain.db`.
- **SMF exporter.** Complete Sovyx Mind Format export for data portability
  outbound.

### Planned — voice

- **Speaker recognition.** ECAPA-TDNN biometrics with enrollment and
  verification. Enables multi-user voice interactions.
- **Voice cloning.** Speaker adaptation as a premium feature.

### Planned — cognitive and emotional refinements

- **Emotional model migration to 3D PAD.** Move from the current 2D
  (valence + arousal) representation on episodes to a 3D model (pleasure,
  arousal, dominance) across both concepts and episodes, with a schema
  migration and optional backfill via LLM inference. Updates to importance
  weighting, consolidation, context assembly, and personality drift follow.
- **Configurable emotional baseline.** Per-mind baseline and homeostasis
  rate in `MindConfig`.
- **In-loop consolidation.** The existing `ConsolidationScheduler` runs
  every 6 h as a background job, which is what SPE-003 §1.1 actually
  specified ("periodic", dotted arrow). The earlier "in-loop" framing was
  redundant; consolidation stays as scheduler.

### Already shipped in v0.11 (was originally v0.6 scope)

- **Dream phase** — shipped in v0.11.6 as `brain/dream.py`. Time-of-day
  scheduler (default `02:00` in the mind's timezone), 1 LLM call per run
  extracts up to 5 recurring themes from the last 24 h of episodes,
  derives concepts at low confidence, attenuated cross-episode Hebbian
  on co-occurring concepts. Kill-switch via `dream_max_patterns: 0`.

### Planned — cloud and monetization

- **Stripe Connect.** Complete the integration beyond the current webhook
  surface: Express onboarding, destination charges, refunds, disputes,
  payouts, and tax. Unblocks the plugin marketplace revenue share.
- **Pricing experiments.** Van Westendorp and Gabor-Granger instrumentation
  for willingness-to-pay, PQL scoring, and funnel tracking.

### Planned — tooling

- **CLI admin utilities.** DB inspection, config reset, user and mind
  management.
- **Streaming LLM to TTS.** Pipeline token chunks into the speech pipeline
  for lower end-to-end latency.
- **BYOK token isolation.** Per-user API-key routing at the LLM layer.

### Already shipped in v0.11 (was originally v0.6 scope)

- **CLI REPL** — shipped in v0.11.7 as `sovyx chat`. Talks to the daemon
  over the existing JSON-RPC Unix socket (not HTTP), so the REPL works
  even when the dashboard is disabled. prompt_toolkit session with
  persistent history at `~/.sovyx/history` (chmod 0600), word-completer
  over the slash-command vocabulary, history search. Seven slash
  commands: `/help`, `/exit`, `/quit`, `/new`, `/clear`, `/status`,
  `/minds`, `/config`.

---

## v1.0 — "The Mind That Remembers"

v1.0 is the general-availability cut. The theme is stability, a published
plugin marketplace, and the deeper sandbox.

### Planned — sandbox hardening

- **Plugin sandbox v2.** A kernel-level enforcement layer on top of the
  existing five: seccomp-BPF on Linux, Linux namespaces (pid, net, mount),
  macOS Seatbelt, and Windows AppContainer tracked as an experiment.
- **Subprocess IPC for plugins.** Move plugins from in-process execution to
  isolated subprocesses with a JSON-RPC pipe.
- **Zero-downtime plugin update and rollback.** Blue-green atomic switch
  with automatic rollback on a post-upgrade health-check failure.

### Planned — platform

- **Optional Redis caching.** For multi-instance deployments and
  high-throughput workloads. Not required for single-user setups.
- **Full GDPR compliance suite.** Depends on the importers and exporter
  from v0.6 and on an audit-trail hardening pass.
- **Enterprise identity.** SSO (SAML 2.0, OIDC, LDAP), SCIM provisioning,
  and audit log.
- **Multi-mind daemon.** Multiple minds running under the same daemon with
  strong isolation boundaries.

### Planned — ecosystem

- **Public plugin marketplace.** Discoverable, permissioned, with signed
  manifests and a revenue share for paid plugins.
- **Third-party security audit.** External pen-test of the sandbox, the
  cognitive safety stack, the backup crypto, and the billing webhooks.
- **Foundation governance.** Dual-license structure with an independent
  foundation for long-term stewardship.

Release criteria for v1.0 extend the v0.5.x list with:

- Sandbox penetrated by an external security audit with no outstanding
  critical findings.
- At least fifty community plugins in the marketplace, or ten official
  plugins plus a working public SDK.
- 99.9% SLA measured over a quarter on the cloud tier.

---

## Quarterly timeline

Best effort with the current team size. Scope is the contract; dates are
signals.

| Quarter         | Milestone         | Main scope                                               |
| --------------- | ----------------- | -------------------------------------------------------- |
| **2026 Q2**     | v0.11.x           | Enterprise hardening + DREAM, REPL, HA, CalDAV, importers (shipped) |
| **2026 Q3**     | v0.6 preview      | Audio relay, Stripe Connect, Obsidian importer, speaker recognition |
| **2026 Q3–Q4** | v0.6 GA           | PAD 3D emotional migration, pricing experiments, BYOK isolation |
| **2026 Q4**     | v0.6 polish       | Streaming LLM→TTS, CLI admin utilities, voice cloning     |
| **2027 Q1**     | v1.0 preview      | Sandbox v2, subprocess IPC, multi-mind                   |
| **2027 Q1–Q2** | v1.0 GA           | Security audit, foundation, marketplace launch           |

---

## Design principles that will not change

These are load-bearing. They survive every roadmap review.

- **Local-first.** Default is local. Every core feature works offline. The
  cloud is always opt-in and never a requirement.
- **Bring your own key.** Users choose which LLM provider to use and supply
  their own credentials. Sovyx never forces a specific provider.
- **Open-core under AGPL-3.0.** The library, the daemon, the dashboard, and
  all official plugins are open source. Commercial services (hosted cloud,
  backups, marketplace) fund development without privatising the core.
- **Zero fake metrics.** We optimise for contributors, downloads, real users.
  Not stars, not vanity.
- **Underpromise, overdeliver.** The README reflects what already works.
  Roadmap items above are clearly marked as planned.
- **Multi-provider everywhere.** LLM, STT, TTS, bridge channels, storage.
  No vendor lock-in.
- **Ship iteratively.** Never a perpetual rewrite. Every week ends on a
  green `main`.
