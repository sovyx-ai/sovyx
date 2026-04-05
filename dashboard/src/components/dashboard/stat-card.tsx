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
    <Card className={cn("glass", className)}>
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
            />
          )}
          {icon && (
            <span className="text-muted-foreground">{icon}</span>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value}</div>
        <div className="flex items-center gap-2">
          {trend && (
            <span
              className={cn(
                "text-xs font-medium",
                trend.value >= 0 ? "text-[var(--color-success)]" : "text-destructive",
              )}
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
