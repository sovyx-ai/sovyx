/**
 * EmptyState — Generic empty state for pages with no data.
 *
 * Ref: DASH-42
 */

import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  icon: ReactNode;
  title: string;
  description?: string;
  action?: {
    label: string;
    onClick: () => void;
  };
  className?: string;
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 py-16 text-center", className)}>
      <div className="text-[var(--svx-color-text-disabled)]">{icon}</div>
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
