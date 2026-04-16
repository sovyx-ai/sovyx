import { useState } from "react";
import {
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
  Cell,
} from "recharts";

interface ScatterPoint {
  valence: number;
  arousal: number;
  dominance: number;
  summary: string;
  timestamp: string;
}

interface PADScatterProps {
  points: ScatterPoint[];
}

type Projection = "VA" | "VD" | "AD";

const PROJ_LABELS: Record<Projection, { x: string; y: string }> = {
  VA: { x: "Valence", y: "Arousal" },
  VD: { x: "Valence", y: "Dominance" },
  AD: { x: "Arousal", y: "Dominance" },
};

function pointColor(v: number): string {
  if (v >= 0.2) return "#14b8a6";
  if (v <= -0.2) return "#f87171";
  return "#94a3b8";
}

export function PADScatter({ points }: PADScatterProps) {
  const [proj, setProj] = useState<Projection>("VA");
  const labels = PROJ_LABELS[proj];

  const data = points.map((p) => {
    const xVal = proj === "AD" ? p.arousal : p.valence;
    const yVal = proj === "VA" ? p.arousal : p.dominance;
    return { x: xVal, y: yVal, v: p.valence, a: p.arousal, d: p.dominance, summary: p.summary };
  });

  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-5">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          PAD Space
        </h3>
        <div className="flex gap-1">
          {(["VA", "VD", "AD"] as Projection[]).map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setProj(p)}
              className={`rounded-[var(--svx-radius-sm)] px-2 py-0.5 text-[10px] font-medium transition-colors ${
                proj === p
                  ? "bg-[var(--svx-color-brand-primary)]/15 text-[var(--svx-color-brand-primary)]"
                  : "text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-secondary)]"
              }`}
            >
              {p.split("").join(" x ")}
            </button>
          ))}
        </div>
      </div>

      {data.length === 0 ? (
        <div className="flex h-52 items-center justify-center text-xs text-[var(--svx-color-text-tertiary)]">
          No data points yet
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <ScatterChart margin={{ top: 5, right: 5, bottom: 5, left: -10 }}>
            <XAxis
              dataKey="x"
              type="number"
              domain={[-1, 1]}
              tick={{ fontSize: 10, fill: "var(--svx-color-text-tertiary)" }}
              axisLine={false}
              tickLine={false}
              name={labels.x}
              ticks={[-1, -0.5, 0, 0.5, 1]}
            />
            <YAxis
              dataKey="y"
              type="number"
              domain={[-1, 1]}
              tick={{ fontSize: 10, fill: "var(--svx-color-text-tertiary)" }}
              axisLine={false}
              tickLine={false}
              name={labels.y}
              ticks={[-1, -0.5, 0, 0.5, 1]}
            />
            <ReferenceLine x={0} stroke="var(--svx-color-border-default)" strokeDasharray="3 3" />
            <ReferenceLine y={0} stroke="var(--svx-color-border-default)" strokeDasharray="3 3" />
            <Tooltip
              contentStyle={{
                backgroundColor: "var(--svx-color-bg-elevated)",
                border: "1px solid var(--svx-color-border-default)",
                borderRadius: 8,
                fontSize: 11,
              }}
              formatter={(value, name) => [Number(value).toFixed(3), String(name)]}
              labelFormatter={() => ""}
            />
            <Scatter data={data} name="Episodes">
              {data.map((entry, i) => (
                <Cell
                  key={i}
                  fill={pointColor(entry.v)}
                  fillOpacity={0.7 + Math.abs(entry.d ?? 0) * 0.3}
                  r={3 + Math.abs(entry.a) * 5}
                />
              ))}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      )}

      <div className="mt-2 flex justify-center gap-4 text-[10px] text-[var(--svx-color-text-disabled)]">
        <span>X: {labels.x}</span>
        <span>Y: {labels.y}</span>
      </div>
    </div>
  );
}
