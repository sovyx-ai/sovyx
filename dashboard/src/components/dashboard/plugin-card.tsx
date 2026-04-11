/**
 * PluginCard — Hero card for plugin list view.
 *
 * Displays plugin identity, status, tools count, permissions risk,
 * and category. Glass morphism for active, muted for disabled.
 *
 * TASK-454
 */

import { useState, useRef, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { MoreVerticalIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDashboardStore } from "@/stores/dashboard";
import type { PluginInfo, PermissionRisk } from "@/types/api";

// ── Letter Avatar ──

/** Generate a deterministic gradient from plugin name. */
function nameToHue(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash) % 360;
}

function LetterAvatar({
  name,
  className,
}: {
  name: string;
  className?: string;
}) {
  const hue = nameToHue(name);
  const letter = name.charAt(0).toUpperCase();

  return (
    <div
      className={cn(
        "flex items-center justify-center rounded-[var(--svx-radius-md)] text-sm font-bold text-white",
        className,
      )}
      style={{
        background: `linear-gradient(135deg, hsl(${hue}, 70%, 50%), hsl(${(hue + 40) % 360}, 70%, 40%))`,
      }}
      aria-hidden="true"
    >
      {letter}
    </div>
  );
}

// ── Status Dot ──

const STATUS_STYLES = {
  active:
    "bg-[var(--svx-color-brand-primary)] shadow-[0_0_6px_var(--svx-color-brand-primary)]",
  disabled: "bg-[var(--svx-color-text-disabled)]",
  error:
    "bg-[var(--svx-color-error)] shadow-[0_0_6px_var(--svx-color-error)] animate-pulse",
} as const;

function PluginStatusDot({ status }: { status: string }) {
  const style =
    STATUS_STYLES[status as keyof typeof STATUS_STYLES] ??
    STATUS_STYLES.disabled;
  return <div className={cn("size-2 rounded-full shrink-0", style)} />;
}

// ── Risk Indicator ──

const RISK_COLORS: Record<PermissionRisk, string> = {
  low: "text-[var(--svx-color-success)]",
  medium: "text-[var(--svx-color-warning)]",
  high: "text-[var(--svx-color-error)]",
};

const RISK_DOTS: Record<PermissionRisk, string> = {
  low: "🟢",
  medium: "🟡",
  high: "🔴",
};

function highestRisk(
  permissions: PluginInfo["permissions"],
): PermissionRisk | null {
  if (!permissions.length) return null;
  if (permissions.some((p) => p.risk === "high")) return "high";
  if (permissions.some((p) => p.risk === "medium")) return "medium";
  return "low";
}

// ── Category Icons ──

const CATEGORY_ICONS: Record<string, string> = {
  finance: "💰",
  weather: "🌤️",
  productivity: "⚡",
  social: "💬",
  data: "📊",
  security: "🔒",
  ai: "🧠",
  media: "🎬",
  developer: "🛠️",
  communication: "📡",
};

// ── Kebab Menu ──

function QuickActions({
  plugin,
  onViewDetails,
}: {
  plugin: PluginInfo;
  onViewDetails: () => void;
}) {
  const { t } = useTranslation("plugins");
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const enablePlugin = useDashboardStore((s) => s.enablePlugin);
  const disablePlugin = useDashboardStore((s) => s.disablePlugin);
  const reloadPlugin = useDashboardStore((s) => s.reloadPlugin);

  // Close on click outside
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const handleAction = async (action: "enable" | "disable" | "reload" | "details") => {
    setOpen(false);
    if (action === "details") {
      onViewDetails();
      return;
    }
    if (action === "disable" && !window.confirm(t("actions.disableConfirm"))) return;
    if (action === "reload" && !window.confirm(t("actions.reloadConfirm"))) return;

    let success = false;
    if (action === "enable") success = await enablePlugin(plugin.name);
    else if (action === "disable") success = await disablePlugin(plugin.name);
    else success = await reloadPlugin(plugin.name);

    if (success) {
      toast.success(t(`actions.${action === "enable" ? "enabled" : action === "disable" ? "disabled" : "reloaded"}`));
    } else {
      toast.error(t(`actions.${action}Failed`));
    }
  };

  return (
    <div className="relative" ref={menuRef}>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(!open);
        }}
        className="rounded-[var(--svx-radius-sm)] p-1 text-[var(--svx-color-text-tertiary)] hover:bg-[var(--svx-color-bg-elevated)] hover:text-[var(--svx-color-text-secondary)] opacity-0 group-hover:opacity-100 transition-opacity"
        aria-label="Plugin actions"
      >
        <MoreVerticalIcon className="size-3.5" />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 min-w-[140px] rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] py-1 shadow-lg">
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); void handleAction("details"); }}
            className="flex w-full items-center px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-bg-elevated)]"
          >
            {t("card.viewDetails")}
          </button>
          {plugin.status === "disabled" ? (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); void handleAction("enable"); }}
              className="flex w-full items-center px-3 py-1.5 text-xs text-[var(--svx-color-success)] hover:bg-[var(--svx-color-bg-elevated)]"
            >
              {t("actions.enable")}
            </button>
          ) : (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); void handleAction("disable"); }}
              className="flex w-full items-center px-3 py-1.5 text-xs text-[var(--svx-color-warning)] hover:bg-[var(--svx-color-bg-elevated)]"
            >
              {t("actions.disable")}
            </button>
          )}
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); void handleAction("reload"); }}
            className="flex w-full items-center px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-bg-elevated)]"
          >
            {t("actions.reload")}
          </button>
        </div>
      )}
    </div>
  );
}

