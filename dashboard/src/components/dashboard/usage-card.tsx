/**
 * UsageCard — Monthly usage summary with spark line.
 *
 * Shows:
 * - Monthly cost (primary)
 * - Monthly messages (secondary)
 * - Spark line SVG (last 30 days cost trend)
 * - All-time total (tertiary)
 *
 * Design: one card, one glance, three pieces of information.
 * No hover, no expand, no modal. Glanceable.
 */

import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { useDashboardStore } from "@/stores/dashboard";
import type { DailyStats } from "@/types/api";

/** Fill date gaps in history with zero-value entries. */
function fillGaps(days: DailyStats[]): DailyStats[] {
  if (days.length < 2) return days;

  const filled: DailyStats[] = [];
  const first = days[0];
  const last = days[days.length - 1];
  if (!first || !last) return days;
  const start = new Date(first.date);
  const end = new Date(last.date);
  const lookup = new Map(days.map((d) => [d.date, d]));

  for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
    const key = d.toISOString().slice(0, 10);
    filled.push(
      lookup.get(key) ?? {
        date: key,
        cost: 0,
        messages: 0,
        llm_calls: 0,
        tokens: 0,
      },
    );
  }
  return filled;
}

/** Tiny SVG spark line — no chart library needed. */
function SparkLine({
  data,
  className,
}: {
  data: number[];
  className?: string;
}) {
  if (data.length < 2) return null;

  const max = Math.max(...data, 0.001); // avoid division by zero
  const w = 200;
  const h = 32;
  const pad = 1;

  const points = data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * (w - 2 * pad);
    const y = h - pad - (v / max) * (h - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className={cn("w-full", className)}
      aria-hidden="true"
      data-testid="spark-line"
    >
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="var(--svx-color-brand-primary, #8B5CF6)"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function formatCost(cost: number): string {
  if (cost < 0.01) return "$0.00";
  if (cost < 10) return `$${cost.toFixed(2)}`;
  if (cost < 100) return `$${cost.toFixed(1)}`;
  return `$${Math.round(cost)}`;
}

export function UsageCard({ className }: { className?: string }) {
  const { t } = useTranslation("overview");

  const statsHistory = useDashboardStore((s) => s.statsHistory);
  const statsTotals = useDashboardStore((s) => s.statsTotals);
  const statsMonth = useDashboardStore((s) => s.statsMonth);
  const fetchStatsHistory = useDashboardStore((s) => s.fetchStatsHistory);

  useEffect(() => {
    let cancelled = false;
    void fetchStatsHistory(30).catch(() => {
      if (!cancelled) { /* already handled in store */ }
    });
    return () => { cancelled = true; };
  }, [fetchStatsHistory]);

  const hasHistory = statsHistory.length > 0;
  const filled = fillGaps(statsHistory);
  const costData = filled.map((d) => d.cost);

  const monthlyCost = statsMonth?.cost ?? 0;
  const monthlyMessages = statsMonth?.messages ?? 0;
  const totalCost = statsTotals?.cost ?? 0;
  const daysActive = statsTotals?.days_active ?? 0;

  return (
    <div
      className={cn(
        "rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)]",
        "bg-[var(--svx-color-bg-surface)] p-3 transition-colors",
        "hover:border-[var(--svx-color-border-strong)]",
        className,
      )}
      role="group"
      aria-label={t("usage.title")}
      data-testid="usage-card"
    >
      {!hasHistory ? (
        <div className="text-center">
          <span className="text-sm text-[var(--svx-color-text-tertiary)]">
            {t("usage.noHistory")}
          </span>
        </div>
      ) : (
        <>
          {/* Header */}
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
              {t("usage.title")}
            </span>
          </div>

          {/* Main metrics row */}
          <div className="flex items-baseline justify-between gap-4">
            <span
              className="text-2xl font-bold tracking-tight text-[var(--svx-color-text-primary)]"
              data-testid="usage-monthly-cost"
            >
              {formatCost(monthlyCost)}
            </span>
            <span
              className="text-sm text-[var(--svx-color-text-secondary)]"
              data-testid="usage-monthly-messages"
            >
              {monthlyMessages.toLocaleString()} {t("usage.messages")}
            </span>
          </div>

          {/* Spark line — hidden on very small screens */}
          <div className="my-2 hidden sm:block" data-testid="spark-line-container">
            <SparkLine data={costData} className="h-8" />
          </div>

          {/* All-time total */}
          <div className="text-xs text-[var(--svx-color-text-tertiary)]" data-testid="usage-total">
            {t("usage.total", {
              cost: formatCost(totalCost),
              days: daysActive,
            })}
          </div>
        </>
      )}
    </div>
  );
}
