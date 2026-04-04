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
  "conversation.message": {
    icon: "💬",
    color: "text-[var(--color-info)]",
    label: "Message",
  },
  "cognitive.transition": {
    icon: "⚡",
    color: "text-[var(--color-warning)]",
    label: "Cognitive",
  },
  "brain.concept_created": {
    icon: "🧠",
    color: "text-primary",
    label: "Concept",
  },
  "health.alert": {
    icon: "🔴",
    color: "text-destructive",
    label: "Health",
  },
  "llm.response": {
    icon: "🤖",
    color: "text-[var(--color-success)]",
    label: "LLM",
  },
  "log.entry": {
    icon: "📋",
    color: "text-muted-foreground",
    label: "Log",
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
  const payload = event.payload as Record<string, unknown>;
  switch (event.type) {
    case "conversation.message":
      return `${payload["participant"] ?? "user"}: ${String(payload["content"] ?? "").slice(0, 60)}`;
    case "brain.concept_created":
      return `Created: ${String(payload["label"] ?? "unknown")}`;
    case "llm.response":
      return `${payload["model"] ?? "?"} — ${payload["tokens"] ?? "?"} tokens`;
    case "health.alert":
      return `${payload["check"] ?? "?"}: ${payload["status"] ?? "?"}`;
    case "cognitive.transition":
      return `${payload["from"] ?? "?"} → ${payload["to"] ?? "?"}`;
    case "log.entry":
      return String(payload["message"] ?? "");
    default:
      return JSON.stringify(payload).slice(0, 60);
  }
}

export function ActivityFeed({ events, className }: ActivityFeedProps) {
  const reversed = [...events].reverse();

  return (
    <Card className={className}>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">Recent Activity</CardTitle>
      </CardHeader>
      <CardContent>
        <ScrollArea className="h-64">
          {reversed.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              No activity yet. Events will appear here in real-time.
            </p>
          ) : (
            <div className="space-y-1">
              {reversed.map((event, i) => {
                const config = EVENT_CONFIG[event.type];
                return (
                  <div
                    key={`${event.timestamp}-${i}`}
                    className="flex items-start gap-3 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-secondary"
                  >
                    <span className="mt-0.5 shrink-0 text-sm">
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
