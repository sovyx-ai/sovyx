/**
 * Mission H4 §4.8 ADR-D8 + v0.49.25 — Thread snapshot deep-link page.
 *
 * Resolves the ``/engine/resources/thread-snapshot/:ts`` route. Parses
 * the timestamp from the URL path and hands it to ThreadSnapshotViewer.
 */

import { useTranslation } from "react-i18next";
import { useParams, Link } from "react-router";

import { ThreadSnapshotViewer } from "@/components/engine/ThreadSnapshotViewer";

export default function EngineResourcesThreadSnapshotPage() {
  const { t } = useTranslation("voice");
  const params = useParams<{ ts?: string }>();
  const tsParam = params.ts ?? "";
  const ts = Number.parseInt(tsParam, 10);
  const isValidTs = Number.isFinite(ts) && ts > 0 && String(ts) === tsParam;

  return (
    <div
      className="space-y-4 p-4 md:p-6"
      data-testid="engine-resources-thread-snapshot-page"
    >
      <Link
        to="/engine/resources"
        className="text-xs text-[var(--svx-color-text-tertiary)] underline hover:text-[var(--svx-color-text-secondary)]"
      >
        ← {t("resources.title")}
      </Link>
      {isValidTs ? (
        <ThreadSnapshotViewer timestamp={ts} />
      ) : (
        <div
          className="rounded border border-[var(--svx-color-warning-border)] bg-[var(--svx-color-warning-bg)] px-3 py-2 text-xs text-[var(--svx-color-warning-text)]"
          data-testid="thread-snapshot-invalid-ts"
        >
          {t("threadSnapshot.error", { error: `invalid_timestamp:${tsParam}` })}
        </div>
      )}
    </div>
  );
}
