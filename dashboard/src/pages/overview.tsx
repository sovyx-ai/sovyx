import { useTranslation } from "react-i18next";
import { DollarSignIcon, BrainIcon, MessageSquareIcon, ActivityIcon, MicIcon, HeartIcon, ListTodoIcon } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { StatCard, StatCardSkeleton, HealthGrid, ActivityFeed, MetricChart } from "@/components/dashboard";
import { formatUptime, formatCost, formatNumber } from "@/lib/format";
import { ComingSoon } from "@/components/coming-soon";
import { WelcomeBanner } from "@/components/dashboard/welcome-banner";
import { ChannelStatusCard } from "@/components/dashboard/channel-status";

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

      {/* Welcome Banner — shown on first use */}
      {isFresh && connected && <WelcomeBanner />}

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
                  ? t("cards.activeCount", { count: formatNumber(status.active_conversations) })
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
                  ? t("cards.costSubtitle", { calls: formatNumber(status.llm_calls_today), tokens: formatNumber(status.tokens_today) })
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
        label={t("chart.costLabel")}
      />

      {/* Channel Status + Activity Feed */}
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-1">
          <ChannelStatusCard />
        </div>
        <div className="lg:col-span-2">
          <ActivityFeed events={recentEvents} />
        </div>
      </div>

      {/* v1.0 Placeholders */}
      <div className="grid gap-4 md:grid-cols-3">
        <ComingSoon
          title={t("placeholders.voice")}
          description={t("placeholders.voiceDesc")}
          icon={<MicIcon className="size-10" />}
        />
        <ComingSoon
          title={t("placeholders.emotional")}
          description={t("placeholders.emotionalDesc")}
          icon={<HeartIcon className="size-10" />}
        />
        <ComingSoon
          title={t("placeholders.tasks")}
          description={t("placeholders.tasksDesc")}
          icon={<ListTodoIcon className="size-10" />}
        />
      </div>
    </div>
  );
}
