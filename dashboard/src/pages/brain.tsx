/**
 * Brain Explorer — knowledge graph visualization.
 *
 * POLISH-01: AbortController on fetch to prevent race conditions.
 * POLISH-02: Error state with retry (no silent catches).
 *
 * Ref: Architecture §3.4
 */

import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { InfoIcon, SparklesIcon, AlertTriangleIcon } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { api, isAbortError } from "@/lib/api";
import { BrainGraph } from "@/components/dashboard/brain-graph";
import { CategoryLegend, RelationLegend } from "@/components/dashboard/category-legend";
import { EmptyState } from "@/components/empty-state";
import { BrainEmptyAnimation } from "@/components/empty-state-animations";
import { Button } from "@/components/ui/button";
import type { BrainNode, BrainGraph as BrainGraphType } from "@/types/api";

export default function BrainPage() {
  const { t } = useTranslation(["brain", "common"]);
  const brainGraph = useDashboardStore((s) => s.brainGraph);
  const setBrainGraph = useDashboardStore((s) => s.setBrainGraph);
  const brainNodes = brainGraph?.nodes ?? [];
  const brainLinks = brainGraph?.links ?? [];

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<BrainNode | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 500 });

  // Fetch brain graph with AbortController
  const fetchGraph = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setLoading(true);
        setError(null);
        const data = await api.get<BrainGraphType>("/api/brain/graph?limit=200", { signal });
        setBrainGraph(data);
      } catch (err) {
        if (isAbortError(err)) return; // Navigation away — ignore
        setError(t("error.loadFailed"));
      } finally {
        setLoading(false);
      }
    },
    [setBrainGraph],
  );

  useEffect(() => {
    const controller = new AbortController();
    void fetchGraph(controller.signal);
    return () => controller.abort();
  }, [fetchGraph]);

  // Responsive dimensions
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

  const graphData = useMemo(
    () => ({ nodes: brainNodes, links: brainLinks }),
    [brainNodes, brainLinks],
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">
            {t("stats.concepts", { count: brainNodes.length })} · {t("stats.relations", { count: brainLinks.length })}
          </p>
        </div>
      </div>

      {/* Legends */}
      <div className="space-y-2">
        <CategoryLegend counts={categoryCounts} />
        {brainLinks.length > 0 && <RelationLegend />}
      </div>

      {/* Graph container */}
      <div className="overflow-hidden rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]">
        <div ref={containerRef} className="h-[calc(100vh-20rem)] min-h-[300px] sm:min-h-[400px]">
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
              <dt className="text-[10px] uppercase text-[var(--svx-color-text-secondary)]">{t("detail.category")}</dt>
              <dd className="font-medium capitalize">{selectedNode.category}</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-[var(--svx-color-text-secondary)]">{t("detail.importance")}</dt>
              <dd className="font-medium">{(selectedNode.importance * 100).toFixed(0)}%</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-[var(--svx-color-text-secondary)]">{t("detail.confidence")}</dt>
              <dd className="font-medium">{(selectedNode.confidence * 100).toFixed(0)}%</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-[var(--svx-color-text-secondary)]">{t("detail.accessCount")}</dt>
              <dd className="font-medium">{selectedNode.access_count}</dd>
            </div>
          </dl>
        </div>
      )}
    </div>
  );
}
