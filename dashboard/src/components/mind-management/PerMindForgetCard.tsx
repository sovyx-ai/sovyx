/**
 * PerMindForgetCard — destructive right-to-erasure card.
 *
 * Mission ``MISSION-claude-autonomous-batch-2026-05-03.md`` §Phase 2
 * (T2.1 / D3). Mounts inside the per-mind grid in voice.tsx alongside
 * :func:`PerMindWakeWordCard`. Drives ``POST /api/mind/{id}/forget``
 * via the :class:`MindManagementSlice`.
 *
 * UX (defense-in-depth):
 *   1. Operator clicks "Forget this mind" → modal-style block expands
 *      inline with red warning banner.
 *   2. Operator MUST type the mind id verbatim into a text input
 *      (matches GitHub's repo-deletion pattern + backend's
 *      ``confirm: <mind_id>`` requirement at routes/mind.py:173).
 *   3. Optional "preview only" checkbox toggles ``dry_run`` so the
 *      operator can see counts before the actual purge.
 *   4. Submit fires the slice action; the result panel renders the
 *      per-table count breakdown so the operator sees exactly what
 *      was wiped.
 *
 * Pessimistic UX — destructive action MUST NOT be optimistic. The
 * operator only sees confirmation of erasure after the server returns
 * counts. Errors stay visible until dismissed.
 */
import type { JSX } from "react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useDashboardStore } from "@/stores/dashboard";

interface PerMindForgetCardProps {
  /** Target mind id. Must match the backend's per-mind data. */
  mindId: string;
}

