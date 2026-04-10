/**
 * HealthGrid — Displays all health checks from GET /api/health.
 *
 * Backend provides up to 10 checks (offline + online tiers):
 * Offline: Disk Space, RAM, CPU, Embedding Model
 * Online:  sqlite_writable, sqlite_vec, brain, llm, telegram, event_bus,
 *          event_loop_lag, memory (when engine registry is wired)
 *
 * Layout: 2×5 grid on desktop, scrollable on mobile.
 * Each check shows StatusDot + name, with tooltip for details.
 *
 * Ref: Architecture §3.1, META-05 §3
 */

import { useTranslation } from "react-i18next";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { HealthCheck, HealthStatus } from "@/types/api";
import { StatusDot, healthStatusToState } from "./status-dot";
import { cn } from "@/lib/utils";

interface HealthGridProps {
  checks: HealthCheck[];
  className?: string;
}

const STATUS_BG: Record<HealthStatus, string> = {
  green: "bg-[var(--svx-color-success-subtle)]",
  yellow: "bg-[var(--svx-color-warning-subtle)]",
  red: "bg-[var(--svx-color-error-subtle)]",
};

function overallStatus(checks: HealthCheck[]): HealthStatus {
  if (checks.some((c) => c.status === "red")) return "red";
  if (checks.some((c) => c.status === "yellow")) return "yellow";
  return "green";
}

export function HealthGrid({ checks, className }: HealthGridProps) {
  const { t } = useTranslation("overview");
  const overall = overallStatus(checks);
  const greenCount = checks.filter((c) => c.status === "green").length;

  return (
    <div
      className={cn(
        "rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4",
        className,
      )}
    >
      {/* Header */}
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          {t("health.title")}
        </h2>
        <div className="flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]">
          <StatusDot status={healthStatusToState(overall)} size="sm" />
          {t("common:health.checksPass", { passed: greenCount, total: checks.length })}
        </div>
      </div>

      {/* Grid: 2×5 on desktop, responsive on smaller screens */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-2 xl:grid-cols-3">
        {checks.map((check) => (
          <Tooltip key={check.name}>
            <TooltipTrigger>
              <div
                className={cn(
                  "flex items-center gap-2 rounded-[var(--svx-radius-md)] px-3 py-2 text-xs transition-colors",
                  "cursor-default hover:bg-[var(--svx-color-bg-hover)]",
                  STATUS_BG[check.status],
                )}
                role="status"
                aria-label={`${check.name}: ${check.status}`}
                tabIndex={0}
              >
                <StatusDot status={healthStatusToState(check.status)} size="sm" />
                <span className="truncate text-[var(--svx-color-text-secondary)]">
                  {check.name}
                </span>
              </div>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-xs">
              <p className="font-medium text-[var(--svx-color-text-primary)]">
                {check.name}
              </p>
              <p className="text-xs text-[var(--svx-color-text-tertiary)]">
                {check.message}
              </p>
              {check.latency_ms != null && (
                <p className="font-code text-xs text-[var(--svx-color-text-tertiary)]">
                  {check.latency_ms.toFixed(1)}ms
                </p>
              )}
            </TooltipContent>
          </Tooltip>
        ))}
      </div>
    </div>
  );
}
