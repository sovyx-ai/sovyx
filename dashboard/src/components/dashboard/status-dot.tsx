/**
 * StatusDot — Reusable status indicator component.
 *
 * 6 states from BRD-003 Brand Identity:
 * - online:   Accent cyan, pulse 2s   → Engine active, connected
 * - idle:     Warning amber, static   → Engine running but idle
 * - thinking: Brand violet, pulse 1s  → Cognitive cycle active
 * - dreaming: Brand muted, pulse 3s   → Consolidation/sleep cycle
 * - error:    Error red, static       → Health check failed
 * - offline:  Disabled gray, static   → Engine not running
 *
 * Respects prefers-reduced-motion (handled by global CSS rule).
 */

import { cn } from "@/lib/utils";

export type StatusDotState = "online" | "idle" | "thinking" | "dreaming" | "error" | "offline";

/** Maps health status strings from backend to StatusDotState. */
export type HealthStatus = "green" | "yellow" | "red";

export function healthStatusToState(status: HealthStatus): StatusDotState {
  switch (status) {
    case "green":
      return "online";
    case "yellow":
      return "idle";
    case "red":
      return "error";
  }
}

const STATUS_CONFIG: Record<StatusDotState, { color: string; animation: string }> = {
  online: {
    color: "bg-[var(--svx-color-accent-cyan)]",
    animation: "animate-[pulse-dot_2s_ease-in-out_infinite]",
  },
  idle: {
    color: "bg-[var(--svx-color-warning)]",
    animation: "",
  },
  thinking: {
    color: "bg-[var(--svx-color-brand-primary)]",
    animation: "animate-[pulse-dot_1s_ease-in-out_infinite]",
  },
  dreaming: {
    color: "bg-[var(--svx-color-brand-muted)]",
    animation: "animate-[pulse-dot_3s_ease-in-out_infinite]",
  },
  error: {
    color: "bg-[var(--svx-color-error)]",
    animation: "",
  },
  offline: {
    color: "bg-[var(--svx-color-text-disabled)]",
    animation: "",
  },
};

const STATUS_LABELS: Record<StatusDotState, string> = {
  online: "Online",
  idle: "Idle",
  thinking: "Thinking",
  dreaming: "Dreaming",
  error: "Error",
  offline: "Offline",
};

interface StatusDotProps {
  status: StatusDotState;
  /** Override size. Default: 8px (0.5rem) */
  size?: "sm" | "md" | "lg";
  /** Show label text next to the dot */
  showLabel?: boolean;
  className?: string;
}

const SIZE_CLASSES = {
  sm: "size-1.5",   // 6px
  md: "size-2",     // 8px (default)
  lg: "size-2.5",   // 10px
} as const;

export function StatusDot({ status, size = "md", showLabel = false, className }: StatusDotProps) {
  const config = STATUS_CONFIG[status];
  const label = STATUS_LABELS[status];

  return (
    <span className={cn("inline-flex items-center gap-1.5", className)}>
      <span
        className={cn(
          "inline-block shrink-0 rounded-full",
          SIZE_CLASSES[size],
          config.color,
          config.animation,
        )}
        role="status"
        aria-label={label}
      />
      {showLabel && (
        <span className="text-xs text-muted-foreground">{label}</span>
      )}
    </span>
  );
}
