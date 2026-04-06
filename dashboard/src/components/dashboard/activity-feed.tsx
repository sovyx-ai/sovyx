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

/** Build event summary string from WS event data via i18n templates. */
function eventSummary(event: WsEvent, t: TFunction): string {
  const d = event.data as Record<string, unknown>;
  const s = (key: string, fallback = "?") => String(d[key] ?? fallback);
  const n = (key: string, fallback = 0) => Number(d[key] ?? fallback);

  switch (event.type) {
    case "PerceptionReceived":
      return t("eventSummary.PerceptionReceived", { source: s("source"), person: s("person_id", "unknown") });
    case "ThinkCompleted":
      return t("eventSummary.ThinkCompleted", { model: s("model"), tokensIn: n("tokens_in"), tokensOut: n("tokens_out"), cost: n("cost_usd").toFixed(4) });
    case "ResponseSent":
      return t("eventSummary.ResponseSent", { channel: s("channel"), latency: s("latency_ms") });
    case "ConceptCreated":
      return t("eventSummary.ConceptCreated", { title: s("title", "unknown") });
    case "EpisodeEncoded":
      return t("eventSummary.EpisodeEncoded", { importance: n("importance").toFixed(2) });
    case "ServiceHealthChanged":
      return t("eventSummary.ServiceHealthChanged", { service: s("service"), status: s("status") });
    case "ConsolidationCompleted":
      return t("eventSummary.ConsolidationCompleted", { merged: s("merged", "0"), pruned: s("pruned", "0"), strengthened: s("strengthened", "0") });
    case "EngineStarted":
      return t("eventSummary.EngineStarted");
    case "EngineStopping":
      return t("eventSummary.EngineStopping", { reason: s("reason", "shutdown") });
    case "ChannelConnected":
      return t("eventSummary.ChannelConnected", { channel: s("channel_type") });
    case "ChannelDisconnected":
      return t("eventSummary.ChannelDisconnected", { channel: s("channel_type"), reason: s("reason", "unknown") });
    default:
      return JSON.stringify(d).slice(0, 60);
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
                      {eventSummary(event, t)}
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