// ── Card Component ──

interface PluginCardProps {
  plugin: PluginInfo;
  onClick?: () => void;
  className?: string;
  /** Animation delay for stagger entrance (ms) */
  delay?: number;
}

export function PluginCard({
  plugin,
  onClick,
  className,
  delay = 0,
}: PluginCardProps) {
  const { t } = useTranslation("plugins");
  const risk = highestRisk(plugin.permissions);
  const isActive = plugin.status === "active";
  const categoryIcon =
    CATEGORY_ICONS[plugin.category] ?? (plugin.category ? "🔌" : "");

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        // Base
        "group relative w-full rounded-[var(--svx-radius-lg)] border p-4 text-left transition-all duration-200",
        // Active: glass morphism
        isActive && [
          "border-[var(--svx-color-border-default)]",
          "bg-[var(--svx-color-bg-surface)]",
          "hover:border-[var(--svx-color-brand-primary)]/40",
          "hover:shadow-[var(--svx-shadow-glow-sm)]",
        ],
        // Disabled/error: muted
        !isActive && [
          "border-[var(--svx-color-border-default)]/60",
          "bg-[var(--svx-color-bg-surface)]/60",
          "opacity-75 hover:opacity-90",
        ],
        className,
      )}
      style={{
        animationDelay: `${delay}ms`,
      }}
      role="article"
      aria-label={`${plugin.name} — ${t(`status.${plugin.status}`)}`}
    >
      {/* Header: Icon + Name + Status */}
      <div className="flex items-start gap-3">
        {plugin.icon_url ? (
          <img
            src={plugin.icon_url}
            alt=""
            className="size-10 rounded-[var(--svx-radius-md)] object-cover"
          />
        ) : (
          <LetterAvatar name={plugin.name} className="size-10" />
        )}

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-sm font-semibold text-[var(--svx-color-text-primary)]">
              {plugin.name}
            </h3>
            <span className="shrink-0 text-[10px] text-[var(--svx-color-text-tertiary)]">
              v{plugin.version}
            </span>
          </div>

          <p className="mt-0.5 line-clamp-2 text-xs text-[var(--svx-color-text-secondary)]">
            {plugin.description || t("card.noDescription")}
          </p>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <PluginStatusDot status={plugin.status} />
          <QuickActions
            plugin={plugin}
            onViewDetails={() => onClick?.()}
          />
        </div>
      </div>

      {/* Footer: Badges */}
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {/* Tool count */}
        <span className="inline-flex items-center rounded-full bg-[var(--svx-color-bg-elevated)] px-2 py-0.5 text-[10px] font-medium text-[var(--svx-color-text-secondary)]">
          {t("card.tools", { count: plugin.tools_count })}
        </span>

        {/* Risk indicator */}
        {risk && (
          <span
            className={cn(
              "inline-flex items-center gap-0.5 rounded-full bg-[var(--svx-color-bg-elevated)] px-2 py-0.5 text-[10px] font-medium",
              RISK_COLORS[risk],
            )}
            title={t(`permission.risk.${risk}`)}
          >
            {RISK_DOTS[risk]}
          </span>
        )}

        {/* Category */}
        {categoryIcon && (
          <span className="inline-flex items-center gap-0.5 rounded-full bg-[var(--svx-color-bg-elevated)] px-2 py-0.5 text-[10px] font-medium text-[var(--svx-color-text-secondary)]">
            {categoryIcon}
            {plugin.category && (
              <span className="ml-0.5">{plugin.category}</span>
            )}
          </span>
        )}

        {/* Pricing (only if not free) */}
        {plugin.pricing && plugin.pricing !== "free" && (
          <span className="inline-flex items-center rounded-full bg-[var(--svx-color-warning)]/10 px-2 py-0.5 text-[10px] font-medium text-[var(--svx-color-warning)]">
            {t(`pricing.${plugin.pricing}`)}
          </span>
        )}
      </div>

      {/* Health warning */}
      {plugin.health.consecutive_failures > 0 && (
        <div className="mt-2 flex items-center gap-1 text-[10px] text-[var(--svx-color-warning)]">
          <span>⚠️</span>
          <span>
            {t("health.autoDisableWarning", {
              count: plugin.health.consecutive_failures,
              max: 5,
            })}
          </span>
        </div>
      )}

      {/* Hover overlay */}
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-[var(--svx-radius-lg)] bg-black/0 transition-colors group-hover:bg-black/5 dark:group-hover:bg-white/5">
        <span className="text-xs font-medium text-[var(--svx-color-text-primary)] opacity-0 transition-opacity group-hover:opacity-100">
          {t("card.viewDetails")}
        </span>
      </div>
    </button>
  );
}
