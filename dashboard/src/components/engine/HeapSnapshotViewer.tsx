/* Mission H4 §8 T4.3 — HeapSnapshotViewer widget.
 *
 * Renders a persisted heap-snapshot JSON file served from
 * GET /api/engine/resources/heap-snapshot/<timestamp>. Top-50 allocator
 * histogram (rank / size_bytes / count / traceback) — operators use
 * this AFTER the ResourceCohortGovernor fires RSS_GROWTH + the file
 * is persisted under ~/.sovyx/diagnostics/heap-snapshot-<ts>.json.
 *
 * Accepts a `timestamp` prop or reads from URL query string. Renders
 * empty + degraded + loading states consistently with other H4
 * widgets.
 */

import { Loader2Icon } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch } from "@/lib/api";

interface HeapAllocator {
  rank: number;
  size_bytes: number;
  count: number;
  traceback: string[];
}

interface HeapSnapshotPayload {
  kind: string;
  schema_version: string;
  observed_at_unix: number;
  cohort?: string;
  cohort_observed?: number;
  cohort_budget?: number;
  tracemalloc_snapshot: {
    top_allocators: HeapAllocator[];
    total_allocators?: number;
  };
}

interface HeapSnapshotViewerProps {
  timestamp: number;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

export function HeapSnapshotViewer({ timestamp }: HeapSnapshotViewerProps) {
  const { t } = useTranslation("voice");
  const [payload, setPayload] = useState<HeapSnapshotPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch(`/api/engine/resources/heap-snapshot/${timestamp}`)
      .then(async (resp: Response) => {
        if (cancelled) return;
        if (resp.status === 404) {
          setError("not_found");
          setPayload(null);
          return;
        }
        if (!resp.ok) {
          setError(`http_${resp.status}`);
          setPayload(null);
          return;
        }
        const data = (await resp.json()) as HeapSnapshotPayload;
        setPayload(data);
        setError(null);
      })
      .catch(() => {
        if (cancelled) return;
        setError("network");
        setPayload(null);
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [timestamp]);

  if (loading) {
    return (
      <div
        className="flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]"
        data-testid="heap-snapshot-loading"
      >
        <Loader2Icon className="size-3.5 animate-spin" />
        {t("heapSnapshot.loading")}
      </div>
    );
  }

  if (error === "not_found") {
    return (
      <div
        className="rounded border border-[var(--svx-color-border)] px-3 py-2 text-xs text-[var(--svx-color-text-tertiary)]"
        data-testid="heap-snapshot-not-found"
      >
        {t("heapSnapshot.notFound", { timestamp })}
      </div>
    );
  }

  if (error || !payload) {
    return (
      <div
        className="rounded border border-[var(--svx-color-warning-border)] bg-[var(--svx-color-warning-bg)] px-3 py-2 text-xs text-[var(--svx-color-warning-text)]"
        data-testid="heap-snapshot-error"
      >
        {t("heapSnapshot.error", { error: error ?? "unknown" })}
      </div>
    );
  }

  const allocators = payload.tracemalloc_snapshot.top_allocators ?? [];
  const observedAt = new Date(payload.observed_at_unix * 1000).toLocaleString();

  return (
    <section
      aria-labelledby="heap-snapshot-heading"
      className="space-y-3"
      data-testid="heap-snapshot-viewer"
    >
      <div className="flex items-baseline justify-between">
        <h2
          id="heap-snapshot-heading"
          className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
        >
          {t("heapSnapshot.title")}
        </h2>
        <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
          {observedAt}
        </span>
      </div>
      {payload.cohort && (
        <p className="text-xs text-[var(--svx-color-text-tertiary)]">
          {t("heapSnapshot.cohortContext", {
            cohort: payload.cohort,
            observed: payload.cohort_observed,
            budget: payload.cohort_budget,
          })}
        </p>
      )}
      <p className="text-xs text-[var(--svx-color-text-tertiary)]">
        {t("heapSnapshot.subtitle", { count: allocators.length })}
      </p>
      <table
        className="w-full text-xs font-mono"
        data-testid="heap-snapshot-table"
      >
        <thead className="text-[var(--svx-color-text-tertiary)]">
          <tr className="border-b border-[var(--svx-color-border)]">
            <th className="py-1 text-left">{t("heapSnapshot.col.rank")}</th>
            <th className="py-1 text-right">{t("heapSnapshot.col.size")}</th>
            <th className="py-1 text-right">{t("heapSnapshot.col.count")}</th>
            <th className="py-1 text-left">{t("heapSnapshot.col.traceback")}</th>
          </tr>
        </thead>
        <tbody>
          {allocators.map((alloc) => (
            <tr
              key={alloc.rank}
              className="border-b border-[var(--svx-color-border)]/30"
              data-testid={`heap-snapshot-row-${alloc.rank}`}
            >
              <td className="py-1">{alloc.rank}</td>
              <td className="py-1 text-right">{formatBytes(alloc.size_bytes)}</td>
              <td className="py-1 text-right">{alloc.count}</td>
              <td className="py-1 text-[var(--svx-color-text-secondary)] break-all">
                {alloc.traceback.slice(-2).join(" ← ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
