import { useTranslation } from "react-i18next";
import { DollarSignIcon, BrainIcon, MessageSquareIcon, ActivityIcon, MicIcon, HeartIcon, ListTodoIcon } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { StatCard, StatCardSkeleton, HealthGrid, ActivityFeed, MetricChart } from "@/components/dashboard";
import { formatUptime, formatCost, formatNumber } from "@/lib/format";
import { ComingSoon } from "@/components/coming-soon";

/**
 * Format a stat value for fresh-engine state.
 * Shows contextual text instead of dead "0" when the engine has no data yet.
 */
function freshValue(value: number, formatter: (n: number) => string, freshLabel: string): string {
  return value === 0 ? freshLabel : formatter(value);
}

export default function OverviewPage() {
  const { t } = useTranslation(["overview", "common"]);
  const status = useDashboardStore((s) => s.status);
  const healthChecks = useDashboardStore((s) => s.healthChecks);
  const connected = useDashboardStore((s) => s.connected);
  const recentEvents = useDashboardStore((s) => s.recentEvents);
  const costData = useDashboardStore((s) => s.costData);

  /** True when engine just started with no activity */
  const isFresh = status
    ? status.messages_today === 0 && status.memory_concepts === 0 && status.llm_calls_today === 0
    : false;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        <p className="text-sm text-[var(--svx-color-text-secondary)]">
          {isFresh ? t("subtitleFresh") : t("subtitle")}
        </p>
      </div>

      {/* 4 Stat Cards — skeleton while loading */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {!status ? (
          <>
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
          </>
        ) : (
          <>
            {/* Engine Status */}
            <StatCard
              title={t("cards.engineStatus")}
              value={connected ? t("common:status.online") : t("common:status.offline")}
              subtitle={
                isFresh && connected
                  ? t("cards.justBooted")
                  : t("cards.uptime", { duration: formatUptime(status.uptime_seconds) })
              }
              status={connected ? "green" : "red"}
              icon={<ActivityIcon className="size-4" />}
            />

            {/* Messages */}
            <StatCard
              title={t("cards.messages")}
              value={freshValue(status.messages_today, formatNumber, t("cards.messagesFresh"))}
              subtitle={
                status.active_conversations > 0
                  ? `${formatNumber(status.active_conversations)} active`
                  : t("cards.messagesHint")
              }
              icon={<MessageSquareIcon className="size-4" />}
            />

            {/* Brain */}
            <StatCard
              title={t("cards.brainConcepts")}
              value={freshValue(status.memory_concepts, formatNumber, t("cards.brainFresh"))}
              subtitle={
                status.memory_concepts > 0
                  ? t("cards.episodeCount", { count: status.memory_episodes })
                  : t("cards.brainHint")
              }
              icon={<BrainIcon className="size-4" />}
            />

            {/* LLM Cost */}
            <StatCard
              title={t("cards.llmCost")}
              value={freshValue(status.llm_cost_today, formatCost, t("cards.costFresh"))}
              subtitle={
                status.llm_calls_today > 0
                  ? `${formatNumber(status.llm_calls_today)} calls · ${formatNumber(status.tokens_today)} tokens`
                  : t("cards.costHint")
              }
              icon={<DollarSignIcon className="size-4" />}
            />
          </>
        )}
      </div>

      {/* Health Grid */}
      <HealthGrid checks={healthChecks} />

      {/* Cost Chart */}
      <MetricChart
        title={t("chart.costTitle")}
        data={costData}
        color="var(--chart-1)"
        unit="$"
        label="Cost"
      />

      {/* Activity Feed */}
      <ActivityFeed events={recentEvents} />

      {/* v1.0 Placeholders */}
      <div className="grid gap-4 md:grid-cols-3">
        <ComingSoon
          title="Voice"
          description="Real-time voice interaction with the engine."
          icon={<MicIcon className="size-10" />}
        />
        <ComingSoon
          title="Emotional Timeline"
          description="Mood and emotional state tracking over time."
          icon={<HeartIcon className="size-10" />}
        />
        <ComingSoon
          title="Tasks & Productivity"
          description="Task tracking, reminders, and productivity insights."
          icon={<ListTodoIcon className="size-10" />}
        />
      </div>
    </div>
  );
}
