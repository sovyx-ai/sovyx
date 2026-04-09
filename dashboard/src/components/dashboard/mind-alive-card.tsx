/**
 * Mind Alive card — post-onboarding state.
 *
 * Replaces the WelcomeBanner once all onboarding steps are complete.
 * Shows live metrics from the engine (concepts, episodes, conversations, messages).
 * Dismissable — once closed, the card doesn't return (localStorage persisted).
 *
 * DASH-08: Onboarding completion state.
 */

import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { BrainIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useDashboardStore } from "@/stores/dashboard";
import { formatUptime, formatNumber } from "@/lib/format";

// ── Metric Cell ──

interface MetricProps {
  value: string;
  label: string;
}

function Metric({ value, label }: MetricProps) {
  return (
    <div className="text-center" data-testid={`alive-metric-${label}`}>
      <div className="text-xl font-bold text-[var(--svx-color-text-primary)]">
        {value}
      </div>
      <div className="text-xs text-[var(--svx-color-text-secondary)]">
        {label}
      </div>
    </div>
  );
}

// ── Main Card ──

interface MindAliveCardProps {
  /** Whether this is the first render after transition (enables glow animation). */
  animate?: boolean;
  onDismiss: () => void;
}

export function MindAliveCard({ animate = false, onDismiss }: MindAliveCardProps) {
  const { t } = useTranslation("overview");
  const status = useDashboardStore((s) => s.status);

  // Graceful fallback when status hasn't loaded yet
  if (!status) {
    return (
      <div
        className="rounded-2xl border border-[var(--svx-color-border-subtle)] bg-gradient-to-br from-[var(--svx-color-bg-elevated)] to-[var(--svx-color-bg-surface)] p-6"
        data-testid="mind-alive-card"
      >
        <div className="flex items-center gap-3">
          <div className="size-8 animate-pulse rounded-lg bg-[var(--svx-color-bg-elevated)]" />
          <div className="h-5 w-48 animate-pulse rounded bg-[var(--svx-color-bg-elevated)]" />
        </div>
      </div>
    );
  }

  return (
    <div
      className={`relative rounded-2xl border border-[var(--svx-color-border-subtle)] bg-gradient-to-br from-[var(--svx-color-bg-elevated)] to-[var(--svx-color-bg-surface)] p-6 ${
        animate ? "animate-[glow-once_1s_ease-in-out_1]" : ""
      }`}
      data-testid="mind-alive-card"
    >
      {/* Dismiss */}
      <button
        onClick={onDismiss}
        className="absolute right-4 top-4 rounded-lg p-1 text-[var(--svx-color-text-secondary)] transition-colors duration-[var(--svx-duration-fast)] hover:bg-[var(--svx-color-bg-elevated)] hover:text-[var(--svx-color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--svx-color-brand-primary)]/50"
        aria-label={t("alive.dismiss", { defaultValue: "Close" })}
        data-testid="alive-dismiss"
      >
        <XIcon className="size-4" />
      </button>

      {/* Title */}
      <div className="mb-5 flex items-center gap-3">
        <BrainIcon className="size-6 text-[var(--svx-color-brand-primary)]" />
        <h2 className="text-lg font-bold text-[var(--svx-color-text-primary)]">
          {t("alive.title", { defaultValue: "Your mind is alive." })}
        </h2>
      </div>

      {/* Metrics Grid — 2 cols mobile, 4 cols desktop */}
      <div className="mb-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Metric
          value={formatNumber(status.memory_concepts)}
          label={t("alive.concepts", { defaultValue: "concepts" })}
        />
        <Metric
          value={formatNumber(status.memory_episodes)}
          label={t("alive.memories", { defaultValue: "memories" })}
        />
        <Metric
          value={formatNumber(status.active_conversations)}
          label={t("alive.channels", { defaultValue: "channels" })}
        />
        <Metric
          value={formatNumber(status.messages_today)}
          label={t("alive.messages", { defaultValue: "messages" })}
        />
      </div>

      {/* Subtitle — uptime */}
      <p className="mb-4 text-xs text-[var(--svx-color-text-secondary)]">
        {t("alive.activeFor", {
          duration: formatUptime(status.uptime_seconds),
          defaultValue: `Active for ${formatUptime(status.uptime_seconds)}`,
        })}
      </p>

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/brain">
          <Button variant="outline" size="sm" className="gap-1.5 text-xs">
            {t("alive.exploreBrain", { defaultValue: "Explore Brain" })}
            <BrainIcon className="size-3" />
          </Button>
        </Link>
        <Link to="/chat">
          <Button
            size="sm"
            className="gap-1.5 text-xs bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)] hover:bg-[var(--svx-color-brand-hover)]"
          >
            {t("alive.openChat", { defaultValue: "Open Chat" })}
          </Button>
        </Link>
      </div>
    </div>
  );
}
