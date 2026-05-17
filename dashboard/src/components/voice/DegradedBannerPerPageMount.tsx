/**
 * Per-page degraded-banner mount — registered on /voice + /voice/health.
 *
 * Mission C4 §T1.11. Declares the per-page mount as active via the
 * ``DegradedBannerMountedContext`` so the global mount yields.
 * Provides richer per-axis context (closer to the page's domain) and
 * lives at the natural top-of-page position rather than the dashboard
 * shell's header.
 */
import { useCallback, useEffect } from "react";
import { DegradedBanner } from "./DegradedBanner";
import {
  ackComposite,
  useEngineDegradedPoller,
} from "@/hooks/use-engine-degraded-poller";
import { useDegradedBannerMounted } from "@/contexts/degraded-banner-mounted";

export function DegradedBannerPerPageMount() {
  const { setPerPageMounted } = useDegradedBannerMounted();
  const { data } = useEngineDegradedPoller();

  // Mission C4 §Phase 3 §T3.7 — ack click handler. Mirrors the global
  // mount; both invocations POST to the same /api/voice/degraded/ack.
  const handleAck = useCallback((ttlSec: number) => {
    void ackComposite(ttlSec).catch(() => {
      /* swallow — poll cycle re-syncs */
    });
  }, []);

  useEffect(() => {
    setPerPageMounted(true);
    return () => setPerPageMounted(false);
  }, [setPerPageMounted]);

  if (!data || !data.axes || data.axes.length === 0) return null;
  if ((data.composite_axis_count ?? 0) === 0) return null;

  return (
    <div
      data-testid="degraded-banner-per-page-mount"
      className="mb-4"
    >
      <DegradedBanner payload={data} onAck={handleAck} />
    </div>
  );
}