export function PerMindForgetCard({ mindId }: PerMindForgetCardProps): JSX.Element {
  const { t } = useTranslation("voice");

  const forgetMind = useDashboardStore((s) => s.forgetMind);
  const clearForgetReport = useDashboardStore((s) => s.clearForgetReport);
  const clearForgetError = useDashboardStore((s) => s.clearForgetError);
  const pending = useDashboardStore((s) => s.forgetPending[mindId] ?? false);
  const report = useDashboardStore((s) => s.forgetReports[mindId] ?? null);
  const error = useDashboardStore((s) => s.forgetErrors[mindId] ?? null);

  const [expanded, setExpanded] = useState(false);
  const [confirmInput, setConfirmInput] = useState("");
  const [dryRun, setDryRun] = useState(true);

  const confirmMatches = confirmInput === mindId && confirmInput.length > 0;
  const submitDisabled = pending || !confirmMatches;

  const handleSubmit = async () => {
    await forgetMind(mindId, { confirm: confirmInput, dryRun });
  };

  const handleReset = () => {
    setExpanded(false);
    setConfirmInput("");
    setDryRun(true);
    clearForgetReport(mindId);
    clearForgetError(mindId);
  };

  return (
    <div
      data-testid={`mind-forget-card-${mindId}`}
      className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-danger)]/40 bg-[var(--svx-color-surface-secondary)] p-3"
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {t("mind.forget.title")}
          </div>
          <div className="text-xs text-[var(--svx-color-text-tertiary)]">
            {t("mind.forget.subtitle")}
          </div>
        </div>
        {!expanded && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-danger)] px-3 py-1 text-xs font-medium text-[var(--svx-color-danger)] hover:bg-[var(--svx-color-danger)] hover:text-white"
          >
            {t("mind.forget.openButton")}
          </button>
        )}
      </div>

      {expanded && (
        <div className="mt-3 space-y-3">
          <div
            role="alert"
            className="rounded border border-[var(--svx-color-danger)] bg-[var(--svx-color-danger-soft)] p-2 text-xs text-[var(--svx-color-danger)]"
          >
            <div className="font-semibold">{t("mind.forget.warningTitle")}</div>
            <div className="mt-1">{t("mind.forget.warningBody")}</div>
          </div>

          <label className="block text-xs">
            <span className="text-[var(--svx-color-text-secondary)]">
              {t("mind.forget.confirmLabel", { mindId })}
            </span>
            <input
              type="text"
              value={confirmInput}
              onChange={(e) => setConfirmInput(e.target.value)}
              aria-label={t("mind.forget.confirmAriaLabel")}
              className="mt-1 block w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] px-2 py-1 font-mono text-xs"
              placeholder={mindId}
              disabled={pending}
            />
          </label>

          <label className="flex items-center gap-2 text-xs text-[var(--svx-color-text-secondary)]">
            <input
              type="checkbox"
              checked={dryRun}
              onChange={(e) => setDryRun(e.target.checked)}
              disabled={pending}
            />
            {t("mind.forget.dryRunLabel")}
          </label>

          {error !== null && (
            <div
              role="alert"
              className="rounded border border-[var(--svx-color-danger)] bg-[var(--svx-color-danger-soft)] p-2 text-xs text-[var(--svx-color-danger)]"
            >
              {error}
            </div>
          )}

          {report !== null && (
            <ForgetReportPanel report={report} t={t} />
          )}

          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={handleReset}
              disabled={pending}
              className="rounded-[var(--svx-radius-md)] px-3 py-1 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)]"
            >
              {report !== null
                ? t("mind.forget.closeButton")
                : t("mind.forget.cancelButton")}
            </button>
            {report === null && (
              <button
                type="button"
                onClick={() => void handleSubmit()}
                disabled={submitDisabled}
                className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-danger)] px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
              >
                {pending
                  ? t("mind.forget.submittingButton")
                  : dryRun
                    ? t("mind.forget.previewButton")
                    : t("mind.forget.submitButton")}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Per-table count breakdown rendered after a successful forget.
 *
 * Mirror of the backend's :class:`ForgetMindResponse` shape — every
 * non-aggregate count field gets its own row so the operator can
 * forensically verify "I expected only conversations to be wiped, not
 * brain rows" without parsing the JSON.
 */
function ForgetReportPanel({
  report,
  t,
}: {
  report: import("@/types/api").ForgetMindResponse;
  t: (key: string, opts?: Record<string, unknown>) => string;
}): JSX.Element {
  return (
    <div
      data-testid="mind-forget-report"
      className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-surface-tertiary)] p-2 text-xs"
    >
      <div className="mb-1.5 font-semibold text-[var(--svx-color-text-primary)]">
        {report.dry_run
          ? t("mind.forget.reportTitleDryRun")
          : t("mind.forget.reportTitleApplied")}
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 font-mono">
        <CountRow label="concepts" value={report.concepts_purged} />
        <CountRow label="relations" value={report.relations_purged} />
        <CountRow label="episodes" value={report.episodes_purged} />
        <CountRow
          label="concept_embeddings"
          value={report.concept_embeddings_purged}
        />
        <CountRow
          label="episode_embeddings"
          value={report.episode_embeddings_purged}
        />
        <CountRow
          label="conversation_imports"
          value={report.conversation_imports_purged}
        />
        <CountRow
          label="consolidation_log"
          value={report.consolidation_log_purged}
        />
        <CountRow label="conversations" value={report.conversations_purged} />
        <CountRow
          label="conversation_turns"
          value={report.conversation_turns_purged}
        />
        <CountRow label="daily_stats" value={report.daily_stats_purged} />
        <CountRow
          label="consent_ledger"
          value={report.consent_ledger_purged}
        />
      </dl>
      <div className="mt-1.5 border-t border-[var(--svx-color-border)] pt-1.5 font-semibold text-[var(--svx-color-text-primary)]">
        {t("mind.forget.totalLabel")}: {report.total_rows_purged}
      </div>
    </div>
  );
}

function CountRow({ label, value }: { label: string; value: number }): JSX.Element {
  return (
    <>
      <dt className="text-[var(--svx-color-text-tertiary)]">{label}</dt>
      <dd className="text-right tabular-nums">{value}</dd>
    </>
  );
}
