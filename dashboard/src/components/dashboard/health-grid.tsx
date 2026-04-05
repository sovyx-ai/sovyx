import { useTranslation } from "react-i18next";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { HealthCheck, HealthStatus } from "@/types/api";
import { cn } from "@/lib/utils";

interface HealthGridProps {
  checks: HealthCheck[];
  className?: string;
}

const STATUS_CLASSES: Record<HealthStatus, string> = {
  green: "status-dot-green",
  yellow: "status-dot-yellow",
  red: "status-dot-red",
};

const STATUS_BG: Record<HealthStatus, string> = {
  green: "bg-[var(--color-success)]/10",
  yellow: "bg-[var(--color-warning)]/10",
  red: "bg-destructive/10",
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
    <Card className={className}>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium">{t("health.title")}</CardTitle>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className={STATUS_CLASSES[overall]} />
          {t("common:health.checksPass", { passed: greenCount, total: checks.length })}
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {checks.map((check) => (
            <Tooltip key={check.name}>
              <TooltipTrigger>
                <div
                  className={cn(
                    "flex items-center gap-2 rounded-md px-3 py-2 text-xs transition-colors",
                    STATUS_BG[check.status],
                  )}
                >
                  <span className={STATUS_CLASSES[check.status]} />
                  <span className="truncate">{check.name}</span>
                </div>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                <p className="font-medium">{check.name}</p>
                <p className="text-xs text-muted-foreground">{check.message}</p>
                {check.latency_ms != null && (
                  <p className="text-xs text-muted-foreground">
                    {check.latency_ms}ms
                  </p>
                )}
              </TooltipContent>
            </Tooltip>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
