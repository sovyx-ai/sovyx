/**
 * Plugin detail panel — slide-over Sheet with full plugin info.
 *
 * Sections: header, tools, permissions, health, events, dependencies, manifest.
 * Actions: enable/disable toggle, reload button.
 *
 * TASK-457
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  PowerIcon,
  RefreshCwIcon,
  ChevronDownIcon,
  ExternalLinkIcon,
  ShieldIcon,
  WrenchIcon,
  HeartPulseIcon,
  FileTextIcon,
  LinkIcon,
  RadioIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useDashboardStore } from "@/stores/dashboard";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { PermissionBadge, PluginToolBadge, PricingBadge } from "./plugin-badges";
import type { PluginDetail as PluginDetailType, PluginToolDetail } from "@/types/api";

// ── Collapsible Section ──

function Section({
  title,
  icon: Icon,
  children,
  defaultOpen = true,
  count,
}: {
  title: string;
  icon: React.ElementType;
  children: React.ReactNode;
  defaultOpen?: boolean;
  count?: number;
}) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="border-t border-[var(--svx-color-border-default)] pt-3">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 text-left text-xs font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)] hover:text-[var(--svx-color-text-primary)] transition-colors"
      >
        <Icon className="size-3.5" />
        <span>{title}</span>
        {count !== undefined && (
          <span className="ml-1 text-[10px] font-normal text-[var(--svx-color-text-tertiary)]">
            ({count})
          </span>
        )}
        <ChevronDownIcon
          className={cn(
            "ml-auto size-3.5 transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      {open && <div className="mt-2">{children}</div>}
    </div>
  );
}

// ── Tool Item (expandable schema) ──

function ToolItem({ tool }: { tool: PluginToolDetail }) {
  const [expanded, setExpanded] = useState(false);
  const hasParams =
    tool.parameters && Object.keys(tool.parameters).length > 0;

  return (
    <div className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] p-2">
      <button
        type="button"
        onClick={() => hasParams && setExpanded(!expanded)}
        className={cn(
          "flex w-full items-center gap-2 text-left",
          hasParams && "cursor-pointer",
        )}
      >
        <span className="text-xs font-medium text-[var(--svx-color-text-primary)]">
          {tool.name}
        </span>
        {tool.requires_confirmation && (
          <span
            className="text-[9px] text-[var(--svx-color-warning)]"
            title="Requires confirmation"
          >
            ⚠️
          </span>
        )}
        {hasParams && (
          <ChevronDownIcon
            className={cn(
              "ml-auto size-3 text-[var(--svx-color-text-tertiary)] transition-transform",
              expanded && "rotate-180",
            )}
          />
        )}
      </button>
      <p className="mt-0.5 text-[10px] text-[var(--svx-color-text-tertiary)]">
        {tool.description}
      </p>
      {expanded && hasParams && (
        <pre className="mt-2 max-h-32 overflow-auto rounded bg-[var(--svx-color-bg-elevated)] p-2 text-[10px] text-[var(--svx-color-text-secondary)]">
          {JSON.stringify(tool.parameters, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ── Health Bar ──

function HealthBar({
  value,
  max,
  label,
}: {
  value: number;
  max: number;
  label: string;
}) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  const color =
    pct < 40
      ? "bg-[var(--svx-color-success)]"
      : pct < 80
        ? "bg-[var(--svx-color-warning)]"
        : "bg-[var(--svx-color-error)]";

  return (
    <div className="space-y-1">
      <div className="flex justify-between text-[10px] text-[var(--svx-color-text-secondary)]">
        <span>{label}</span>
        <span>
          {value}/{max}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--svx-color-bg-elevated)]">
        <div
          className={cn("h-full rounded-full transition-all", color)}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Main Component ──

interface PluginDetailPanelProps {
  pluginName: string | null;
  open: boolean;
  onClose: () => void;
}

export function PluginDetailPanel({
  pluginName,
  open,
  onClose,
}: PluginDetailPanelProps) {
  const { t } = useTranslation("plugins");

  const detail = useDashboardStore((s) => s.pluginDetail);
  const loading = useDashboardStore((s) => s.pluginDetailLoading);
  const fetchPluginDetail = useDashboardStore((s) => s.fetchPluginDetail);
  const enablePlugin = useDashboardStore((s) => s.enablePlugin);
  const disablePlugin = useDashboardStore((s) => s.disablePlugin);
  const reloadPlugin = useDashboardStore((s) => s.reloadPlugin);

  const [actionLoading, setActionLoading] = useState<string | null>(null);

  useEffect(() => {
    if (pluginName && open) {
      void fetchPluginDetail(pluginName);
    }
  }, [pluginName, open, fetchPluginDetail]);

  const handleAction = async (
    action: "enable" | "disable" | "reload",
  ) => {
    if (!pluginName) return;

    // Confirmation for destructive actions
    if (action === "disable") {
      if (!window.confirm(t("actions.disableConfirm"))) return;
    }
    if (action === "reload") {
      if (!window.confirm(t("actions.reloadConfirm"))) return;
    }

    setActionLoading(action);
    try {
      let success = false;
      if (action === "enable") success = await enablePlugin(pluginName);
      else if (action === "disable") success = await disablePlugin(pluginName);
      else success = await reloadPlugin(pluginName);

      if (success) {
        toast.success(t(`actions.${action === "enable" ? "enabled" : action === "disable" ? "disabled" : "reloaded"}`));
      } else {
        toast.error(t(`actions.${action}Failed`));
      }
      // Refresh detail after action
      void fetchPluginDetail(pluginName);
    } catch {
      toast.error(t(`actions.${action}Failed`));
    } finally {
      setActionLoading(null);
    }
  };

  const manifest = detail?.manifest && "name" in detail.manifest ? detail.manifest : null;

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        side="right"
        className="w-full overflow-y-auto sm:max-w-md"
      >
        {loading && !detail ? (
          <div className="flex h-full items-center justify-center">
            <RefreshCwIcon className="size-5 animate-spin text-[var(--svx-color-text-tertiary)]" />
          </div>
        ) : detail ? (
          <div className="flex flex-col gap-4 pb-8">
            {/* ── Header ── */}
            <SheetHeader className="space-y-3">
              <div className="flex items-start gap-3">
                <div
                  className="flex size-12 items-center justify-center rounded-[var(--svx-radius-lg)] text-lg font-bold text-white"
                  style={{
                    background: `linear-gradient(135deg, hsl(${nameToHue(detail.name)}, 70%, 50%), hsl(${(nameToHue(detail.name) + 40) % 360}, 70%, 40%))`,
                  }}
                >
                  {detail.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <SheetTitle className="text-lg">
                    {detail.name}
                  </SheetTitle>
                  <SheetDescription className="mt-0.5">
                    v{detail.version}
                    {manifest?.author && ` · ${manifest.author}`}
                  </SheetDescription>
                </div>
              </div>

              {/* Status + Actions */}
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
                    detail.status === "active" &&
                      "bg-[var(--svx-color-success)]/10 text-[var(--svx-color-success)]",
                    detail.status === "disabled" &&
                      "bg-[var(--svx-color-text-disabled)]/10 text-[var(--svx-color-text-disabled)]",
                    detail.status === "error" &&
                      "bg-[var(--svx-color-error)]/10 text-[var(--svx-color-error)]",
                  )}
                >
                  <span
                    className={cn(
                      "size-1.5 rounded-full",
                      detail.status === "active" && "bg-[var(--svx-color-success)]",
                      detail.status === "disabled" && "bg-[var(--svx-color-text-disabled)]",
                      detail.status === "error" && "bg-[var(--svx-color-error)]",
                    )}
                  />
                  {t(`status.${detail.status}`)}
                </span>

                {manifest?.pricing && (
                  <PricingBadge pricing={manifest.pricing} />
                )}

                <div className="ml-auto flex gap-1">
                  {detail.status === "disabled" ? (
                    <button
                      type="button"
                      onClick={() => void handleAction("enable")}
                      disabled={actionLoading !== null}
                      className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-success)] px-3 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                    >
                      {actionLoading === "enable" ? (
                        <RefreshCwIcon className="size-3 animate-spin" />
                      ) : (
                        <PowerIcon className="size-3" />
                      )}
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void handleAction("disable")}
                      disabled={actionLoading !== null}
                      className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] px-3 py-1 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-bg-elevated)] disabled:opacity-50"
                    >
                      {actionLoading === "disable" ? (
                        <RefreshCwIcon className="size-3 animate-spin" />
                      ) : (
                        <PowerIcon className="size-3" />
                      )}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => void handleAction("reload")}
                    disabled={actionLoading !== null}
                    title={t("actions.reload")}
                    className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] px-2 py-1 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-bg-elevated)] disabled:opacity-50"
                  >
                    <RefreshCwIcon
                      className={cn(
                        "size-3",
                        actionLoading === "reload" && "animate-spin",
                      )}
                    />
                  </button>
                </div>
              </div>

              {/* Homepage link */}
              {manifest?.homepage && (
                <a
                  href={manifest.homepage}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs text-[var(--svx-color-brand-primary)] hover:underline"
                >
                  <ExternalLinkIcon className="size-3" />
                  {t("detail.homepage")}
                </a>
              )}

              {/* Description */}
              <p className="text-sm text-[var(--svx-color-text-secondary)]">
                {detail.description}
              </p>
            </SheetHeader>

            {/* ── Tools ── */}
            <Section
              title={t("detail.tools")}
              icon={WrenchIcon}
              count={detail.tools.length}
            >
              {detail.tools.length > 0 ? (
                <div className="space-y-1.5">
                  {detail.tools.map((tool) => (
                    <ToolItem key={tool.name} tool={tool} />
                  ))}
                </div>
              ) : (
                <p className="text-xs text-[var(--svx-color-text-tertiary)]">
                  {t("detail.noTools")}
                </p>
              )}
            </Section>

            {/* ── Permissions ── */}
            <Section
              title={t("detail.permissions")}
              icon={ShieldIcon}
              count={detail.permissions.length}
            >
              {detail.permissions.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                  {detail.permissions.map((p) => (
                    <PermissionBadge
                      key={p.permission}
                      permission={p.permission}
                      risk={p.risk}
                      description={p.description}
                    />
                  ))}
                </div>
              ) : (
                <p className="text-xs text-[var(--svx-color-text-tertiary)]">
                  {t("detail.noPermissions")}
                </p>
              )}
            </Section>

            {/* ── Health ── */}
            <Section title={t("detail.health")} icon={HeartPulseIcon}>
              <div className="space-y-3">
                <HealthBar
                  value={detail.health.consecutive_failures}
                  max={5}
                  label={t("health.failures")}
                />
                <div className="flex justify-between text-[10px]">
                  <span className="text-[var(--svx-color-text-secondary)]">
                    {t("health.activeTasks")}
                  </span>
                  <span className="text-[var(--svx-color-text-primary)]">
                    {detail.health.active_tasks}
                  </span>
                </div>
                {detail.health.last_error && (
                  <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/5 p-2">
                    <p className="text-[10px] font-medium text-[var(--svx-color-error)]">
                      {t("health.lastError")}
                    </p>
                    <p className="mt-0.5 text-[10px] text-[var(--svx-color-text-secondary)]">
                      {detail.health.last_error}
                    </p>
                  </div>
                )}
              </div>
            </Section>

            {/* ── Events (from manifest) ── */}
            {manifest && (
              <Section
                title={t("detail.events")}
                icon={RadioIcon}
                defaultOpen={false}
                count={
                  (manifest.events?.emits?.length ?? 0) +
                  (manifest.events?.subscribes?.length ?? 0)
                }
              >
                {(manifest.events?.emits?.length ?? 0) > 0 ||
                (manifest.events?.subscribes?.length ?? 0) > 0 ? (
                  <div className="space-y-2">
                    {manifest.events.emits.length > 0 && (
                      <div>
                        <p className="text-[10px] font-medium text-[var(--svx-color-text-secondary)]">
                          {t("detail.emits")}
                        </p>
                        <div className="mt-1 flex flex-wrap gap-1">
                          {manifest.events.emits.map((e) => (
                            <PluginToolBadge
                              key={e.name}
                              name={e.name}
                              description={e.description}
                            />
                          ))}
                        </div>
                      </div>
                    )}
                    {manifest.events.subscribes.length > 0 && (
                      <div>
                        <p className="text-[10px] font-medium text-[var(--svx-color-text-secondary)]">
                          {t("detail.subscribes")}
                        </p>
                        <div className="mt-1 flex flex-wrap gap-1">
                          {manifest.events.subscribes.map((s) => (
                            <PluginToolBadge key={s} name={s} />
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="text-xs text-[var(--svx-color-text-tertiary)]">
                    {t("detail.noEvents")}
                  </p>
                )}
              </Section>
            )}

            {/* ── Dependencies (from manifest) ── */}
            {manifest && (
              <Section
                title={t("detail.dependencies")}
                icon={LinkIcon}
                defaultOpen={false}
                count={
                  (manifest.depends?.length ?? 0) +
                  (manifest.optional_depends?.length ?? 0)
                }
              >
                {(manifest.depends?.length ?? 0) > 0 ||
                (manifest.optional_depends?.length ?? 0) > 0 ? (
                  <div className="space-y-1">
                    {manifest.depends.map((d) => (
                      <div
                        key={d.name}
                        className="flex items-center gap-2 text-xs text-[var(--svx-color-text-secondary)]"
                      >
                        <span className="font-medium">{d.name}</span>
                        <span className="text-[10px] text-[var(--svx-color-text-tertiary)]">
                          {d.version}
                        </span>
                      </div>
                    ))}
                    {manifest.optional_depends.map((d) => (
                      <div
                        key={d.name}
                        className="flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]"
                      >
                        <span className="font-medium italic">{d.name}</span>
                        <span className="text-[10px]">{d.version}</span>
                        <span className="text-[9px]">(optional)</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-[var(--svx-color-text-tertiary)]">
                    {t("detail.noDependencies")}
                  </p>
                )}
              </Section>
            )}

            {/* ── Manifest (raw) ── */}
            {manifest && (
              <Section
                title={t("detail.manifest")}
                icon={FileTextIcon}
                defaultOpen={false}
              >
                <pre className="max-h-48 overflow-auto rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)] p-3 text-[10px] leading-relaxed text-[var(--svx-color-text-secondary)]">
                  {JSON.stringify(manifest, null, 2)}
                </pre>
              </Section>
            )}
          </div>
        ) : (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-[var(--svx-color-text-tertiary)]">
              Plugin not found
            </p>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}

// ── Helper (duplicated from plugin-card to avoid cross-import) ──

function nameToHue(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash) % 360;
}
