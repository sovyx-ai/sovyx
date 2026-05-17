/**
 * use-engine-degraded-poller — thin wrapper around the generic
 * {@link useApiPoller} hook for the composite ``/api/engine/degraded``
 * endpoint.
 *
 * Mission C4 §T1.10 — keeps the banner mounts trivially testable by
 * isolating endpoint + schema + baseline interval choices in one
 * place. Reuses the C3 v0.45.5 ``useApiPoller<S,T>`` (sibling of
 * ``use-voice-status-poller``) so the composite banner inherits the
 * same circuit-breaker discipline (3× baseline after 2 5xx, 10× after
 * 4, 20× + degraded after 11) without duplicating state-machine code.
 *
 * Baseline poll interval: 5 s. Matches C3's failover-history
 * cadence — the operator-experience requirement is "banner surfaced
 * within 5 s of any degraded state" (F1 falsifiability gate).
 *
 * Mission anchor:
 * docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md
 * §T1.10.
 */
import { useApiPoller, type ApiPollerResult } from "@/hooks/use-api-poller";
import { api } from "@/lib/api";
import { EngineDegradedResponseSchema } from "@/types/schemas";
import type { z } from "zod";

export type EngineDegradedPayload = z.infer<typeof EngineDegradedResponseSchema>;

export const ENGINE_DEGRADED_POLL_INTERVAL_MS = 5000;

interface UseEngineDegradedPollerOptions {
  /** Master enable — when false, no poll runs. Defaults to true. */
  enabled?: boolean;
}

export function useEngineDegradedPoller(
  options: UseEngineDegradedPollerOptions = {},
): ApiPollerResult<EngineDegradedPayload> {
  const { enabled = true } = options;
  return useApiPoller({
    endpoint: "/api/engine/degraded",
    schema: EngineDegradedResponseSchema,
    baselineIntervalMs: ENGINE_DEGRADED_POLL_INTERVAL_MS,
    enabled,
    warnTag: "engine_degraded_poller_degraded",
  });
}

/**
 * POST /api/voice/degraded/ack with the canonical composite-ack body.
 *
 * Mission C4 §Phase 3 §T3.3 — operator acknowledges all currently-
 * active degraded axes for ``ttlSec`` seconds. Server-side
 * persistence (OperatorAcksStore + operator_acks SQLite table) so
 * the ack survives browser refresh + multi-tab.
 *
 * Returns the response body; throws ``ApiError`` on 4xx/5xx via the
 * shared api helper.
 */
export async function ackComposite(ttlSec = 3600): Promise<unknown> {
  return api.post("/api/voice/degraded/ack", {
    reason: "composite",
    ttl_sec: ttlSec,
  });
}
