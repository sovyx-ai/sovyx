/**
 * Mission H4 §4.8 ADR-D8 + v0.49.25 — Heap snapshot deep-link page.
 *
 * Resolves the ``/engine/resources/heap-snapshot/:ts`` route. Parses
 * the timestamp from the URL path and hands it to HeapSnapshotViewer.
 * Invalid timestamps (non-numeric) render a guard-rail error state
 * instead of crashing the viewer with NaN fetches.
 */

import { useTranslation } from "react-i18next";
import { useParams, Link } from "react-router";

import { HeapSnapshotViewer } from "@/components/engine/HeapSnapshotViewer";

export default function EngineResourcesHeapSnapshotPage() {
  const { t } = useTranslation("voice");
  const params = useParams<{ ts?: string }>();
  const tsParam = params.ts ?? "";
  const ts = Number.parseInt(tsParam, 10);
  const isValidTs = Number.isFinite(ts) && ts > 0 && String(ts) === tsParam;

  return (
    <div
      className="space-y-4 p-4 md:p-6"
      data-testid="engine-resources-heap-snapshot-page"
    >
      <Link
        to="/engine/resources"
        className="text-xs text-[var(--svx-color-text-tertiary)] underline hover:text-[var(--svx-color-text-secondary)]"
      >
        ← {t("resources.title")}
      </Link>
      {isValidTs ? (
        <HeapSnapshotViewer timestamp={ts} />
      ) : (
        <div
          className="rounded border border-[var(--svx-color-warning-border)] bg-[var(--svx-color-warning-bg)] px-3 py-2 text-xs text-[var(--svx-color-warning-text)]"
          data-testid="heap-snapshot-invalid-ts"
        >
          {t("heapSnapshot.error", { error: `invalid_timestamp:${tsParam}` })}
        </div>
      )}
    </div>
  );
}
