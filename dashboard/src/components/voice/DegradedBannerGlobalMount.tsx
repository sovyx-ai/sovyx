/**
 * Global degraded-banner mount — renders on every dashboard route.
 *
 * Mission C4 §T1.10. The mount yields to the per-page mount
 * (``DegradedBannerPerPageMount``) when active so the operator never
 * sees a stacked duplicate. Polling cadence 5 s via the shared
 * ``useEngineDegradedPoller`` (C3-era useApiPoller circuit breaker).
 */
import { useCallback } from "react";

import { DegradedBanner } from "./DegradedBanner";
import {
  ackComposite,
  useEngineDegradedPoller,
} from "@/hooks/use-engine-degraded-poller";
import { useDegradedBannerMounted } from "@/contexts/degraded-banner-mounted";

export function DegradedBannerGlobalMount() {
  const { perPageMounted } = useDegradedBannerMounted();
  const { data } = useEngineDegradedPoller();

  // Mission C4 §Phase 3 §T3.7 — ack click handler. POSTs to
  // /api/voice/degraded/ack with reason="composite" so the server
  // records one ack per active axis. Errors are swallowed silently —
  // the next poll will reflect server state regardless.
  const handleAck = useCallback((ttlSec: number) => {
    void ackComposite(ttlSec).catch(() => {
      // Best-effort: poll cycle (5 s) re-syncs state if ack failed
      // server-side. The user's optimistic dismiss is fine to retain
      // here — banner will re-render on the next poll if needed.
    });
  }, []);

  // Defer to the per-page mount when active.
  if (perPageMounted) return null;
  // Defensive: a polled payload missing composite_axis_count OR axes
  // (shouldn't happen against the real endpoint per Quality Gate 8
  // round-trip — but the dashboard's shared useApiPoller mock in some
  // page-level tests returns a different shape).
  if (!data || !data.axes || data.axes.length === 0) return null;
  if ((data.composite_axis_count ?? 0) === 0) return null;

  return (
    <div
      data-testid="degraded-banner-global-mount"
      className="px-4 pt-3 md:px-6"
    >
      <DegradedBanner payload={data} onAck={handleAck} />
    </div>
  );
}
