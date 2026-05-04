/**
 * PerMindRetentionCard — time-based scheduled-policy prune card.
 *
 * Mission ``MISSION-claude-autonomous-batch-2026-05-03.md`` §Phase 2
 * (T2.2 / D3). Sibling to :func:`PerMindForgetCard` — both mount in
 * the per-mind grid in voice.tsx; both consume
 * :class:`MindManagementSlice`. Drives ``POST /api/mind/{id}/retention/prune``.
 *
 * UX (less destructive than forget):
 *   1. Operator clicks "Manage retention…" → expanded block.
 *   2. "Preview prune" button fires ``dry_run=true``; result panel
 *      renders per-surface counts + the server-computed
 *      ``effective_horizons`` map (so the operator sees "episodes
 *      older than 90 days will be wiped" before committing).
 *   3. After preview, "Apply prune" button fires ``dry_run=false``.
 *   4. The endpoint requires NO ``confirm`` field (per
 *      routes/mind.py:266 — retention is scheduled-policy, only
 *      removes aged records, not arbitrary rows).
 *
 * The "preview-then-apply" two-step is operator UX, not a backend
 * requirement: the slice can issue dry_run=false without a prior
 * preview. We choose to render the "Apply" button only after a
 * preview report has landed because the ``effective_horizons`` map
 * is server-computed — operators can't know what will be pruned
 * until they see the preview.
 */
import type { JSX } from "react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useDashboardStore } from "@/stores/dashboard";

interface PerMindRetentionCardProps {
  /** Target mind id. */
  mindId: string;
}

export function PerMindRetentionCard({
  mindId,
}: PerMindRetentionCardProps): JSX.Element {
  const { t } = useTranslation("voice");

  const pruneRetention = useDashboardStore((s) => s.pruneRetention);
  const clearRetentionReport = useDashboardStore(
    (s) => s.clearRetentionReport,
  );
  const clearRetentionError = useDashboardStore((s) => s.clearRetentionError);
  const pending = useDashboardStore(
    (s) => s.retentionPending[mindId] ?? false,
  );
  const report = useDashboardStore((s) => s.retentionReports[mindId] ?? null);
  const error = useDashboardStore((s) => s.retentionErrors[mindId] ?? null);

  const [expanded, setExpanded] = useState(false);

  const handlePreview = async () => {
    await pruneRetention(mindId, { dryRun: true });
  };

  const handleApply = async () => {
    await pruneRetention(mindId, { dryRun: false });
  };

  const handleReset = () => {
    setExpanded(false);
    clearRetentionReport(mindId);
    clearRetentionError(mindId);
  };

  // The "Apply" button only renders AFTER a preview lands — see the
  // module docstring's "preview-then-apply" rationale.
  const previewLanded = report !== null && report.dry_run;
  const applyLanded = report !== null && !report.dry_run;

  return (
    <div
      data-testid={`mind-retention-card-${mindId}`}
      className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-3"
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {t("mind.retention.title")}
          </div>
          <div className="text-xs text-[var(--svx-color-text-tertiary)]">
            {t("mind.retention.subtitle")}
          </div>
        </div>
        {!expanded && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-accent)] bg-[var(--svx-color-accent-soft)] px-3 py-1 text-xs font-medium text-[var(--svx-color-accent)] hover:bg-[var(--svx-color-accent)] hover:text-white"
          >
            {t("mind.retention.openButton")}
          </button>
        )}
      </div>

      {expanded && (
        <div className="mt-3 space-y-3">
          {error !== null && (
            <div
              role="alert"
              className="rounded border border-[var(--svx-color-danger)] bg-[var(--svx-color-danger-soft)] p-2 text-xs text-[var(--svx-color-danger)]"
            >
              {error}
            </div>
          )}

          {report !== null && (
            <RetentionReportPanel report={report} t={t} />
          )}

          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={handleReset}
              disabled={pending}
              className="rounded-[var(--svx-radius-md)] px-3 py-1 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)]"
            >
              {applyLanded
                ? t("mind.retention.closeButton")
                : t("mind.retention.cancelButton")}
            </button>
            {!applyLanded && (
              <>
                {!previewLanded ? (
                  <button
                    type="button"
                    onClick={() => void handlePreview()}
                    disabled={pending}
                    className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-accent)] px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
                  >
                    {pending
                      ? t("mind.retention.submittingButton")
                      : t("mind.retention.previewButton")}
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void handleApply()}
                    disabled={pending}
                    className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-warning)] px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
                  >
                    {pending
                      ? t("mind.retention.submittingButton")
                      : t("mind.retention.applyButton")}
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Per-surface count breakdown + horizons map.
 *
 * Mirror of :class:`PruneRetentionResponse` — every count field gets
 * a row, plus the cutoff timestamp + the ``effective_horizons`` map
 * so the operator forensically verifies "episodes older than X days
 * were the cutoff for this prune". Server-computed; operator can't
 * predict locally.
 */
function RetentionReportPanel({
  report,
  t,
}: {
  report: import("@/types/api").PruneRetentionResponse;
  t: (key: string, opts?: Record<string, unknown>) => string;
}): JSX.Element {
  const horizonEntries = Object.entries(report.effective_horizons);
  return (
    <div
      data-testid="mind-retention-report"
      className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-surface-tertiary)] p-2 text-xs"
    >
      <div className="mb-1.5 font-semibold text-[var(--svx-color-text-primary)]">
        {report.dry_run
          ? t("mind.retention.reportTitleDryRun")
          : t("mind.retention.reportTitleApplied")}
      </div>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 font-mono">
        <CountRow label="episodes" value={report.episodes_purged} />
        <CountRow label="conversations" value={report.conversations_purged} />
        <CountRow
          label="conversation_turns"
          value={report.conversation_turns_purged}
        />
        <CountRow label="daily_stats" value={report.daily_stats_purged} />
        <CountRow
          label="consolidation_log"
          value={report.consolidation_log_purged}
        />
        <CountRow
          label="consent_ledger"
          value={report.consent_ledger_purged}
        />
      </dl>

      <div className="mt-1.5 border-t border-[var(--svx-color-border)] pt-1.5 font-semibold text-[var(--svx-color-text-primary)]">
        {t("mind.retention.totalLabel")}: {report.total_rows_purged}
      </div>

      <div className="mt-1.5 text-[var(--svx-color-text-tertiary)]">
        {t("mind.retention.cutoffLabel")}: {report.cutoff_utc}
      </div>

      {horizonEntries.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[var(--svx-color-text-secondary)]">
            {t("mind.retention.horizonsLabel")}
          </summary>
          <div className="mt-1 text-[var(--svx-color-text-tertiary)]">
            {t("mind.retention.horizonsHint")}
          </div>
          <dl className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 font-mono">
            {horizonEntries.map(([surface, days]) => (
              <CountRow key={surface} label={surface} value={days} />
            ))}
          </dl>
        </details>
      )}
    </div>
  );
}

function CountRow({
  label,
  value,
}: {
  label: string;
  value: number;
}): JSX.Element {
  return (
    <>
      <dt className="text-[var(--svx-color-text-tertiary)]">{label}</dt>
      <dd className="text-right tabular-nums">{value}</dd>
    </>
  );
}
