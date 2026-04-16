import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from "recharts";

interface Distribution {
  positive_active: number;
  positive_passive: number;
  negative_active: number;
  negative_passive: number;
  neutral: number;
}

interface MoodDistributionProps {
  distribution: Distribution;
  total: number;
  period: string;
  onPeriodChange: (p: string) => void;
}

const SEGMENTS = [
  { key: "positive_active", label: "Excited", color: "#f59e0b" },
  { key: "positive_passive", label: "Calm", color: "#14b8a6" },
  { key: "negative_active", label: "Stressed", color: "#f87171" },
  { key: "negative_passive", label: "Melancholy", color: "#818cf8" },
  { key: "neutral", label: "Neutral", color: "#94a3b8" },
] as const;

const PERIODS = ["7d", "30d", "all"];

export function MoodDistribution({
  distribution,
  total,
  period,
  onPeriodChange,
}: MoodDistributionProps) {
  const data = SEGMENTS.map((s) => ({
    name: s.label,
    value: distribution[s.key] || 0,
    color: s.color,
  })).filter((d) => d.value > 0);

  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-5">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          Mood Distribution
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

      {total === 0 ? (
        <div className="flex h-40 items-center justify-center text-xs text-[var(--svx-color-text-tertiary)]">
          No emotional data yet
        </div>
      ) : (
        <div className="flex items-center gap-6">
          <ResponsiveContainer width={140} height={140}>
            <PieChart>
              <Pie
                data={data}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                innerRadius={35}
                outerRadius={60}
                paddingAngle={2}
                strokeWidth={0}
              >
                {data.map((entry, i) => (
                  <Cell key={i} fill={entry.color} />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{
                  backgroundColor: "var(--svx-color-bg-elevated)",
                  border: "1px solid var(--svx-color-border-default)",
                  borderRadius: 8,
                  fontSize: 11,
                }}
                formatter={(value) => [`${Number(value).toFixed(1)}%`, ""]}
              />
            </PieChart>
          </ResponsiveContainer>
          <div className="flex-1 space-y-1.5">
            {data.map((d) => (
              <div key={d.name} className="flex items-center gap-2 text-xs">
                <span
                  className="size-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: d.color }}
                />
                <span className="flex-1 text-[var(--svx-color-text-secondary)]">{d.name}</span>
                <span className="font-mono text-[var(--svx-color-text-tertiary)]">
                  {d.value.toFixed(1)}%
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
