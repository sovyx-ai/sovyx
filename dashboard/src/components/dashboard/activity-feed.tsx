/**
 * ActivityFeed — Real-time cognitive cycle event stream.
 *
 * Displays WS events with per-type Lucide icons, timestamps,
 * model name and cost on ThinkCompleted events.
 * Auto-follow with break-on-scroll pattern (handled by parent).
 *
 * Ref: Architecture §3.1, META-04 §6
 */

import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  EyeIcon,
  BrainIcon,
  MessageSquareIcon,
  LightbulbIcon,
  BookmarkIcon,
  MergeIcon,
  PlugIcon,
  RocketIcon,
  SquareIcon,
  AlertTriangleIcon,
  CircleHelpIcon,
} from "lucide-react";
import type { WsEvent, WsEventType } from "@/types/api";
import { formatTimePrecise } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

interface ActivityFeedProps {
  events: WsEvent[];
  className?: string;
}

const EVENT_CONFIG: Record<
  WsEventType,
  { icon: ReactNode; color: string }
> = {
  PerceptionReceived: {
    icon: <EyeIcon className="size-3.5" />,
    color: "text-[var(--svx-color-info)]",
  },
  ThinkCompleted: {
    icon: <BrainIcon className="size-3.5" />,
    color: "text-[var(--svx-color-brand-primary)]",
  },
  ResponseSent: {
    icon: <MessageSquareIcon className="size-3.5" />,
    color: "text-[var(--svx-color-info)]",
  },
  ConceptCreated: {
    icon: <LightbulbIcon className="size-3.5" />,
    color: "text-[var(--svx-color-accent-cyan)]",
  },
  EpisodeEncoded: {
    icon: <BookmarkIcon className="size-3.5" />,
    color: "text-[var(--svx-color-brand-muted)]",
  },
  ServiceHealthChanged: {
    icon: <AlertTriangleIcon className="size-3.5" />,
    color: "text-[var(--svx-color-warning)]",
  },
  ConsolidationCompleted: {
    icon: <MergeIcon className="size-3.5" />,
    color: "text-[var(--svx-color-brand-primary)]",
  },
  EngineStarted: {
    icon: <RocketIcon className="size-3.5" />,
    color: "text-[var(--svx-color-success)]",
  },
  EngineStopping: {
    icon: <SquareIcon className="size-3.5" />,
    color: "text-[var(--svx-color-warning)]",
  },
  ChannelConnected: {
    icon: <PlugIcon className="size-3.5" />,
    color: "text-[var(--svx-color-success)]",
  },
  ChannelDisconnected: {
    icon: <PlugIcon className="size-3.5" />,
    color: "text-[var(--svx-color-error)]",
  },
};

const FALLBACK_CONFIG = {
  icon: <CircleHelpIcon className="size-3.5" />,
  color: "text-[var(--svx-color-text-tertiary)]",
};

/** Resolve event type label from i18n. */
function eventLabel(type: string, t: TFunction): string {
  return t(`events.${type}`, { defaultValue: t("events.unknown") });
}

function eventSummary(event: WsEvent): string {
  const data = event.data as Record<string, unknown>;
  switch (event.type) {
    case "PerceptionReceived":
      return `from ${String(data["source"] ?? "?")} (${String(data["person_id"] ?? "unknown")})`;
    case "ThinkCompleted":
      return `${String(data["model"] ?? "?")} — ${Number(data["tokens_in"] ?? 0)}+${Number(data["tokens_out"] ?? 0)} tokens — $${Number(data["cost_usd"] ?? 0).toFixed(4)}`;
    case "ResponseSent":
      return `via ${String(data["channel"] ?? "?")} (${String(data["latency_ms"] ?? "?")}ms)`;
    case "ConceptCreated":
      return `Created: ${String(data["title"] ?? "unknown")}`;
    case "EpisodeEncoded":
      return `importance: ${Number(data["importance"] ?? 0).toFixed(2)}`;
    case "ServiceHealthChanged":
      return `${String(data["service"] ?? "?")}: ${String(data["status"] ?? "?")}`;
    case "ConsolidationCompleted":
      return `merged: ${String(data["merged"] ?? 0)}, pruned: ${String(data["pruned"] ?? 0)}, strengthened: ${String(data["strengthened"] ?? 0)}`;
    case "EngineStarted":
      return "Engine started";
    case "EngineStopping":
      return `Stopping: ${String(data["reason"] ?? "shutdown")}`;
    case "ChannelConnected":
      return `${String(data["channel_type"] ?? "?")} connected`;
    case "ChannelDisconnected":
      return `${String(data["channel_type"] ?? "?")} disconnected: ${String(data["reason"] ?? "unknown")}`;
    default:
      return JSON.stringify(data).slice(0, 60);
  }
}

export function ActivityFeed({ events, className }: ActivityFeedProps) {
  const { t } = useTranslation("overview");
  const reversed = [...events].reverse();
  const getLabel = (type: string) => eventLabel(type, t);

  return (
    <div
      className={cn(
        "rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4",
        className,
      )}
    >
      {/* Header */}
      <h2 className="mb-3 text-sm font-medium text-[var(--svx-color-text-primary)]">
        {t("feed.title")}
      </h2>

      <ScrollArea className="h-64">
        {reversed.length === 0 ? (
          <div className="flex flex-col items-center gap-1.5 py-8 text-center">
            <div className="flex items-center gap-1.5 text-xs text-[var(--svx-color-text-tertiary)]">
              <span className="inline-block size-1.5 animate-[pulse-dot_2s_ease-in-out_infinite] rounded-full bg-[var(--svx-color-brand-primary)]" />
              {t("feed.empty")}
            </div>
            <p className="text-[10px] text-[var(--svx-color-text-disabled)]">
              {t("feed.emptyHint")}
            </p>
          </div>
        ) : (
          <div className="space-y-0.5" role="log" aria-label="Activity feed" aria-live="polite">
            {reversed.map((event, i) => {
              const config = EVENT_CONFIG[event.type] ?? FALLBACK_CONFIG;
              const label = getLabel(event.type);
              return (
                <div
                  key={`${event.timestamp}-${i}`}
                  className="flex items-start gap-3 rounded-[var(--svx-radius-md)] px-2 py-1.5 text-xs transition-colors hover:bg-[var(--svx-color-bg-hover)]"
                  role="article"
                  aria-label={`${label} at ${formatTimePrecise(event.timestamp)}`}
                >
                  <span
                    className={cn("mt-0.5 shrink-0", config.color)}
                    aria-hidden="true"
                  >
                    {config.icon}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className={cn("font-medium", config.color)}>
                        {label}
                      </span>
                      <span className="text-[var(--svx-color-text-tertiary)]">
                        {formatTimePrecise(event.timestamp)}
                      </span>
                    </div>
                    <p className="truncate text-[var(--svx-color-text-tertiary)]">
                      {eventSummary(event)}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
