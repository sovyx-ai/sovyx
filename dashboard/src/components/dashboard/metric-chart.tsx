/**
 * MetricChart — Token-consistent chart card for overview metrics.
 *
 * Uses raw divs with --svx-* tokens instead of shadcn Card.
 * Shows branded empty animation when no data.
 *
 * Ref: Architecture §3.1, REFINE-09
 */

import { useId } from "react";
import { useTranslation } from "react-i18next";
import {
  AreaChart,
  Area,
  CartesianGrid,
  XAxis,
  YAxis,
} from "recharts";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { ChartEmptyAnimation } from "@/components/empty-state-animations";
import { formatChartTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface DataPoint {
  time: number; // Unix ms timestamp
  value: number;
}

interface MetricChartProps {
  title: string;
  data: DataPoint[];
  color?: string;
  unit?: string;
  label?: string;
  className?: string;
}

export function MetricChart({
  title,
  data,
  color = "var(--chart-1)",
  unit = "",
  label,
  className,
}: MetricChartProps) {
  const { t } = useTranslation("overview");
  const gradientId = useId();
  const dataLabel = label ?? title;

  const chartConfig: ChartConfig = {
    value: {
      label: dataLabel,
      color,
    },
  };

  return (
    <div
      className={cn(
        "rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4",
        className,
      )}
    >
      {/* Header */}
      <h2 className="mb-3 text-sm font-medium text-[var(--svx-color-text-secondary)]">
        {title}
      </h2>

      {/* Chart or empty */}
      {data.length === 0 ? (
        <div className="flex h-[140px] flex-col items-center justify-center gap-2">
          <ChartEmptyAnimation />
          <span className="text-xs text-[var(--svx-color-text-tertiary)]">
            {t("chart.noData")}
          </span>
        </div>
      ) : (
        <ChartContainer config={chartConfig} className="h-[140px] w-full">
          <AreaChart accessibilityLayer data={data}>
            <CartesianGrid vertical={false} strokeDasharray="3 3" />
            <XAxis
              dataKey="time"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              tickFormatter={formatChartTime}
              tickLine={false}
              axisLine={false}
              tickMargin={8}
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              width={40}
              tickFormatter={(v: number) => `${v}${unit}`}
            />
            <ChartTooltip
              content={
                <ChartTooltipContent
                  labelFormatter={(
                    _: React.ReactNode,
                    payload: ReadonlyArray<{ payload?: Record<string, unknown> }>,
                  ) => {
                    const time = payload?.[0]?.payload?.time;
                    return typeof time === "number" ? formatChartTime(time) : "";
                  }}
                  formatter={(value: unknown) => [
                    `${String(value ?? "")}${unit}`,
                    dataLabel,
                  ]}
                />
              }
            />
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-value)" stopOpacity={0.3} />
                <stop offset="100%" stopColor="var(--color-value)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <Area
              type="monotone"
              dataKey="value"
              stroke="var(--color-value)"
              strokeWidth={2}
              fill={`url(#${gradientId})`}
              dot={false}
              activeDot={{ r: 4 }}
            />
          </AreaChart>
        </ChartContainer>
      )}
    </div>
  );
}


