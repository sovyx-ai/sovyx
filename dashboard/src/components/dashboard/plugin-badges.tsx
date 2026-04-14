/**
 * Plugin badge components — reusable across card and detail views.
 *
 * - PluginToolBadge: tool name with tooltip
 * - PermissionBadge: permission + risk color + description tooltip
 * - CategoryBadge: category with icon
 * - PricingBadge: free/paid/freemium
 *
 * TASK-456
 */

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { PermissionRisk } from "@/types/api";

// ── Shared badge base ──

function BadgeBase({
  children,
  className,
  title,
}: {
  children: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium",
        className,
      )}
      title={title}
    >
      {children}
    </span>
  );
}

// ── Tool Badge ──

interface PluginToolBadgeProps {
  name: string;
  description?: string;
  className?: string;
}

export function PluginToolBadge({
  name,
  description,
  className,
}: PluginToolBadgeProps) {
  return (
    <BadgeBase
      className={cn(
        "bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-secondary)]",
        className,
      )}
      title={description || name}
    >
      <span className="text-[var(--svx-color-text-tertiary)]">⚙️</span>
      {name}
    </BadgeBase>
  );
}

// ── Permission Badge ──

const RISK_STYLES: Record<
  PermissionRisk,
  { bg: string; text: string; dot: string }
> = {
  low: {
    bg: "bg-[var(--svx-color-success)]/10",
    text: "text-[var(--svx-color-success)]",
    dot: "bg-[var(--svx-color-success)]",
  },
  medium: {
    bg: "bg-[var(--svx-color-warning)]/10",
    text: "text-[var(--svx-color-warning)]",
    dot: "bg-[var(--svx-color-warning)]",
  },
  high: {
    bg: "bg-[var(--svx-color-error)]/10",
    text: "text-[var(--svx-color-error)]",
    dot: "bg-[var(--svx-color-error)]",
  },
};

interface PermissionBadgeProps {
  permission: string;
  risk: PermissionRisk;
  description?: string;
  className?: string;
}

export function PermissionBadge({
  permission,
  risk,
  description,
  className,
}: PermissionBadgeProps) {
  const { t } = useTranslation("plugins");
  const styles = RISK_STYLES[risk] ?? RISK_STYLES.medium;

  return (
    <BadgeBase
      className={cn(styles.bg, styles.text, className)}
      title={description || t(`permission.risk.${risk}`)}
    >
      <span
        className={cn("size-1.5 rounded-full shrink-0", styles.dot)}
        aria-hidden="true"
      />
      {permission}
    </BadgeBase>
  );
}

// ── Category Badge ──

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
  health: "❤️",
  education: "📚",
  gaming: "🎮",
  travel: "✈️",
  music: "🎵",
  shopping: "🛒",
  utility: "🔧",
  analytics: "📈",
};

interface CategoryBadgeProps {
  category: string;
  className?: string;
}

export function CategoryBadge({ category, className }: CategoryBadgeProps) {
  if (!category) return null;

  const icon = CATEGORY_ICONS[category.toLowerCase()] ?? "🔌";

  return (
    <BadgeBase
      className={cn(
        "bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-secondary)]",
        className,
      )}
    >
      {icon}
      <span>{category}</span>
    </BadgeBase>
  );
}

// ── Pricing Badge ──

type PricingStyle = { bg: string; text: string };

const PRICING_FALLBACK: PricingStyle = {
  bg: "bg-[var(--svx-color-success)]/10",
  text: "text-[var(--svx-color-success)]",
};

const PRICING_STYLES: Record<string, PricingStyle> = {
  free: PRICING_FALLBACK,
  paid: {
    bg: "bg-[var(--svx-color-warning)]/10",
    text: "text-[var(--svx-color-warning)]",
  },
  freemium: {
    bg: "bg-[var(--svx-color-brand-primary)]/10",
    text: "text-[var(--svx-color-brand-primary)]",
  },
};

interface PricingBadgeProps {
  pricing: string;
  className?: string;
}

export function PricingBadge({ pricing, className }: PricingBadgeProps) {
  const { t } = useTranslation("plugins");
  const styles = PRICING_STYLES[pricing] ?? PRICING_FALLBACK;
  const label = t(
    `pricing.${pricing}`,
    pricing.charAt(0).toUpperCase() + pricing.slice(1),
  );

  return (
    <BadgeBase className={cn(styles.bg, styles.text, className)}>
      {label}
    </BadgeBase>
  );
}
