/**
 * Brain Explorer — knowledge graph visualization + semantic search.
 *
 * POLISH-01: AbortController on fetch to prevent race conditions.
 * POLISH-02: Error state with retry (no silent catches).
 * V05-P03: Semantic search via /api/brain/search + result highlighting in graph.
 *
 * Ref: SPE-009 §3.4, Architecture §3.4
 */

import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  InfoIcon,
  SparklesIcon,
  AlertTriangleIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { api, isAbortError } from "@/lib/api";
import { BrainGraph } from "@/components/dashboard/brain-graph";
import {
  CategoryLegend,
  RelationLegend,
} from "@/components/dashboard/category-legend";
import { EmptyState } from "@/components/empty-state";
import { BrainEmptyAnimation } from "@/components/empty-state-animations";
import { Button } from "@/components/ui/button";
import type {
  BrainNode,
  BrainGraph as BrainGraphType,
  BrainSearchResponse,
} from "@/types/api";

/** Debounce delay for search input (ms). */
const SEARCH_DEBOUNCE_MS = 300;

export default function BrainPage() {
  const { t } = useTranslation(["brain", "common"]);
  const brainGraph = useDashboardStore((s) => s.brainGraph);
  const setBrainGraph = useDashboardStore((s) => s.setBrainGraph);
  const brainSearchResults = useDashboardStore((s) => s.brainSearchResults);
  const setBrainSearchResults = useDashboardStore(
    (s) => s.setBrainSearchResults,
  );
  const brainSearchQuery = useDashboardStore((s) => s.brainSearchQuery);
  const setBrainSearchQuery = useDashboardStore((s) => s.setBrainSearchQuery);
  const brainNodes = brainGraph?.nodes ?? [];
  const brainLinks = brainGraph?.links ?? [];

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<BrainNode | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 500 });

  // Set of matched node IDs for highlighting in graph
  const highlightedNodeIds = useMemo(
    () => new Set(brainSearchResults.map((r) => r.id)),
    [brainSearchResults],
  );

  // ── Graph fetch ─────────────────────────────────────────────────────
  const fetchGraph = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setLoading(true);
        setError(null);
        const data = await api.get<BrainGraphType>(
          "/api/brain/graph?limit=200",
          { signal },
        );
        setBrainGraph(data);
      } catch (err) {
        if (isAbortError(err)) return;
        setError(t("error.loadFailed"));
      } finally {
        setLoading(false);
      }
    },
    [setBrainGraph, t],
  );

  useEffect(() => {
    const controller = new AbortController();
    void fetchGraph(controller.signal);
    return () => controller.abort();
  }, [fetchGraph]);

  // ── Search with debounce ────────────────────────────────────────────
  const executeSearch = useCallback(
    async (query: string) => {
      if (!query.trim()) {
        setBrainSearchResults([]);
        setSearchLoading(false);
        return;
      }

      try {
        setSearchLoading(true);
        const data = await api.get<BrainSearchResponse>(
          `/api/brain/search?q=${encodeURIComponent(query)}&limit=20`,
        );
        setBrainSearchResults(data.results);
      } catch {
        setBrainSearchResults([]);
      } finally {
        setSearchLoading(false);
      }
    },
    [setBrainSearchResults],
  );

  const handleSearchChange = useCallback(
    (value: string) => {
      setBrainSearchQuery(value);
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
      searchTimerRef.current = setTimeout(() => {
        void executeSearch(value);
      }, SEARCH_DEBOUNCE_MS);
    },
    [executeSearch, setBrainSearchQuery],
  );

  const clearSearch = useCallback(() => {
    setBrainSearchQuery("");
    setBrainSearchResults([]);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchInputRef.current?.focus();
  }, [setBrainSearchQuery, setBrainSearchResults]);

  useEffect(() => {
    return () => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, []);

  // ── Responsive dimensions ───────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setDimensions({
          width: entry.contentRect.width,
          height: Math.max(entry.contentRect.height, 400),
        });
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Category counts
  const categoryCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const node of brainNodes) {
      counts[node.category] = (counts[node.category] ?? 0) + 1;
    }
    return counts;
  }, [brainNodes]);

  const relationCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const link of brainLinks) {
      counts[link.relation_type] = (counts[link.relation_type] ?? 0) + 1;
    }
    return counts;
  }, [brainLinks]);

  const graphData = useMemo(
    () => ({ nodes: brainNodes, links: brainLinks }),
    [brainNodes, brainLinks],
  );

  return (
    <div className="space-y-4">
      {/* Header + Search */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">
            {t("stats.concepts", { count: brainNodes.length })} ·{" "}
            {t("stats.relations", { count: brainLinks.length })}
          </p>
        </div>

        {/* Search bar */}
        <div className="relative w-full sm:w-72">
          <SearchIcon className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-[var(--svx-color-text-secondary)]" />
          <input
            ref={searchInputRef}
            aria-label={t("search")}
            placeholder={t("search")}
            value={brainSearchQuery}
            onChange={(e) => handleSearchChange(e.target.value)}
            className="h-9 w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] pl-8 pr-8 text-sm text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-secondary)] focus:border-[var(--svx-color-brand-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--svx-color-brand-primary)]"
          />
          {brainSearchQuery && (
            <button
              onClick={clearSearch}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-[var(--svx-color-text-secondary)] hover:text-[var(--svx-color-text-primary)]"
              aria-label={t("common:clear", { defaultValue: "Clear" })}
              type="button"
            >
              <XIcon className="size-3.5" />
            </button>
          )}
          {searchLoading && (
            <div className="absolute right-8 top-1/2 -translate-y-1/2">
              <div className="size-3.5 animate-spin rounded-full border-2 border-[var(--svx-color-brand-primary)] border-t-transparent" />
            </div>
          )}
        </div>
      </div>

      {/* Search results chips */}
      {brainSearchResults.length > 0 && (
        <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-brand-primary)]/30 bg-[var(--svx-color-bg-surface)] p-3">
          <p className="mb-2 text-xs font-medium text-[var(--svx-color-text-secondary)]">
            {brainSearchResults.length} results
          </p>
          <div className="flex flex-wrap gap-1.5">
            {brainSearchResults.map((result) => (
              <button
                key={result.id}
                type="button"
                onClick={() => {
                  const node = brainNodes.find((n) => n.id === result.id);
                  if (node) setSelectedNode(node);
                }}
                className="inline-flex items-center gap-1.5 rounded-full bg-[var(--svx-color-bg-elevated)] px-2.5 py-1 text-xs font-medium text-[var(--svx-color-text-primary)] transition-colors hover:bg-[var(--svx-color-brand-primary)]/20"
              >
                <span className="capitalize">{result.name}</span>
                <span className="text-[10px] text-[var(--svx-color-text-secondary)]">
                  {Math.round(result.score * 100)}%
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Legends */}
      <div className="space-y-2">
        <CategoryLegend counts={categoryCounts} />
        {brainLinks.length > 0 && <RelationLegend counts={relationCounts} />}
      </div>

      {/* Graph container */}
      <div className="overflow-hidden rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]">
        <div
          ref={containerRef}
          className="h-[calc(100vh-20rem)] min-h-[300px] sm:min-h-[400px]"
        >
          {error ? (
            <EmptyState
              icon={<AlertTriangleIcon className="size-10" />}
              title={error}
              description={t("error.engineHint")}
              action={{
                label: t("error.retry"),
                onClick: () => void fetchGraph(),
              }}
              className="h-full"
            />
          ) : loading ? (
            <div className="flex h-full items-center justify-center">
              <div className="size-6 animate-spin rounded-full border-2 border-[var(--svx-color-brand-primary)] border-t-transparent" />
            </div>
          ) : brainNodes.length === 0 ? (
            <EmptyState
              icon={<SparklesIcon className="size-10" />}
              animation={<BrainEmptyAnimation />}
              title={t("empty")}
              description={t("emptyDescription")}
              className="h-full"
            />
          ) : (
            <BrainGraph
              data={graphData}
              width={dimensions.width}
              height={dimensions.height}
              onNodeClick={(node) => setSelectedNode(node)}
              highlightedNodeIds={highlightedNodeIds}
            />
          )}
        </div>
      </div>

      {/* Node Detail Panel */}
      {selectedNode && (
        <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
          <div className="flex items-center justify-between pb-3">
            <div className="flex items-center gap-2 text-sm font-medium text-[var(--svx-color-text-primary)]">
              <InfoIcon className="size-4" />
              {selectedNode.name}
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="size-6"
              onClick={() => setSelectedNode(null)}
              aria-label={t("detail.close")}
            >
              ✕
            </Button>
          </div>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-[10px] uppercase text-[var(--svx-color-text-secondary)]">
                {t("detail.category")}
              </dt>
              <dd className="font-medium capitalize">
                {selectedNode.category}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-[var(--svx-color-text-secondary)]">
                {t("detail.importance")}
              </dt>
              <dd className="font-medium">
                {(selectedNode.importance * 100).toFixed(0)}%
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-[var(--svx-color-text-secondary)]">
                {t("detail.confidence")}
              </dt>
              <dd className="font-medium">
                {(selectedNode.confidence * 100).toFixed(0)}%
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-[var(--svx-color-text-secondary)]">
                {t("detail.accessCount")}
              </dt>
              <dd className="font-medium">{selectedNode.access_count}</dd>
            </div>
          </dl>
        </div>
      )}
    </div>
  );
}
