/**
 * Plugins page — plugin management dashboard.
 *
 * Grid view with stat cards, search, filters, sort.
 * Connects to zustand plugin slice for data + real-time updates.
 *
 * TASK-455
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { PuzzleIcon, WrenchIcon, PowerIcon, AlertTriangleIcon } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { StatCard } from "@/components/dashboard/stat-card";
import { PluginCard } from "@/components/dashboard/plugin-card";
import { PluginDetailPanel } from "@/components/dashboard/plugin-detail";
import { PluginsSkeleton } from "@/components/skeletons";
import type { PluginInfo } from "@/types/api";
import type { PluginFilter, PluginSort } from "@/stores/slices/plugins";

// ── Filter / Sort Logic ──

function filterPlugins(
  plugins: PluginInfo[],
  filter: PluginFilter,
  search: string,
): PluginInfo[] {
  let result = plugins;

  // Status filter
  if (filter !== "all") {
    result = result.filter((p) => p.status === filter);
  }

  // Search filter (name + description, case-insensitive)
  if (search.trim()) {
    const q = search.toLowerCase();
    result = result.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        p.tags.some((tag) => tag.toLowerCase().includes(q)),
    );
  }

  return result;
}

function sortPlugins(plugins: PluginInfo[], sort: PluginSort): PluginInfo[] {
  const copy = [...plugins];
  switch (sort) {
    case "name":
      return copy.sort((a, b) => a.name.localeCompare(b.name));
    case "status": {
      const order = { active: 0, error: 1, disabled: 2 };
      return copy.sort(
        (a, b) =>
          (order[a.status] ?? 3) - (order[b.status] ?? 3) ||
          a.name.localeCompare(b.name),
      );
    }
    case "tools":
      return copy.sort(
        (a, b) => b.tools_count - a.tools_count || a.name.localeCompare(b.name),
      );
    default:
      return copy;
  }
}

// ── Filter Tabs ──

const FILTERS: PluginFilter[] = ["all", "active", "disabled", "error"];

function FilterTabs({
  current,
  onChange,
}: {
  current: PluginFilter;
  onChange: (f: PluginFilter) => void;
}) {
  const { t } = useTranslation("plugins");

  return (
    <div className="flex gap-1" role="tablist" aria-label="Plugin filter">
      {FILTERS.map((f) => (
        <button
          key={f}
          type="button"
          role="tab"
          aria-selected={current === f}
          onClick={() => onChange(f)}
          className={`rounded-[var(--svx-radius-md)] px-3 py-1.5 text-xs font-medium transition-colors ${
            current === f
              ? "bg-[var(--svx-color-brand-primary)] text-white"
              : "text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-bg-elevated)]"
          }`}
        >
          {t(`filter.${f}`)}
        </button>
      ))}
    </div>
  );
}

// ── Sort Dropdown ──

const SORTS: PluginSort[] = ["name", "status", "tools"];

function SortSelect({
  current,
  onChange,
}: {
  current: PluginSort;
  onChange: (s: PluginSort) => void;
}) {
  const { t } = useTranslation("plugins");

  return (
    <select
      value={current}
      onChange={(e) => onChange(e.target.value as PluginSort)}
      className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-2 py-1.5 text-xs text-[var(--svx-color-text-secondary)] outline-none"
      aria-label="Sort plugins"
    >
      {SORTS.map((s) => (
        <option key={s} value={s}>
          {t(`sort.${s}`)}
        </option>
      ))}
    </select>
  );
}

// ── Empty States ──

function EmptyNoPlugins() {
  const { t } = useTranslation("plugins");

  return (
    <div className="flex flex-col items-center justify-center py-16 text-center animate-page-in">
      <div className="mb-4 flex items-center justify-center">
        <PuzzleIcon className="size-12 text-[var(--svx-color-brand-primary)]/40 animate-puzzle-float" />
      </div>
      <h3 className="text-lg font-semibold text-[var(--svx-color-text-primary)]">
        {t("empty.title")}
      </h3>
      <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
        {t("empty.subtitle")}
      </p>
      <code className="mt-4 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)]">
        {t("empty.cliHint")}
      </code>
    </div>
  );
}

function EmptyNoMatch({ onClear }: { onClear: () => void }) {
  const { t } = useTranslation("plugins");

  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <p className="text-sm text-[var(--svx-color-text-secondary)]">
        {t("empty.noMatch")}
      </p>
      <button
        type="button"
        onClick={onClear}
        className="mt-2 text-xs font-medium text-[var(--svx-color-brand-primary)] hover:underline"
      >
        {t("empty.clearFilter")}
      </button>
    </div>
  );
}

// ── Error State ──

function ErrorState({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation(["plugins", "common"]);

  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <AlertTriangleIcon className="mb-3 size-10 text-[var(--svx-color-error)]" />
      <p className="text-sm text-[var(--svx-color-text-secondary)]">
        {t("common:errors.generic")}
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-brand-primary)] px-4 py-1.5 text-xs font-medium text-white hover:opacity-90"
      >
        {t("common:actions.retry")}
      </button>
    </div>
  );
}

// ── Main Page ──

export default function PluginsPage() {
  const { t } = useTranslation("plugins");
  const [selectedPlugin, setSelectedPlugin] = useState<string | null>(null);

  const plugins = useDashboardStore((s) => s.plugins);
  const pluginsLoading = useDashboardStore((s) => s.pluginsLoading);
  const pluginsError = useDashboardStore((s) => s.pluginsError);
  const pluginCounts = useDashboardStore((s) => s.pluginCounts);
  const pluginFilter = useDashboardStore((s) => s.pluginFilter);
  const pluginSearchQuery = useDashboardStore((s) => s.pluginSearchQuery);
  const pluginSort = useDashboardStore((s) => s.pluginSort);
  const fetchPlugins = useDashboardStore((s) => s.fetchPlugins);
  const setPluginFilter = useDashboardStore((s) => s.setPluginFilter);
  const setPluginSearchQuery = useDashboardStore((s) => s.setPluginSearchQuery);
  const setPluginSort = useDashboardStore((s) => s.setPluginSort);

  // Fetch on mount
  useEffect(() => {
    void fetchPlugins();
  }, [fetchPlugins]);

  // Filter + sort (memoized)
  const displayPlugins = useMemo(
    () =>
      sortPlugins(
        filterPlugins(plugins, pluginFilter, pluginSearchQuery),
        pluginSort,
      ),
    [plugins, pluginFilter, pluginSearchQuery, pluginSort],
  );

  const hasPlugins = plugins.length > 0;
  const hasResults = displayPlugins.length > 0;
  const isFiltering = pluginFilter !== "all" || pluginSearchQuery.trim() !== "";

  // Loading
  if (pluginsLoading && !hasPlugins) {
    return <PluginsSkeleton />;
  }

  return (
    <div className="space-y-6 animate-page-in">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-[var(--svx-color-text-primary)]">
          {t("title")}
        </h1>
        <p className="text-sm text-[var(--svx-color-text-secondary)]">
          {t("subtitle")}
        </p>
      </div>

      {/* Stats */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title={t("stats.total")}
          value={pluginCounts.total}
          icon={<PuzzleIcon className="size-4" />}
        />
        <StatCard
          title={t("stats.active")}
          value={pluginCounts.active}
          icon={<PowerIcon className="size-4" />}
          status={pluginCounts.active > 0 ? "green" : undefined}
        />
        <StatCard
          title={t("stats.disabled")}
          value={pluginCounts.disabled}
          status={pluginCounts.disabled > 0 ? "yellow" : undefined}
        />
        <StatCard
          title={t("stats.totalTools")}
          value={pluginCounts.totalTools}
          icon={<WrenchIcon className="size-4" />}
        />
      </div>

      {/* Error */}
      {pluginsError && !hasPlugins && (
        <ErrorState onRetry={() => void fetchPlugins()} />
      )}

      {/* Empty: No plugins installed */}
      {!pluginsError && !hasPlugins && <EmptyNoPlugins />}

      {/* Plugin grid (only if we have plugins) */}
      {hasPlugins && (
        <>
          {/* Toolbar: Search + Filters + Sort */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <input
              type="text"
              value={pluginSearchQuery}
              onChange={(e) => setPluginSearchQuery(e.target.value)}
              placeholder={t("search.placeholder")}
              className="h-9 w-full max-w-xs rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-3 text-sm text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)] outline-none focus:border-[var(--svx-color-brand-primary)]"
              aria-label={t("search.placeholder")}
            />
            <div className="flex items-center gap-2">
              <FilterTabs current={pluginFilter} onChange={setPluginFilter} />
              <SortSelect current={pluginSort} onChange={setPluginSort} />
            </div>
          </div>

          {/* Grid */}
          {hasResults ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {displayPlugins.map((plugin, i) => (
                <PluginCard
                  key={plugin.name}
                  plugin={plugin}
                  delay={i * 50}
                  onClick={() => setSelectedPlugin(plugin.name)}
                />
              ))}
            </div>
          ) : (
            <EmptyNoMatch
              onClear={() => {
                setPluginFilter("all");
                setPluginSearchQuery("");
              }}
            />
          )}
        </>
      )}

      {/* Detail Panel */}
      <PluginDetailPanel
        pluginName={selectedPlugin}
        open={selectedPlugin !== null}
        onClose={() => setSelectedPlugin(null)}
      />
    </div>
  );
}
