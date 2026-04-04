import { DollarSign, Brain } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { StatCard, HealthGrid, ActivityFeed, MetricChart } from "@/components/dashboard";

export default function OverviewPage() {
  const status = useDashboardStore((s) => s.status);
  const healthChecks = useDashboardStore((s) => s.healthChecks);
  const connected = useDashboardStore((s) => s.connected);
  const recentEvents = useDashboardStore((s) => s.recentEvents);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Overview</h1>

      {/* Stat cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <StatCard
          title="Status"
          value={connected ? "Online" : "Offline"}
          subtitle={
            status
              ? `${status.active_conversations} active conversations`
              : "Connecting..."
          }
          status={connected ? "green" : "red"}
        />
        <StatCard
          title="LLM Cost Today"
          value={`$${status?.llm_cost_today.toFixed(2) ?? "—"}`}
          subtitle={
            status
              ? `${status.llm_calls_today} calls · ${status.tokens_today.toLocaleString()} tokens`
              : undefined
          }
          icon={<DollarSign className="size-4" />}
        />
        <StatCard
          title="Memory"
          value={status?.memory_concepts.toLocaleString() ?? "—"}
          subtitle={
            status
              ? `concepts · ${status.memory_episodes.toLocaleString()} episodes`
              : undefined
          }
          icon={<Brain className="size-4" />}
        />
      </div>

      {/* Health grid */}
      <HealthGrid checks={healthChecks} />

      {/* Charts */}
      <div className="grid gap-4 md:grid-cols-2">
        <MetricChart
          title="LLM Cost (24h)"
          data={[]}
          color="var(--color-chart-1)"
          unit="$"
        />
        <MetricChart
          title="Latency (24h)"
          data={[]}
          color="var(--color-chart-3)"
          unit="ms"
        />
      </div>

      {/* Activity feed */}
      <ActivityFeed events={recentEvents} />

    </div>
  );
}
