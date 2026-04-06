/**
 * About page — version, system info, links.
 *
 * POLISH-06: Replaced shadcn Card wrappers with --svx-* token divs.
 * Consistent with all other pages.
 *
 * Ref: Architecture §3.6
 */

import { ExternalLinkIcon, ShieldIcon, CodeIcon, HeartIcon } from "lucide-react";
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
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="flex items-center gap-2 text-sm font-medium text-[var(--svx-color-text-primary)]">
          <ShieldIcon className="size-4" />
          Version &amp; License
        </h2>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">Version</dt>
            <dd className="font-code font-medium">v{status?.version ?? "0.1.0"}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">License</dt>
            <dd>
              <span className="inline-flex rounded-[var(--svx-radius-full)] bg-[var(--svx-color-bg-elevated)] px-2 py-0.5 font-code text-[10px]">
                AGPL-3.0
              </span>
            </dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">Dashboard</dt>
            <dd className="font-code text-xs">React {__REACT_VERSION__}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">Engine Uptime</dt>
            <dd className="font-code text-xs">
              {status ? formatUptime(status.uptime_seconds) : "—"}
            </dd>
          </div>
        </dl>
      </section>

      {/* System Info */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">System</h2>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">Mind</dt>
            <dd className="font-medium">{status?.mind_name ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">Concepts</dt>
            <dd className="font-code text-xs">{status?.memory_concepts?.toLocaleString() ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">Episodes</dt>
            <dd className="font-code text-xs">{status?.memory_episodes?.toLocaleString() ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">LLM Calls Today</dt>
            <dd className="font-code text-xs">{status?.llm_calls_today?.toLocaleString() ?? "—"}</dd>
          </div>
        </dl>
      </section>

      {/* Links */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">Links</h2>
        <div className="mt-3 flex flex-wrap gap-3">
          {LINKS.map((link) => (
            <a
              key={link.label}
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)] px-3 py-1.5 text-xs font-medium text-[var(--svx-color-text-secondary)] transition-colors hover:bg-[var(--svx-color-bg-hover)] hover:text-[var(--svx-color-text-primary)]"
            >
              <link.icon className="size-3.5" />
              {link.label}
            </a>
          ))}
        </div>
      </section>

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

declare const __REACT_VERSION__: string;

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  const mins = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}
