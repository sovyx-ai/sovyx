/**
 * CognitiveTimeline — persistent activity history from the database.
 *
 * Fetches /api/activity/timeline and displays grouped entries:
 * conversations, messages, concepts learned, episodes encoded, consolidations.
 * Unlike the LiveFeed (WS-only), this always shows data after page refresh.
 *
 * Time grouping: "Just now" (<5min), "Earlier today", "Yesterday", date headers.
 */

import { memo, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  MessageSquareIcon,
  LightbulbIcon,
  BookmarkIcon,
  MergeIcon,
  MessageCircleIcon,
} from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import type { TimelineEntry } from "@/types/api";

// ── Time grouping ──

type TimeGroup = "just_now" | "earlier_today" | "yesterday" | string;

function getTimeGroup(timestamp: string): TimeGroup {
  const now = Date.now();
  const ts = new Date(timestamp).getTime();
  const diffMs = now - ts;
  const diffMin = diffMs / 60_000;
  const diffHours = diffMs / 3_600_000;

  if (diffMin < 5) return "just_now";
  if (diffHours < 24) return "earlier_today";
  if (diffHours < 48) return "yesterday";

  // Return date string for older entries
  return new Date(timestamp).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

function groupEntries(entries: TimelineEntry[]): Map<TimeGroup, TimelineEntry[]> {
  const groups = new Map<TimeGroup, TimelineEntry[]>();
  for (const entry of entries) {
    const group = getTimeGroup(entry.timestamp);
    const existing = groups.get(group) ?? [];
    existing.push(entry);
    groups.set(group, existing);
  }
  return groups;
}

// ── Entry icons ──

function EntryIcon({ type }: { type: string }) {
  const cls = "size-3.5 shrink-0";
  switch (type) {
    case "conversation":
      return <MessageCircleIcon className={cls} />;
    case "message":
      return <MessageSquareIcon className={cls} />;
    case "concepts_learned":
      return <LightbulbIcon className={cls} />;
    case "episode_encoded":
      return <BookmarkIcon className={cls} />;
    case "consolidation":
      return <MergeIcon className={cls} />;
    default:
      return <MessageSquareIcon className={cls} />;
  }
}

// ── Entry type colors ──

function entryColor(type: string): string {
  switch (type) {
    case "conversation":
      return "var(--svx-color-accent-primary)";
    case "message":
      return "var(--svx-color-text-secondary)";
    case "concepts_learned":
      return "var(--svx-color-accent-warning)";
    case "episode_encoded":
      return "var(--svx-color-accent-success)";
    case "consolidation":
      return "var(--svx-color-accent-info)";
    default:
      return "var(--svx-color-text-tertiary)";
  }
}

// ── Format timestamp ──

function formatTime(timestamp: string): string {
  const d = new Date(timestamp);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

// ── Entry summary ──

function entrySummary(entry: TimelineEntry, t: (key: string, opts?: Record<string, unknown>) => string): string {
  const d = entry.data;
  switch (entry.type) {
    case "conversation":
      return t("timeline.conversation", {
        channel: (d.channel as string) ?? "unknown",
        messages: (d.message_count as number) ?? 0,
      });
    case "message":
      return (d.preview as string) ?? t("timeline.message");
    case "concepts_learned": {
      const names = (d.names as string[]) ?? [];
      const count = (d.count as number) ?? names.length;
      if (names.length === 0) return t("timeline.conceptsLearned", { count });
      return names.slice(0, 3).join(", ") + (count > 3 ? ` +${count - 3}` : "");
    }
    case "episode_encoded":
      return t("timeline.episodeEncoded", {
        importance: ((d.importance as number) ?? 0).toFixed(1),
      });
    case "consolidation":
      return t("timeline.consolidation", {
        merged: (d.merged as number) ?? 0,
        pruned: (d.pruned as number) ?? 0,
        strengthened: (d.strengthened as number) ?? 0,
      });
    default:
      return JSON.stringify(d).slice(0, 60);
  }
}

// ── Entry role badge ──

function RoleBadge({ role }: { role?: string }) {
  if (!role) return null;
  const isUser = role === "user";
  return (
    <span
      className="ml-1.5 rounded px-1 py-0.5 text-[10px] font-medium leading-none"
      style={{
        background: isUser
          ? "var(--svx-color-accent-primary)"
          : "var(--svx-color-accent-success)",
        color: "var(--svx-color-bg-surface)",
      }}
    >
      {isUser ? "YOU" : "AI"}
    </span>
  );
}

// ── Concept chips ──

function ConceptChips({ names }: { names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {names.slice(0, 5).map((name) => (
        <span
          key={name}
          className="rounded-full px-1.5 py-0.5 text-[10px] font-medium leading-none"
          style={{
            background: "var(--svx-color-bg-elevated)",
            color: "var(--svx-color-text-secondary)",
            border: "1px solid var(--svx-color-border-default)",
          }}
        >
          {name}
        </span>
      ))}
    </div>
  );
}

// ── Importance dot ──

function ImportanceDot({ importance }: { importance: number }) {
  const color =
    importance >= 0.7
      ? "var(--svx-color-accent-success)"
      : importance >= 0.4
        ? "var(--svx-color-accent-warning)"
        : "var(--svx-color-text-tertiary)";
  return (
    <span
      className="ml-1.5 inline-block size-1.5 rounded-full"
      style={{ background: color }}
      title={`importance: ${importance.toFixed(2)}`}
    />
  );
}

// ── Single entry row ──

const TimelineRow = memo(function TimelineRow({ entry, t }: { entry: TimelineEntry; t: (key: string, opts?: Record<string, unknown>) => string }) {
  const d = entry.data;
  return (
    <div className="group flex items-start gap-2.5 py-1.5 transition-colors hover:bg-[var(--svx-color-bg-elevated)]">
      {/* Icon */}
      <div
        className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded"
        style={{ color: entryColor(entry.type) }}
      >
        <EntryIcon type={entry.type} />
      </div>

      {/* Content */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1">
          <span className="truncate text-xs text-[var(--svx-color-text-primary)]">
            {entrySummary(entry, t)}
          </span>
          {entry.type === "message" && (
            <RoleBadge role={d.role as string} />
          )}
          {entry.type === "episode_encoded" && (
            <ImportanceDot importance={(d.importance as number) ?? 0} />
          )}
        </div>

        {/* Concept chips for concepts_learned */}
        {entry.type === "concepts_learned" && (
          <ConceptChips names={(d.names as string[]) ?? []} />
        )}

        {/* Model badge for messages */}
        {entry.type === "message" && typeof d.model === "string" && (
          <span className="mt-0.5 inline-block text-[10px] text-[var(--svx-color-text-tertiary)]">
            {d.model}
            {typeof d.cost_usd === "number" && d.cost_usd > 0 ? ` · $${d.cost_usd.toFixed(4)}` : ""}
          </span>
        )}
      </div>

      {/* Timestamp */}
      <span className="shrink-0 text-[10px] tabular-nums text-[var(--svx-color-text-tertiary)]">
        {formatTime(entry.timestamp)}
      </span>
    </div>
  );
});

// ── Group header label ──

function groupLabel(group: TimeGroup, t: (key: string) => string): string {
  switch (group) {
    case "just_now":
      return t("timeline.groupJustNow");
    case "earlier_today":
      return t("timeline.groupEarlierToday");
    case "yesterday":
      return t("timeline.groupYesterday");
    default:
      return group; // Already formatted date string
  }
}

// ── Skeleton ──

function TimelineSkeleton() {
  return (
    <div className="space-y-3 p-2">
      {[1, 2, 3].map((i) => (
        <div key={i} className="flex items-center gap-2.5">
          <div className="size-5 animate-pulse rounded bg-[var(--svx-color-bg-elevated)]" />
          <div className="h-3 flex-1 animate-pulse rounded bg-[var(--svx-color-bg-elevated)]" />
          <div className="h-3 w-10 animate-pulse rounded bg-[var(--svx-color-bg-elevated)]" />
        </div>
      ))}
    </div>
  );
}

// ── Main component ──

interface CognitiveTimelineProps {
  className?: string;
}

/**
 * Flattened view of the timeline: a stream of alternating group headers and
 * entry rows. This lets the virtualizer own the scroll offset for the whole
 * thing instead of nesting virtualizers per group.
 */
type FlatItem =
  | { kind: "header"; group: TimeGroup; key: string }
  | { kind: "row"; entry: TimelineEntry; key: string };

function flattenGroups(groups: Map<TimeGroup, TimelineEntry[]>): FlatItem[] {
  const out: FlatItem[] = [];
  for (const [group, rows] of groups) {
    out.push({ kind: "header", group, key: `h:${group}` });
    for (let i = 0; i < rows.length; i++) {
      const entry = rows[i]!;
      out.push({ kind: "row", entry, key: `r:${entry.timestamp}:${i}` });
    }
  }
  return out;
}

export function CognitiveTimeline({ className }: CognitiveTimelineProps) {
  const { t } = useTranslation("overview");
  const entries = useDashboardStore((s) => s.timelineEntries);
  const isLoading = useDashboardStore((s) => s.isLoadingTimeline);
  const fetchTimeline = useDashboardStore((s) => s.fetchTimeline);
  const connected = useDashboardStore((s) => s.connected);

  // Fetch on mount and when WS reconnects
  useEffect(() => {
    let cancelled = false;
    void fetchTimeline().catch(() => {
      if (!cancelled) { /* handled in store */ }
    });
    return () => { cancelled = true; };
  }, [fetchTimeline, connected]);

  const groups = useMemo(() => groupEntries(entries), [entries]);
  const items = useMemo(() => flattenGroups(groups), [groups]);

  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    // Headers render taller than rows — use a single estimate tuned
    // between the two and let measureElement correct each item.
    estimateSize: (index) => (items[index]?.kind === "header" ? 24 : 40),
    overscan: 6,
    getItemKey: (index) => items[index]?.key ?? index,
  });

  return (
    <div
      className={[
        "rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {/* Header */}
      <h2 className="mb-3 text-sm font-medium text-[var(--svx-color-text-primary)]">
        {t("timeline.title")}
      </h2>

      {isLoading ? (
        <div className="h-72 pr-3">
          <TimelineSkeleton />
        </div>
      ) : entries.length === 0 ? (
        <div className="flex h-72 flex-col items-center justify-center gap-1.5 py-8 text-center">
          <div className="flex items-center gap-1.5 text-xs text-[var(--svx-color-text-tertiary)]">
            <LightbulbIcon className="size-3.5" />
            {t("timeline.empty")}
          </div>
          <p className="text-[10px] text-[var(--svx-color-text-tertiary)]">
            {t("timeline.emptyHint")}
          </p>
        </div>
      ) : (
        <div
          ref={parentRef}
          className="h-72 overflow-auto pr-3 contain-strict"
          style={{ overflowAnchor: "none" }}
          role="feed"
          aria-label={t("common:aria.cognitiveTimeline")}
        >
          <div
            style={{
              height: virtualizer.getTotalSize(),
              width: "100%",
              position: "relative",
            }}
          >
            {virtualizer.getVirtualItems().map((vrow) => {
              const item = items[vrow.index];
              if (!item) return null;
              return (
                <div
                  key={vrow.key}
                  data-index={vrow.index}
                  ref={virtualizer.measureElement}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${vrow.start}px)`,
                  }}
                >
                  {item.kind === "header" ? (
                    <div className="mb-1 bg-[var(--svx-color-bg-surface)] pb-1">
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
                        {groupLabel(item.group, t)}
                      </span>
                    </div>
                  ) : (
                    <TimelineRow entry={item.entry} t={t} />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
