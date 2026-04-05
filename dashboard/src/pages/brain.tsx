import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { BrainIcon, InfoIcon } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useDashboardStore } from "@/stores/dashboard";
import { api } from "@/lib/api";
import { BrainGraph } from "@/components/dashboard/brain-graph";
import { CategoryLegend } from "@/components/dashboard/category-legend";
import type { BrainNode, BrainGraph as BrainGraphType } from "@/types/api";

export default function BrainPage() {
  const { t } = useTranslation(["brain", "common"]);
  const brainGraph = useDashboardStore((s) => s.brainGraph);
  const setBrainGraph = useDashboardStore((s) => s.setBrainGraph);
  const brainNodes = brainGraph?.nodes ?? [];
  const brainLinks = brainGraph?.links ?? [];

  const [loading, setLoading] = useState(true);
  const [selectedNode, setSelectedNode] = useState<BrainNode | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 500 });

  // Fetch brain graph
  const fetchGraph = useCallback(async () => {
    try {
      setLoading(true);
      const data = await api.get<BrainGraphType>("/api/brain/graph?limit=200");
      setBrainGraph(data);
    } catch {
      // 401 handled
    } finally {
      setLoading(false);
    }
  }, [setBrainGraph]);

  useEffect(() => {
    void fetchGraph();
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
          <p className="text-sm text-muted-foreground">
            {brainNodes.length} {t("stats.concepts")} · {brainLinks.length} {t("stats.connections")}
          </p>
        </div>
      </div>

      {/* Legend */}
      <CategoryLegend counts={categoryCounts} />

      {/* Graph */}
      <Card className="overflow-hidden">
        <CardContent className="p-0">
          <div ref={containerRef} className="h-[calc(100vh-20rem)] min-h-[400px]">
            {loading ? (
              <div className="flex h-full items-center justify-center">
                <div className="size-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
              </div>
            ) : brainNodes.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
                <BrainIcon className="size-10 opacity-30" />
                <p className="text-sm">{t("empty")}</p>
              </div>
            ) : (
              <BrainGraph
                data={graphData}
                width={dimensions.width}
                height={dimensions.height}
                onNodeClick={(node) => setSelectedNode(node)}
              />
            )}
          </div>
        </CardContent>
      </Card>

      {/* Node Detail Panel */}
      {selectedNode && (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="flex items-center gap-2 text-sm">
                <InfoIcon className="size-4" />
                {selectedNode.name}
              </CardTitle>
              <button
                type="button"
                onClick={() => setSelectedNode(null)}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                ✕
              </button>
            </div>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
              <div>
                <dt className="text-[10px] uppercase text-muted-foreground">{t("details.category")}</dt>
                <dd className="font-medium capitalize">{selectedNode.category}</dd>
              </div>
              <div>
                <dt className="text-[10px] uppercase text-muted-foreground">{t("details.importance")}</dt>
                <dd className="font-medium">{(selectedNode.importance * 100).toFixed(0)}%</dd>
              </div>
              <div>
                <dt className="text-[10px] uppercase text-muted-foreground">{t("details.confidence")}</dt>
                <dd className="font-medium">{(selectedNode.confidence * 100).toFixed(0)}%</dd>
              </div>
              <div>
                <dt className="text-[10px] uppercase text-muted-foreground">{t("details.accessCount")}</dt>
                <dd className="font-medium">{selectedNode.access_count}</dd>
              </div>
            </dl>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
