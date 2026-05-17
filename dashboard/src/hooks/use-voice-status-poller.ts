/**
 * use-voice-status-poller — Mission C2 §T2.3 frontend circuit breaker.
 *
 * Wraps the 2 Hz polling loop the voice page (``pages/voice.tsx``)
 * used pre-mission to read ``GET /api/voice/status``. The
 * v0.43.1 forensic audit (§C2 + §H8) observed the pre-mission
 * loop hammered the backend 960× over 480 s with NO backoff while
 * the boundary 500'd every call — contributing to H4 (RSS growth
 * from ExceptionGroup retention) and H6 (~10 s frame-drop cadence
 * correlated with the 500 cadence).
 *
 * This hook adds an exponential 5xx backoff with a degraded-state
 * surface so a recurrence of the bug class cannot amplify into the
 * same observability storm.
 *
 * Mission anchor:
 * docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md §T2.3
 *
 * Mission C3 §T2.10 ADR closure (v0.45.6) — refactored to consume the
 * generic ``useApiPoller<S, T>`` hook (``use-api-poller.ts``) so both
 * dashboard pollers share one circuit-breaker implementation.
 * Behaviour invariant: BASELINE_INTERVAL_MS = 500 ms; the generic
 * multipliers (3× / 10× / 20×) yield 1500 / 5000 / 10000 ms for the
 * 2-3 / 4-10 / ≥11 5xx tiers — bit-identical to the pre-refactor
 * decision table. C2 calibration window F1/F4 measures BEHAVIOR
 * (5xx counts + degraded transitions), which is unchanged.
 */
import type { z } from "zod";

import { VoiceStatusResponseSchema } from "@/types/schemas";

import {
  DEGRADED_AFTER_5XX as _DEGRADED_AFTER_5XX,
  FIRST_BACKOFF_AFTER_5XX as _FIRST_BACKOFF_AFTER_5XX,
  SUSTAINED_BACKOFF_AFTER_5XX as _SUSTAINED_BACKOFF_AFTER_5XX,
  DEGRADED_MULTIPLIER,
  FIRST_BACKOFF_MULTIPLIER,
  SUSTAINED_BACKOFF_MULTIPLIER,
  useApiPoller,
} from "./use-api-poller";

/**
 * Inferred from the zod schema rather than imported from
 * ``types/api.ts`` — the page-local ``VoiceStatus`` interface in
 * ``pages/voice.tsx`` predates this hook and is intentionally not
 * promoted (the schema is the canonical runtime contract).
 */
export type VoiceStatusResponse = z.infer<typeof VoiceStatusResponseSchema>;

export const BASELINE_INTERVAL_MS = 500;
export const FIRST_BACKOFF_INTERVAL_MS =
  BASELINE_INTERVAL_MS * FIRST_BACKOFF_MULTIPLIER; // 1500
export const SUSTAINED_BACKOFF_INTERVAL_MS =
  BASELINE_INTERVAL_MS * SUSTAINED_BACKOFF_MULTIPLIER; // 5000
export const DEGRADED_INTERVAL_MS =
  BASELINE_INTERVAL_MS * DEGRADED_MULTIPLIER; // 10000
export const FIRST_BACKOFF_AFTER_5XX = _FIRST_BACKOFF_AFTER_5XX;
export const SUSTAINED_BACKOFF_AFTER_5XX = _SUSTAINED_BACKOFF_AFTER_5XX;
export const DEGRADED_AFTER_5XX = _DEGRADED_AFTER_5XX;

export type PollerErrorState = "ok" | "degraded";

export interface UseVoiceStatusPollerOptions {
  /** Master enable — when false, no poll runs and prior state is preserved. */
  enabled: boolean;
}

export interface VoiceStatusPollerResult {
  /** Latest status — null until first successful poll. */
  status: VoiceStatusResponse | null;
  /** ``"degraded"`` after 11 consecutive 5xx — surface as a UI banner. */
  error: PollerErrorState;
  /** Count of consecutive 5xx responses — exposed for diagnostics. */
  consecutive5xx: number;
}

/**
 * Tier the next-tick delay based on consecutive 5xx count.
 *
 * Decision table (preserved from pre-refactor C2 v0.44.3):
 *
 *   0–1 5xx in a row →   500 ms (baseline; transient blips don't penalise)
 *   2–3              → 1 500 ms (early backoff)
 *   4–10             → 5 000 ms (sustained backoff)
 *   ≥ 11             → 10 000 ms (degraded — banner shown)
 *
 * Returns to baseline on the first 2xx.
 *
 * Implementation now delegates to
 * :func:`use-api-poller.intervalForFailureCount` with the C2
 * baseline (500 ms); the generic multipliers preserve the C2 table
 * bit-exactly. Kept as a top-level export so the existing C2 tests
 * (``use-voice-status-poller.test.tsx``) continue to import the
 * function under its historic name.
 */
export function intervalForFailureCount(consecutive5xx: number): number {
  if (consecutive5xx >= DEGRADED_AFTER_5XX) return DEGRADED_INTERVAL_MS;
  if (consecutive5xx >= SUSTAINED_BACKOFF_AFTER_5XX) {
    return SUSTAINED_BACKOFF_INTERVAL_MS;
  }
  if (consecutive5xx >= FIRST_BACKOFF_AFTER_5XX) return FIRST_BACKOFF_INTERVAL_MS;
  return BASELINE_INTERVAL_MS;
}

/**
 * Hook that polls ``GET /api/voice/status`` with exponential 5xx backoff.
 *
 * Each consumer instance owns its own backoff state — mounting the
 * voice page twice does NOT share a single backoff counter
 * (intentional; matches React's component-scoped state model).
 *
 * Emits ``console.warn("voice.status.poller.degraded", …)`` exactly
 * once when the hook transitions into ``error: "degraded"``. Operators
 * with devtools open see the trail; production builds without devtools
 * see only the in-page banner.
 *
 * Mission C3 §T2.10 ADR (v0.45.6) — implementation is now a thin
 * wrapper around the generic ``useApiPoller``. The wrapper preserves
 * the C2 public API surface (``status`` field name, decision-table
 * constants) so existing call sites + tests stay green.
 */
export function useVoiceStatusPoller(
  options: UseVoiceStatusPollerOptions,
): VoiceStatusPollerResult {
  const { data, error, consecutive5xx } = useApiPoller<
    typeof VoiceStatusResponseSchema,
    VoiceStatusResponse
  >({
    endpoint: "/api/voice/status",
    schema: VoiceStatusResponseSchema,
    baselineIntervalMs: BASELINE_INTERVAL_MS,
    enabled: options.enabled,
    warnTag: "voice.status.poller.degraded",
  });
  return { status: data, error, consecutive5xx };
}
