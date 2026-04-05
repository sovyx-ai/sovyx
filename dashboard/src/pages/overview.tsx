import { useTranslation } from "react-i18next";
import { DollarSignIcon, BrainIcon, MessageSquareIcon, ActivityIcon } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { StatCard, HealthGrid, ActivityFeed, MetricChart } from "@/components/dashboard";
import { formatUptime, formatCost, formatNumber } from "@/lib/format";

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
        <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
      </div>

      {/* 4 Stat Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {/* Engine Status */}
        <StatCard
          title={t("cards.engineStatus")}
          value={connected ? t("common:status.online") : t("common:status.offline")}
          subtitle={
            status
              ? t("cards.uptime", { duration: formatUptime(status.uptime_seconds) })
              : t("common:status.loading")
          }
          status={connected ? "green" : "red"}
          icon={<ActivityIcon className="size-4" />}
        />

        {/* Messages */}
        <StatCard
          title={t("cards.messages")}
          value={status ? formatNumber(status.messages_today) : "—"}
          subtitle={
            status
              ? `${formatNumber(status.active_conversations)} active`
              : undefined
          }
          icon={<MessageSquareIcon className="size-4" />}
        />

        {/* Brain */}
        <StatCard
          title={t("cards.brainConcepts")}
          value={status ? formatNumber(status.memory_concepts) : "—"}
          subtitle={
            status
              ? t("cards.episodeCount", { count: status.memory_episodes })
              : undefined
          }
          icon={<BrainIcon className="size-4" />}
        />

        {/* LLM Cost */}
        <StatCard
          title={t("cards.llmCost")}
          value={status ? formatCost(status.llm_cost_today) : "—"}
          subtitle={
            status
              ? t("cards.callsToday", { count: status.llm_calls_today })
              : undefined
          }
          icon={<DollarSignIcon className="size-4" />}
        />
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
    </div>
  );
}
