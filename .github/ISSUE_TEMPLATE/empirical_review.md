---
name: Empirical Constant Review
about: Mandatory periodic review of empirical constants from IMPLEMENTATION_PLAN.md §14 / docs-internal/EMPIRICAL_CONSTANTS.md
title: "[empirical-review] <constant_name> — {YYYY-MM-DD}"
labels: empirical-review, voice
---

## Constant under review
- **Name:** <e.g., probe_jitter_margin_s>
- **Registry entry:** [docs-internal/EMPIRICAL_CONSTANTS.md §14.E?](../../docs-internal/EMPIRICAL_CONSTANTS.md)
- **Current value:** <e.g., 0.5>
- **Introduced by:** <PR #, deploy date>
- **Horizon reached:** <deploy_date + N days>

## Data collection (fill before closing)
- [ ] Metric name: <e.g., sovyx.voice.health.bypass.probe_wait_ms>
- [ ] Observed p50 / p95 / p99: <values from Prometheus / Grafana>
- [ ] Trigger threshold crossed? <y/n + details>
- [ ] Related counters:
  - [ ] `sovyx_voice_health_bypass_probe_window_contaminated` (label: strategy)
  - [ ] `sovyx_voice_health_bypass_improvement_resolution` (label: strategy)
- [ ] Window: <30 / 60 / 90 days>

## Decision (tick exactly one)
- [ ] **Ratify current value** — trigger not crossed; the constant holds
- [ ] **Adjust to N** — trigger crossed; PR: <URL>
- [ ] **Demote to config-only default** — value is now user-tunable
      without further mandatory review

## Next review
- Mandatory re-review date: <this review date + 90 or 180 days per §14>
- Issue auto-created? <y/n + URL>

---

*Do not close this issue without ticking a Decision and scheduling the
next review. Silence is not evidence; time-boxed re-examination is.*
