import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useDashboardStore } from "@/stores/dashboard";

export default function OverviewPage() {
  const status = useDashboardStore((s) => s.status);
  const healthChecks = useDashboardStore((s) => s.healthChecks);
  const connected = useDashboardStore((s) => s.connected);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Overview</h1>

      {/* Stat cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Card className="glass">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Status
            </CardTitle>
            <span className={connected ? "status-dot-green" : "status-dot-red"} />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {connected ? "Online" : "Offline"}
            </div>
            <p className="text-xs text-muted-foreground">
              {status
                ? `${status.active_conversations} active conversations`
                : "Connecting..."}
            </p>
          </CardContent>
        </Card>

        <Card className="glass">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              LLM Cost Today
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-primary">
              ${status?.llm_cost_today.toFixed(2) ?? "—"}
            </div>
            <p className="text-xs text-muted-foreground">
              {status
                ? `${status.llm_calls_today} calls · ${status.tokens_today.toLocaleString()} tokens`
                : "—"}
            </p>
          </CardContent>
        </Card>

        <Card className="glass">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Memory
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {status?.memory_concepts.toLocaleString() ?? "—"}
            </div>
            <p className="text-xs text-muted-foreground">
              {status
                ? `concepts · ${status.memory_episodes.toLocaleString()} episodes`
                : "—"}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Health checks */}
      {healthChecks.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Health Checks</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-3">
              {healthChecks.map((check) => (
                <div
                  key={check.name}
                  className="flex items-center gap-2 rounded-md bg-secondary px-3 py-1.5 text-xs"
                >
                  <span
                    className={
                      check.status === "GREEN"
                        ? "status-dot-green"
                        : check.status === "YELLOW"
                          ? "status-dot-yellow"
                          : "status-dot-red"
                    }
                  />
                  {check.name}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
