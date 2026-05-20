/* Mission H4 §4.8 ADR-D8 + v0.49.25 — ThreadSnapshotViewer widget.
 *
 * Sibling of HeapSnapshotViewer. Renders the persisted text dump
 * served from GET /api/engine/resources/thread-snapshot/<timestamp>.
 * The endpoint returns ``{content: string, timestamp: string}`` —
 * content is the raw text dump from sys._current_frames() +
 * threading.enumerate(). Bottom-up stack frames per forensic
 * convention.
 */

import { Loader2Icon } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch } from "@/lib/api";

interface ThreadSnapshotPayload {
  content: string;
  timestamp: string;
}

interface ThreadSnapshotViewerProps {
  timestamp: number;
}

export function ThreadSnapshotViewer({ timestamp }: ThreadSnapshotViewerProps) {
  const { t } = useTranslation("voice");
  const [payload, setPayload] = useState<ThreadSnapshotPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch(`/api/engine/resources/thread-snapshot/${timestamp}`)
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
        const data = (await resp.json()) as ThreadSnapshotPayload;
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
        data-testid="thread-snapshot-loading"
      >
        <Loader2Icon className="size-3.5 animate-spin" />
        {t("threadSnapshot.loading")}
      </div>
    );
  }

  if (error === "not_found") {
    return (
      <div
        className="rounded border border-[var(--svx-color-border)] px-3 py-2 text-xs text-[var(--svx-color-text-tertiary)]"
        data-testid="thread-snapshot-not-found"
      >
        {t("threadSnapshot.notFound", { timestamp })}
      </div>
    );
  }

  if (error || !payload) {
    return (
      <div
        className="rounded border border-[var(--svx-color-warning-border)] bg-[var(--svx-color-warning-bg)] px-3 py-2 text-xs text-[var(--svx-color-warning-text)]"
        data-testid="thread-snapshot-error"
      >
        {t("threadSnapshot.error", { error: error ?? "unknown" })}
      </div>
    );
  }

  // Count threads by scanning for "=== Thread " markers in the dump.
  const threadCount = (payload.content.match(/^=== Thread /gm) || []).length;
  const observedAt = new Date(parseInt(payload.timestamp, 10) * 1000).toLocaleString();

  return (
    <section
      aria-labelledby="thread-snapshot-heading"
      className="space-y-3"
      data-testid="thread-snapshot-viewer"
    >
      <div className="flex items-baseline justify-between">
        <h2
          id="thread-snapshot-heading"
          className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
        >
          {t("threadSnapshot.title")}
        </h2>
        <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
          {observedAt}
        </span>
      </div>
      <p className="text-xs text-[var(--svx-color-text-tertiary)]">
        {t("threadSnapshot.subtitle", { count: threadCount })}
      </p>
      <pre
        className="overflow-auto rounded border border-[var(--svx-color-border)] bg-[var(--svx-color-surface)] p-3 text-[11px] font-mono leading-relaxed text-[var(--svx-color-text-primary)]"
        data-testid="thread-snapshot-dump"
      >
        {payload.content}
      </pre>
    </section>
  );
}
