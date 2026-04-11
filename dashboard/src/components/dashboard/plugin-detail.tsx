/**
 * Plugin detail panel — slide-over Sheet with full plugin info.
 *
 * Redesigned with proper spacing, visual hierarchy, and professional UX.
 * Sections: header, description, tools, permissions, health, events, deps, manifest.
 *
 * TASK-457 (redesign)
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
  CheckCircle2Icon,
  AlertTriangleIcon,
  XCircleIcon,
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
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogClose,
} from "@/components/ui/dialog";
import { PermissionBadge, PluginToolBadge, PricingBadge } from "./plugin-badges";
import { PermissionDialog } from "./permission-dialog";
import type { PluginDetail as PluginDetailType, PluginToolDetail } from "@/types/api";

// ── Status Config ──

const STATUS_CONFIG = {
  active: {
    icon: CheckCircle2Icon,
    color: "text-[var(--svx-color-success)]",
    bg: "bg-[var(--svx-color-success)]/10",
    dot: "bg-[var(--svx-color-success)]",
  },
  disabled: {
    icon: PowerIcon,
    color: "text-[var(--svx-color-text-disabled)]",
    bg: "bg-[var(--svx-color-text-disabled)]/10",
    dot: "bg-[var(--svx-color-text-disabled)]",
  },
  error: {
    icon: XCircleIcon,
    color: "text-[var(--svx-color-error)]",
    bg: "bg-[var(--svx-color-error)]/10",
    dot: "bg-[var(--svx-color-error)]",
  },
} as const;

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
    <div className="rounded-[var(--svx-radius-lg)] bg-[var(--svx-color-bg-elevated)]/30">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2.5 px-4 py-3.5 text-left transition-colors hover:bg-[var(--svx-color-bg-elevated)]/60 rounded-[var(--svx-radius-lg)]"
      >
        <div className="flex size-7 items-center justify-center rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)]">
          <Icon className="size-3.5 text-[var(--svx-color-text-secondary)]" />
        </div>
        <span className="text-xs font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]">
          {title}
        </span>
        {count !== undefined && (
          <span className="rounded-full bg-[var(--svx-color-bg-elevated)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--svx-color-text-tertiary)]">
            {count}
          </span>
        )}
        <ChevronDownIcon
          className={cn(
            "ml-auto size-4 text-[var(--svx-color-text-tertiary)] transition-transform duration-200",
            open && "rotate-180",
          )}
        />
      </button>
      <div
        className={cn(
          "grid transition-all duration-200 ease-in-out",
          open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
        )}
      >
        <div className="overflow-hidden">
          <div className="px-4 pb-4 pt-0.5">{children}</div>
        </div>
      </div>
    </div>
  );
}

// ── Tool Item ──

function ToolItem({ tool }: { tool: PluginToolDetail }) {
  const { t } = useTranslation("plugins");
  const [expanded, setExpanded] = useState(false);
  const hasParams =
    tool.parameters && Object.keys(tool.parameters).length > 0;

  return (
    <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)]/60 transition-colors hover:bg-[var(--svx-color-bg-elevated)]">
      <button
        type="button"
        onClick={() => hasParams && setExpanded(!expanded)}
        className={cn(
          "flex w-full items-center gap-3 p-3 text-left",
          hasParams && "cursor-pointer",
        )}
      >
        <div className="flex size-8 shrink-0 items-center justify-center rounded-[var(--svx-radius-md)] bg-[var(--svx-color-brand-primary)]/10">
          <WrenchIcon className="size-3.5 text-[var(--svx-color-brand-primary)]" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-[var(--svx-color-text-primary)]">
              {tool.name}
            </span>
            {tool.requires_confirmation && (
              <span
                className="text-xs text-[var(--svx-color-warning)]"
                title={t("detail.requiresConfirmation")}
              >
                ⚠️
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs leading-relaxed text-[var(--svx-color-text-tertiary)]">
            {tool.description}
          </p>
        </div>
        {hasParams && (
          <ChevronDownIcon
            className={cn(
              "size-4 shrink-0 text-[var(--svx-color-text-tertiary)] transition-transform duration-200",
              expanded && "rotate-180",
            )}
          />
        )}
      </button>
      {expanded && hasParams && (
        <div className="border-t border-[var(--svx-color-border-default)] px-3 pb-3 pt-2">
          <pre className="max-h-40 overflow-auto rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-base)] p-3 text-xs leading-relaxed text-[var(--svx-color-text-secondary)] font-mono">
            {JSON.stringify(tool.parameters, null, 2)}
          </pre>
        </div>
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
    <div className="space-y-2">
      <div className="flex justify-between text-xs">
        <span className="font-medium text-[var(--svx-color-text-secondary)]">
          {label}
        </span>
        <span className="tabular-nums text-[var(--svx-color-text-primary)]">
          {value}/{max}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--svx-color-bg-elevated)]">
        <div
          className={cn("h-full rounded-full transition-all duration-500", color)}
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
  const [permDialogOpen, setPermDialogOpen] = useState(false);
  const [confirmAction, setConfirmAction] = useState<"disable" | "reload" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    if (pluginName && open) {
      void fetchPluginDetail(pluginName);
      setActionError(null);
    }
  }, [pluginName, open, fetchPluginDetail]);

  const executeAction = async (action: "enable" | "disable" | "reload") => {
    if (!pluginName) return;
    setActionLoading(action);
    setActionError(null);
    try {
      let success = false;
      if (action === "enable") success = await enablePlugin(pluginName);
      else if (action === "disable") success = await disablePlugin(pluginName);
      else success = await reloadPlugin(pluginName);

      if (success) {
        const key = action === "enable" ? "enabled" : action === "disable" ? "disabled" : "reloaded";
        toast.success(t(`actions.${key}`));
      } else {
        const msg = t(`actions.${action}Failed`);
        toast.error(msg);
        setActionError(msg);
      }
      void fetchPluginDetail(pluginName);
    } catch {
      const msg = t(`actions.${action}Failed`);
      toast.error(msg);
      setActionError(msg);
    } finally {
      setActionLoading(null);
    }
  };

  const handleAction = (action: "enable" | "disable" | "reload") => {
    if (action === "disable" || action === "reload") {
      setConfirmAction(action);
    } else {
      void executeAction(action);
    }
  };

  const onConfirm = () => {
    if (confirmAction) {
      void executeAction(confirmAction);
      setConfirmAction(null);
    }
  };

  const manifest = detail?.manifest && "name" in detail.manifest ? detail.manifest : null;
  const statusCfg = detail ? STATUS_CONFIG[detail.status as keyof typeof STATUS_CONFIG] ?? STATUS_CONFIG.disabled : STATUS_CONFIG.disabled;

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        side="right"
        className="w-full overflow-y-auto sm:max-w-md bg-[var(--svx-color-bg-base)]"
      >
        {loading && !detail ? (
          <div className="flex h-full items-center justify-center">
            <RefreshCwIcon className="size-6 animate-spin text-[var(--svx-color-text-tertiary)]" />
          </div>
        ) : detail ? (
          <div className="flex flex-col gap-6 pb-10">
            {/* ── Header ── */}
            <SheetHeader className="space-y-5">
              {/* Identity */}
              <div className="flex items-start gap-4">
                <div
                  className="flex size-14 shrink-0 items-center justify-center rounded-2xl text-xl font-bold text-white shadow-lg"
                  style={{
                    background: `linear-gradient(135deg, hsl(${nameToHue(detail.name)}, 70%, 50%), hsl(${(nameToHue(detail.name) + 40) % 360}, 70%, 40%))`,
                  }}
                >
                  {detail.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1 pt-0.5">
                  <SheetTitle className="text-xl font-bold">
                    {detail.name}
                  </SheetTitle>
                  <SheetDescription className="mt-1 text-sm">
                    v{detail.version}
                    {manifest?.author && (
                      <span className="text-[var(--svx-color-text-tertiary)]">
                        {" "}· {manifest.author}
                      </span>
                    )}
                  </SheetDescription>
                </div>
              </div>

              {/* Status + Actions Row */}
              <div className="flex items-center gap-3 rounded-[var(--svx-radius-lg)] bg-[var(--svx-color-bg-elevated)]/40 px-4 py-3">
                {/* Status */}
                <div className={cn("flex items-center gap-2", statusCfg.color)}>
                  <span className={cn("size-2 rounded-full", statusCfg.dot)} />
                  <span className="text-sm font-medium">
                    {t(`status.${detail.status}`)}
                  </span>
                </div>

                {manifest?.pricing && (
                  <PricingBadge pricing={manifest.pricing} />
                )}

                {/* Actions — pushed right */}
                <div className="ml-auto flex items-center gap-2">
                  {detail.status === "disabled" ? (
                    <button
                      type="button"
                      onClick={() => handleAction("enable")}
                      disabled={actionLoading !== null}
                      className="flex items-center gap-1.5 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-success)] px-3.5 py-2 text-xs font-medium text-white shadow-sm transition-all hover:opacity-90 hover:shadow-md disabled:opacity-50"
                    >
                      {actionLoading === "enable" ? (
                        <RefreshCwIcon className="size-3.5 animate-spin" />
                      ) : (
                        <PowerIcon className="size-3.5" />
                      )}
                      <span>{t("actions.enable")}</span>
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => handleAction("disable")}
                      disabled={actionLoading !== null}
                      className="flex items-center gap-1.5 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)] px-3.5 py-2 text-xs font-medium text-[var(--svx-color-text-secondary)] transition-all hover:text-[var(--svx-color-warning)] disabled:opacity-50"
                    >
                      {actionLoading === "disable" ? (
                        <RefreshCwIcon className="size-3.5 animate-spin" />
                      ) : (
                        <PowerIcon className="size-3.5" />
                      )}
                      <span>{t("actions.disable")}</span>
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => handleAction("reload")}
                    disabled={actionLoading !== null}
                    title={t("actions.reload")}
                    className="flex items-center justify-center rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)] p-2 text-[var(--svx-color-text-secondary)] transition-all hover:bg-[var(--svx-color-bg-surface)] disabled:opacity-50"
                  >
                    <RefreshCwIcon
                      className={cn(
                        "size-3.5",
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
                  className="inline-flex items-center gap-1.5 text-sm text-[var(--svx-color-brand-primary)] hover:underline"
                >
                  <ExternalLinkIcon className="size-3.5" />
                  {t("detail.homepage")}
                </a>
              )}

              {/* Action error */}
              {actionError && (
                <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-4 py-3 text-sm text-[var(--svx-color-error)]">
                  <AlertTriangleIcon className="size-4 shrink-0" />
                  {actionError}
                </div>
              )}

              {/* Description */}
              <p className="text-sm leading-relaxed text-[var(--svx-color-text-secondary)]">
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
                <div className="space-y-2">
                  {detail.tools.map((tool) => (
                    <ToolItem key={tool.name} tool={tool} />
                  ))}
                </div>
              ) : (
                <p className="py-2 text-sm text-[var(--svx-color-text-tertiary)]">
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
                <div className="space-y-3">
                  <div className="flex flex-wrap gap-2">
                    {detail.permissions.map((p) => (
                      <PermissionBadge
                        key={p.permission}
                        permission={p.permission}
                        risk={p.risk}
                        description={p.description}
                      />
                    ))}
                  </div>
                  <button
                    type="button"
                    onClick={() => setPermDialogOpen(true)}
                    className="text-xs font-medium text-[var(--svx-color-brand-primary)] hover:underline"
                  >
                    {t("detail.viewAudit")} →
                  </button>
                </div>
              ) : (
                <div className="flex items-center gap-2 py-2">
                  <CheckCircle2Icon className="size-4 text-[var(--svx-color-success)]" />
                  <p className="text-sm text-[var(--svx-color-text-tertiary)]">
                    {t("detail.noPermissions")}
                  </p>
                </div>
              )}
            </Section>

            {/* ── Health ── */}
            <Section title={t("detail.health")} icon={HeartPulseIcon}>
              <div className="space-y-4">
                <HealthBar
                  value={detail.health.consecutive_failures}
                  max={5}
                  label={t("health.failures")}
                />
                <div className="flex items-center justify-between rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)]/60 px-3 py-2.5">
                  <span className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
                    {t("health.activeTasks")}
                  </span>
                  <span className="text-sm font-semibold tabular-nums text-[var(--svx-color-text-primary)]">
                    {detail.health.active_tasks}
                  </span>
                </div>
                {detail.health.last_error && (
                  <div className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-error)]/20 bg-[var(--svx-color-error)]/5 p-3">
                    <p className="text-xs font-medium text-[var(--svx-color-error)]">
                      {t("health.lastError")}
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-[var(--svx-color-text-secondary)]">
                      {detail.health.last_error}
                    </p>
                  </div>
                )}
              </div>
            </Section>

            {/* ── Events ── */}
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
                  <div className="space-y-4">
                    {manifest.events.emits.length > 0 && (
                      <div>
                        <p className="mb-2 text-xs font-semibold text-[var(--svx-color-text-secondary)]">
                          {t("detail.emits")}
                        </p>
                        <div className="flex flex-wrap gap-2">
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
                        <p className="mb-2 text-xs font-semibold text-[var(--svx-color-text-secondary)]">
                          {t("detail.subscribes")}
                        </p>
                        <div className="flex flex-wrap gap-2">
                          {manifest.events.subscribes.map((s) => (
                            <PluginToolBadge key={s} name={s} />
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="py-2 text-sm text-[var(--svx-color-text-tertiary)]">
                    {t("detail.noEvents")}
                  </p>
                )}
              </Section>
            )}

            {/* ── Dependencies ── */}
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
                  <div className="space-y-2">
                    {manifest.depends.map((d) => (
                      <div
                        key={d.name}
                        className="flex items-center gap-3 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)]/60 px-3 py-2"
                      >
                        <span className="text-sm font-medium text-[var(--svx-color-text-primary)]">
                          {d.name}
                        </span>
                        <span className="text-xs text-[var(--svx-color-text-tertiary)]">
                          {d.version}
                        </span>
                      </div>
                    ))}
                    {manifest.optional_depends.map((d) => (
                      <div
                        key={d.name}
                        className="flex items-center gap-3 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)]/40 px-3 py-2"
                      >
                        <span className="text-sm font-medium italic text-[var(--svx-color-text-secondary)]">
                          {d.name}
                        </span>
                        <span className="text-xs text-[var(--svx-color-text-tertiary)]">
                          {d.version}
                        </span>
                        <span className="ml-auto rounded-full bg-[var(--svx-color-bg-elevated)] px-2 py-0.5 text-[10px] text-[var(--svx-color-text-tertiary)]">
                          {t("detail.optional")}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="py-2 text-sm text-[var(--svx-color-text-tertiary)]">
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
                <pre className="max-h-56 overflow-auto rounded-[var(--svx-radius-lg)] bg-[var(--svx-color-bg-elevated)] p-4 text-xs leading-relaxed text-[var(--svx-color-text-secondary)] font-mono">
                  {JSON.stringify(manifest, null, 2)}
                </pre>
              </Section>
            )}
          </div>
        ) : (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-[var(--svx-color-text-tertiary)]">
              {t("detail.notFound")}
            </p>
          </div>
        )}
      </SheetContent>

      {/* Confirmation dialog */}
      {confirmAction && (
        <Dialog open={true} onOpenChange={() => setConfirmAction(null)}>
          <DialogContent className="sm:max-w-sm">
            <DialogTitle>
              {t(`actions.${confirmAction}ConfirmTitle`)}
            </DialogTitle>
            <DialogDescription>
              {t(`actions.${confirmAction}ConfirmDesc`)}
            </DialogDescription>
            <div className="flex justify-end gap-2 pt-4">
              <DialogClose className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] px-4 py-2 text-sm text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-bg-elevated)]">
                {t("actions.close")}
              </DialogClose>
              <button
                type="button"
                onClick={onConfirm}
                className={cn(
                  "rounded-[var(--svx-radius-md)] px-4 py-2 text-sm font-medium text-white hover:opacity-90 shadow-sm",
                  confirmAction === "disable"
                    ? "bg-[var(--svx-color-warning)]"
                    : "bg-[var(--svx-color-brand-primary)]",
                )}
              >
                {t("actions.confirm")}
              </button>
            </div>
          </DialogContent>
        </Dialog>
      )}

      {/* Permission audit dialog */}
      {detail && (
        <PermissionDialog
          open={permDialogOpen}
          onClose={() => setPermDialogOpen(false)}
          pluginName={detail.name}
          permissions={detail.permissions}
          allowedDomains={
            manifest?.network?.allowed_domains
          }
          mode="audit"
        />
      )}
    </Sheet>
  );
}

// ── Helper ──

function nameToHue(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash) % 360;
}
