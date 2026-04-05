import { ExternalLinkIcon, ShieldIcon, CodeIcon, HeartIcon } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useDashboardStore } from "@/stores/dashboard";

const LINKS = [
  { label: "GitHub", url: "https://github.com/sovyx-ai/sovyx", icon: CodeIcon },
  { label: "Documentation", url: "https://docs.sovyx.ai", icon: ExternalLinkIcon },
] as const;

export default function AboutPage() {
  const status = useDashboardStore((s) => s.status);

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div className="text-center">
        <h1 className="text-3xl font-bold">🔮 Sovyx</h1>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          Sovereign AI Companion Engine
        </p>
      </div>

      {/* Version + License */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <ShieldIcon className="size-4" />
            Version & License
          </CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
            <div>
              <dt className="text-[10px] uppercase text-muted-foreground">Version</dt>
              <dd className="font-mono font-medium">0.5.0-dev</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-muted-foreground">License</dt>
              <dd className="flex items-center gap-2">
                <Badge variant="secondary" className="font-mono text-[10px]">
                  AGPL-3.0
                </Badge>
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-muted-foreground">Dashboard</dt>
              <dd className="font-mono text-xs">React {__REACT_VERSION__}</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-muted-foreground">Engine Uptime</dt>
              <dd className="font-mono text-xs">
                {status ? formatUptime(status.uptime_seconds) : "—"}
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* System Info */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">System</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
            <div>
              <dt className="text-[10px] uppercase text-muted-foreground">Mind</dt>
              <dd className="font-medium">{status?.mind_name ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-muted-foreground">Concepts</dt>
              <dd className="font-mono text-xs">{status?.memory_concepts?.toLocaleString() ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-muted-foreground">Episodes</dt>
              <dd className="font-mono text-xs">{status?.memory_episodes?.toLocaleString() ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase text-muted-foreground">LLM Calls Today</dt>
              <dd className="font-mono text-xs">{status?.llm_calls_today?.toLocaleString() ?? "—"}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* Links */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Links</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-3">
          {LINKS.map((link) => (
            <a
              key={link.label}
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md bg-secondary/50 px-3 py-1.5 text-xs font-medium text-foreground/80 transition-colors hover:bg-secondary hover:text-foreground"
            >
              <link.icon className="size-3.5" />
              {link.label}
            </a>
          ))}
        </CardContent>
      </Card>

      {/* Footer */}
      <div className="text-center">
        <p className="flex items-center justify-center gap-1 text-xs text-[var(--svx-color-text-secondary)]">
          Built with <HeartIcon className="size-3 text-[var(--svx-color-error)]" /> by Sovyx AI
        </p>
        <p className="mt-1 text-[10px] text-[var(--svx-color-text-disabled)]">
          Your mind, your data, your sovereignty.
        </p>
      </div>
    </div>
  );
}

// Injected at build time via Vite define
declare const __REACT_VERSION__: string;

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  const mins = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}
