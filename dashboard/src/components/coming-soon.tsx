/**
 * ComingSoon — Shared placeholder component for future features.
 *
 * Used by all 5 placeholder pages (Voice, Emotions, Productivity, Plugins, Home).
 * Shows icon, title, description, feature checklist, and version badge.
 *
 * Visual: Dashed border card, centered content, brand-subtle icon background.
 * All colors from --svx-* tokens.
 *
 * Ref: DASH-21a, META-06 §15
 */

import type { ReactNode } from "react";
import { RocketIcon, CircleIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface ComingSoonProps {
  title: string;
  description?: string;
  icon?: ReactNode;
  features?: string[];
  version?: string;
  className?: string;
}

export function ComingSoon({
  title,
  description,
  icon,
  features,
  version = "v1.0",
  className,
}: ComingSoonProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-[var(--svx-radius-lg)] border border-dashed border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-6 py-12 text-center",
        className,
      )}
    >
      {/* Icon with brand-subtle background */}
      <div className="mb-4 flex size-16 items-center justify-center rounded-[var(--svx-radius-xl)] bg-[var(--svx-color-brand-subtle)]">
        <span className="text-[var(--svx-color-brand-primary)]">
          {icon ?? <RocketIcon className="size-8" />}
        </span>
      </div>

      {/* Title */}
      <h2 className="text-lg font-semibold text-[var(--svx-color-text-primary)]">
        {title}
      </h2>

      {/* Description */}
      {description && (
        <p className="mt-2 max-w-md text-sm text-[var(--svx-color-text-secondary)]">
          {description}
        </p>
      )}

      {/* Feature checklist */}
      {features && features.length > 0 && (
        <ul className="mt-4 space-y-1.5 text-left">
          {features.map((feature) => (
            <li key={feature} className="flex items-center gap-2 text-xs">
              <CircleIcon className="size-3 shrink-0 text-[var(--svx-color-text-disabled)]" />
              <span className="text-[var(--svx-color-text-tertiary)]">{feature}</span>
            </li>
          ))}
        </ul>
      )}

      {/* Version badge */}
      <span className="mt-4 inline-flex rounded-[var(--svx-radius-full)] bg-[var(--svx-color-bg-elevated)] px-3 py-1 text-[10px] font-medium text-[var(--svx-color-text-tertiary)]">
        Available in {version}
      </span>
    </div>
  );
}
