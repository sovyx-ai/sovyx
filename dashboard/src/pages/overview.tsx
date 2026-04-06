import { useTranslation } from "react-i18next";
import { DollarSignIcon, BrainIcon, MessageSquareIcon, ActivityIcon, MicIcon, HeartIcon, ListTodoIcon } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { StatCard, StatCardSkeleton, HealthGrid, ActivityFeed, MetricChart } from "@/components/dashboard";
import { formatUptime, formatCost, formatNumber } from "@/lib/format";
import { ComingSoon } from "@/components/coming-soon";

export default function OverviewPage() {
  const { t } = useTranslation(["overview", "common"]);
  const status = useDashboardStore((s) => s.status);
  const healthChecks = useDashboardStore((s) => s.healthChecks);
  const connected = useDashboardStore((s) => s.connected);
  const recentEvents = useDashboardStore((s) => s.recentEvents);
  const costData = useDashboardStore((s) => s.costData);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        <p className="text-sm text-[var(--svx-color-text-secondary)]">{t("subtitle")}</p>
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
              subtitle={t("cards.uptime", { duration: formatUptime(status.uptime_seconds) })}
              status={connected ? "green" : "red"}
              icon={<ActivityIcon className="size-4" />}
            />

            {/* Messages */}
            <StatCard
              title={t("cards.messages")}
              value={formatNumber(status.messages_today)}
              subtitle={`${formatNumber(status.active_conversations)} active`}
              icon={<MessageSquareIcon className="size-4" />}
            />

            {/* Brain */}
            <StatCard
              title={t("cards.brainConcepts")}
              value={formatNumber(status.memory_concepts)}
              subtitle={t("cards.episodeCount", { count: status.memory_episodes })}
              icon={<BrainIcon className="size-4" />}
            />

            {/* LLM Cost */}
            <StatCard
              title={t("cards.llmCost")}
              value={formatCost(status.llm_cost_today)}
              subtitle={`${formatNumber(status.llm_calls_today)} calls · ${formatNumber(status.tokens_today)} tokens`}
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
