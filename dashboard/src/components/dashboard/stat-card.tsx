import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface StatCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon?: ReactNode;
  trend?: { value: number; label: string };
  status?: "green" | "red" | "yellow";
  className?: string;
}

const STATUS_LABEL: Record<string, string> = {
  green: "Healthy",
  red: "Error",
  yellow: "Warning",
};

export function StatCard({
  title,
  value,
  subtitle,
  icon,
  trend,
  status,
  className,
}: StatCardProps) {
  return (
    <Card className={cn("glass", className)} role="group" aria-label={title}>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        <div className="flex items-center gap-2">
          {status && (
            <span
              className={
                status === "green"
                  ? "status-dot-green"
                  : status === "red"
                    ? "status-dot-red"
                    : "status-dot-yellow"
              }
              role="status"
              aria-label={STATUS_LABEL[status]}
            />
          )}
          {icon && (
            <span className="text-muted-foreground" aria-hidden="true">{icon}</span>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold" aria-live="polite">{value}</div>
        <div className="flex items-center gap-2">
          {trend && (
            <span
              className={cn(
                "text-xs font-medium",
                trend.value >= 0 ? "text-[var(--color-success)]" : "text-destructive",
              )}
              aria-label={`${trend.value >= 0 ? "Up" : "Down"} ${Math.abs(trend.value)}% ${trend.label}`}
            >
              {trend.value >= 0 ? "↑" : "↓"}
              {Math.abs(trend.value)}% {trend.label}
            </span>
          )}
          {subtitle && (
            <span className="truncate text-xs text-muted-foreground">{subtitle}</span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
