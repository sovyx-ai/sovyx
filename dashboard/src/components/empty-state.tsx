/**
 * EmptyState — Generic empty state for pages with no data.
 *
 * Supports optional `animation` slot for branded visual storytelling (REFINE-06).
 *
 * Ref: DASH-42, REFINE-06
 */

import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  /** Lucide icon or similar — shown when no animation is provided */
  icon: ReactNode;
  title: string;
  description?: string;
  action?: {
    label: string;
    onClick: () => void;
  };
  /** Branded CSS animation component — replaces the icon when provided */
  animation?: ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, description, action, animation, className }: EmptyStateProps) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 py-16 text-center", className)}>
      {animation ? (
        <div className="mb-1">{animation}</div>
      ) : (
        <div className="text-[var(--svx-color-text-disabled)]">{icon}</div>
      )}
      <div>
        <h3 className="text-sm font-medium text-[var(--svx-color-text-secondary)]">{title}</h3>
        {description && (
          <p className="mt-1 max-w-xs text-xs text-[var(--svx-color-text-tertiary)]">{description}</p>
        )}
      </div>
      {action && (
        <Button variant="secondary" size="sm" onClick={action.onClick} className="mt-1">
          {action.label}
        </Button>
      )}
    </div>
  );
}
