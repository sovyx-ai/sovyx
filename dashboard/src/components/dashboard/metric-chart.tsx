import { useId } from "react";
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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

function formatChartTime(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function MetricChart({
  title,
  data,
  color = "var(--chart-1)",
  unit = "",
  label,
  className,
}: MetricChartProps) {
  const gradientId = useId();
  const dataLabel = label ?? title;

  const chartConfig: ChartConfig = {
    value: {
      label: dataLabel,
      color,
    },
  };

  return (
    <Card className={className}>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-[var(--svx-color-text-secondary)]">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="flex h-[140px] items-center justify-center text-xs text-[var(--svx-color-text-secondary)]">
            No data yet
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
                    // eslint-disable-next-line @typescript-eslint/no-explicit-any
                    labelFormatter={(_: any, payload: readonly any[]) => {
                      if (payload?.[0]?.payload?.time) {
                        return formatChartTime(payload[0].payload.time as number);
                      }
                      return "";
                    }}
                    // eslint-disable-next-line @typescript-eslint/no-explicit-any
                    formatter={(value: any) => [`${String(value)}${unit}`, dataLabel] as any}
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
      </CardContent>
    </Card>
  );
}

// ── Compact sparkline variant ──

interface SparklineProps {
  data: number[];
  color?: string;
  className?: string;
}

export function Sparkline({
  data,
  color = "var(--chart-1)",
  className,
}: SparklineProps) {
  const chartData = data.map((value, i) => ({ time: i, value }));
  const chartConfig: ChartConfig = {
    value: { label: "value", color },
  };

  return (
    <div className={cn("h-8 w-24", className)}>
      <ChartContainer config={chartConfig} className="h-full w-full">
        <AreaChart data={chartData}>
          <Area
            type="monotone"
            dataKey="value"
            stroke="var(--color-value)"
            strokeWidth={1.5}
            fill="var(--color-value)"
            fillOpacity={0.1}
            dot={false}
          />
        </AreaChart>
      </ChartContainer>
    </div>
  );
}
