import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";

interface TimelinePoint {
  timestamp: string;
  valence: number;
  arousal: number;
  dominance: number;
  summary: string;
}

interface MoodTimelineProps {
  points: TimelinePoint[];
  period: string;
  onPeriodChange: (p: string) => void;
}

const PERIODS = ["24h", "7d", "30d", "all"];

function formatTime(ts: string, period: string): string {
  const d = new Date(ts);
  if (period === "24h") return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (period === "7d") return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function MoodTimeline({ points, period, onPeriodChange }: MoodTimelineProps) {
  const data = points.map((p) => ({
    ...p,
    time: formatTime(p.timestamp, period),
    upper: Math.min(1, p.valence + Math.abs(p.arousal) * 0.3),
    lower: Math.max(-1, p.valence - Math.abs(p.arousal) * 0.3),
  }));

  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-5">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          Mood Timeline
        </h3>
        <div className="flex gap-1">
          {PERIODS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => onPeriodChange(p)}
              className={`rounded-[var(--svx-radius-sm)] px-2 py-0.5 text-[10px] font-medium transition-colors ${
                period === p
                  ? "bg-[var(--svx-color-brand-primary)]/15 text-[var(--svx-color-brand-primary)]"
                  : "text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-secondary)]"
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      {data.length === 0 ? (
        <div className="flex h-40 items-center justify-center text-xs text-[var(--svx-color-text-tertiary)]">
          No emotional data for this period
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <AreaChart data={data} margin={{ top: 5, right: 5, bottom: 5, left: -20 }}>
            <defs>
              <linearGradient id="valenceGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#14b8a6" stopOpacity={0.3} />
                <stop offset="50%" stopColor="#14b8a6" stopOpacity={0} />
                <stop offset="50%" stopColor="#f87171" stopOpacity={0} />
                <stop offset="100%" stopColor="#f87171" stopOpacity={0.3} />
              </linearGradient>
            </defs>
            <XAxis
              dataKey="time"
              tick={{ fontSize: 10, fill: "var(--svx-color-text-tertiary)" }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              domain={[-1, 1]}
              tick={{ fontSize: 10, fill: "var(--svx-color-text-tertiary)" }}
              axisLine={false}
              tickLine={false}
              ticks={[-1, -0.5, 0, 0.5, 1]}
            />
            <ReferenceLine y={0} stroke="var(--svx-color-border-default)" strokeDasharray="3 3" />
            <Tooltip
              contentStyle={{
                backgroundColor: "var(--svx-color-bg-elevated)",
                border: "1px solid var(--svx-color-border-default)",
                borderRadius: 8,
                fontSize: 11,
              }}
              formatter={(value, name) => [Number(value).toFixed(3), String(name)]}
              labelFormatter={(label) => String(label)}
            />
            <Area
              type="monotone"
              dataKey="upper"
              stroke="none"
              fill="var(--svx-color-brand-primary)"
              fillOpacity={0.05}
            />
            <Area
              type="monotone"
              dataKey="lower"
              stroke="none"
              fill="var(--svx-color-brand-primary)"
              fillOpacity={0.05}
            />
            <Area
              type="monotone"
              dataKey="valence"
              stroke="#14b8a6"
              strokeWidth={2}
              fill="url(#valenceGrad)"
              dot={{ r: 3, fill: "#14b8a6", strokeWidth: 0 }}
              activeDot={{ r: 5, stroke: "#14b8a6", strokeWidth: 2, fill: "var(--svx-color-bg-surface)" }}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
