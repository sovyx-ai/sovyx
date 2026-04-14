# Roadmap

This is a rolling roadmap for Sovyx. Dates are best-effort and may shift; the
scope of each version is stable and only widens with explicit notes in release
announcements.

The current stable line is **v0.5**. The next major cut is **v0.6 — "The Mind
That Connects"**, followed by **v1.0 — "The Mind That Remembers"**.

---

## v0.5 — current

The v0.5 line is in polish. The cognitive core is approximately feature
complete: the Perceive → Attend → Think → Act → Reflect loop, the brain graph
with hybrid retrieval, context assembly, multi-provider LLM routing, the
WAL-based SQLite persistence, the full observability stack (structlog,
OpenTelemetry, Prometheus, health checks), the embedded FastAPI dashboard, and
the base cloud tier with backups and licensing.

Active work:

- Documentation rewrite (this set of files).
- i18n namespace consistency across dashboard pages.
- Clarifying three dashboard stubs (Voice, Emotions, Productivity) targeted
  at v0.6.
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
- **Home Assistant bridge.** A ten-domain entity registry, graduated action
  safety (safe, confirm, deny), mDNS discovery, and a reconnecting WebSocket
  client.
- **CalDAV sync.** Calendar adapter with `ctag` + `etag` incremental sync,
  RRULE expansion, and proper handling of `DATE` vs `DATE-TIME` timezones.

### Planned — data portability and onboarding

- **Conversation importers.** First-class importers for ChatGPT
  (`conversations.json`), Claude, Gemini, and Obsidian vaults (Markdown with
  wiki links). Removes the friction of coming from another assistant and
  covers GDPR Article 20 inbound.
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
- **In-loop consolidation.** Invoke the existing consolidation cycle from
  the cognitive loop on a cadence, so memory stays healthy without a
  separate background job.
- **Dream phase.** A nightly pattern-discovery pass that derives new
  concepts from recent episodes and refines Hebbian edges over a wider
  window.

### Planned — cloud and monetization

- **Stripe Connect.** Complete the integration beyond the current webhook
  surface: Express onboarding, destination charges, refunds, disputes,
  payouts, and tax. Unblocks the plugin marketplace revenue share.
- **Pricing experiments.** Van Westendorp and Gabor-Granger instrumentation
  for willingness-to-pay, PQL scoring, and funnel tracking.

### Planned — tooling

- **CLI REPL.** Interactive multi-line prompt with auto-complete and
  history.
- **CLI admin utilities.** DB inspection, config reset, user and mind
  management.
- **Streaming LLM to TTS.** Pipeline token chunks into the speech pipeline
  for lower end-to-end latency.
- **BYOK token isolation.** Per-user API-key routing at the LLM layer.

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
| **2026 Q2**     | v0.5.x polish     | Docs rewrite, i18n, stub wiring                          |
| **2026 Q3**     | v0.6 preview      | Audio relay, Stripe Connect, importers                   |
| **2026 Q3–Q4** | v0.6 GA           | Emotional 3D migration, consolidation/dream, voice auth  |
| **2026 Q4**     | v0.6 polish       | Home Assistant, CalDAV, pricing experiments              |
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
