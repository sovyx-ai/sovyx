import { useTranslation } from "react-i18next";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { WsEvent, WsEventType } from "@/types/api";
import { cn } from "@/lib/utils";

interface ActivityFeedProps {
  events: WsEvent[];
  className?: string;
}

const EVENT_CONFIG: Record<
  WsEventType,
  { icon: string; color: string; label: string }
> = {
  PerceptionReceived: {
    icon: "💬",
    color: "text-[var(--color-info)]",
    label: "Message",
  },
  ThinkCompleted: {
    icon: "⚡",
    color: "text-[var(--color-warning)]",
    label: "Think",
  },
  ResponseSent: {
    icon: "📤",
    color: "text-[var(--color-info)]",
    label: "Response",
  },
  ConceptCreated: {
    icon: "🧠",
    color: "text-primary",
    label: "Concept",
  },
  EpisodeEncoded: {
    icon: "🧠",
    color: "text-primary/70",
    label: "Episode",
  },
  ServiceHealthChanged: {
    icon: "🔴",
    color: "text-destructive",
    label: "Health",
  },
  ConsolidationCompleted: {
    icon: "🔄",
    color: "text-primary",
    label: "Consolidation",
  },
  EngineStarted: {
    icon: "🚀",
    color: "text-[var(--color-success)]",
    label: "Engine",
  },
  EngineStopping: {
    icon: "⏹",
    color: "text-[var(--color-warning)]",
    label: "Engine",
  },
  ChannelConnected: {
    icon: "🔗",
    color: "text-[var(--color-success)]",
    label: "Channel",
  },
  ChannelDisconnected: {
    icon: "🔌",
    color: "text-destructive",
    label: "Channel",
  },
};

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return "—";
  }
}

function eventSummary(event: WsEvent): string {
  const data = event.data as Record<string, unknown>;
  switch (event.type) {
    case "PerceptionReceived":
      return `from ${data["source"] ?? "?"} (${data["person_id"] ?? "unknown"})`;
    case "ThinkCompleted":
      return `${data["model"] ?? "?"} — ${data["tokens_in"] ?? 0}+${data["tokens_out"] ?? 0} tokens — $${Number(data["cost_usd"] ?? 0).toFixed(4)}`;
    case "ResponseSent":
      return `via ${data["channel"] ?? "?"} (${data["latency_ms"] ?? "?"}ms)`;
    case "ConceptCreated":
      return `Created: ${String(data["title"] ?? "unknown")}`;
    case "EpisodeEncoded":
      return `importance: ${Number(data["importance"] ?? 0).toFixed(2)}`;
    case "ServiceHealthChanged":
      return `${data["service"] ?? "?"}: ${data["status"] ?? "?"}`;
    case "ConsolidationCompleted":
      return `merged: ${data["merged"] ?? 0}, pruned: ${data["pruned"] ?? 0}, strengthened: ${data["strengthened"] ?? 0}`;
    case "EngineStarted":
      return "Engine started";
    case "EngineStopping":
      return `Stopping: ${data["reason"] ?? "shutdown"}`;
    case "ChannelConnected":
      return `${data["channel_type"] ?? "?"} connected`;
    case "ChannelDisconnected":
      return `${data["channel_type"] ?? "?"} disconnected: ${data["reason"] ?? "unknown"}`;
    default:
      return JSON.stringify(data).slice(0, 60);
  }
}

export function ActivityFeed({ events, className }: ActivityFeedProps) {
  const { t } = useTranslation("overview");
  const reversed = [...events].reverse();

  return (
    <Card className={className}>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{t("feed.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <ScrollArea className="h-64">
          {reversed.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              {t("feed.empty")}
            </p>
          ) : (
            <div className="space-y-1" role="log" aria-label="Activity feed" aria-live="polite">
              {reversed.map((event, i) => {
                const config = EVENT_CONFIG[event.type] ?? {
                  icon: "❓",
                  color: "text-muted-foreground",
                  label: event.type,
                };
                return (
                  <div
                    key={`${event.timestamp}-${i}`}
                    className="flex items-start gap-3 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-secondary"
                    role="article"
                    aria-label={`${config.label} at ${formatTime(event.timestamp)}`}
                  >
                    <span className="mt-0.5 shrink-0 text-sm" aria-hidden="true">
                      {config.icon}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className={cn("font-medium", config.color)}>
                          {config.label}
                        </span>
                        <span className="text-muted-foreground">
                          {formatTime(event.timestamp)}
                        </span>
                      </div>
                      <p className="truncate text-muted-foreground">
                        {eventSummary(event)}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
