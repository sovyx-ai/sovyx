/**
 * StatCard — Overview stat card with design token system.
 *
 * Displays a key metric with:
 * - Title (xs, text-secondary)
 * - Value (display size, text-primary)
 * - Optional status dot (StatusDot component)
 * - Optional icon (Lucide)
 * - Optional trend indicator (up/down arrow)
 * - Optional subtitle (secondary text)
 * - Skeleton variant for loading state (REFINE-07)
 *
 * Ref: Architecture §3.1, META-01 §8 (Cards spec)
 */

import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

/**
 * Shimmer skeleton bar — reusable animated placeholder.
 */
function Shimmer({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "animate-[shimmer_1.5s_ease-in-out_infinite] rounded bg-[var(--svx-color-bg-elevated)]",
        className,
      )}
      style={{
        backgroundImage:
          "linear-gradient(90deg, transparent 0%, var(--svx-color-bg-hover) 50%, transparent 100%)",
        backgroundSize: "200% 100%",
      }}
    />
  );
}

/**
 * StatCardSkeleton — Loading placeholder matching StatCard layout.
 */
export function StatCardSkeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4",
        className,
      )}
      role="group"
      aria-label="Loading"
    >
      {/* Title shimmer */}
      <div className="flex items-center justify-between pb-2">
        <Shimmer className="h-3 w-20" />
        <Shimmer className="size-4 rounded-full" />
      </div>
      {/* Value shimmer */}
      <Shimmer className="h-7 w-24" />
      {/* Subtitle shimmer */}
      <div className="mt-2">
        <Shimmer className="h-3 w-16" />
      </div>
    </div>
  );
}
import { StatusDot, healthStatusToState } from "./status-dot";
import type { HealthStatus } from "./status-dot";

interface StatCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon?: ReactNode;
  trend?: { value: number; label: string };
  status?: HealthStatus;
  className?: string;
}

export function StatCard({
  title,
  value,
  subtitle,
  icon,
  trend,
  status,
  className,
}: StatCardProps) {
  const { t } = useTranslation("common");

  return (
    <div
      className={cn(
        "rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4 transition-colors",
        "hover:border-[var(--svx-color-border-strong)]",
        className,
      )}
      role="group"
      aria-label={title}
    >
      {/* Header: title + icon/status */}
      <div className="flex items-center justify-between pb-2">
        <span className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
          {title}
        </span>
        <div className="flex items-center gap-2">
          {status && <StatusDot status={healthStatusToState(status)} size="sm" />}
          {icon && (
            <span className="text-[var(--svx-color-text-tertiary)]" aria-hidden="true">
              {icon}
            </span>
          )}
        </div>
      </div>

      {/* Value */}
      <div
        className="text-2xl font-bold tracking-tight text-[var(--svx-color-text-primary)]"
        aria-live="polite"
      >
        {value}
      </div>

      {/* Footer: trend + subtitle */}
      {(trend || subtitle) && (
        <div className="mt-1 flex items-center gap-2">
          {trend && (
            <span
              className={cn(
                "text-xs font-medium",
                trend.value >= 0
                  ? "text-[var(--svx-color-success)]"
                  : "text-[var(--svx-color-error)]",
              )}
              aria-label={t(trend.value >= 0 ? "trend.up" : "trend.down", { value: Math.abs(trend.value), label: trend.label })}
            >
              {trend.value >= 0 ? "↑" : "↓"}
              {Math.abs(trend.value)}%
            </span>
          )}
          {subtitle && (
            <span className="truncate text-xs text-[var(--svx-color-text-tertiary)]">
              {subtitle}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
