import type { ReactNode } from "react";
import { RocketIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface ComingSoonProps {
  /** Lucide icon element rendered above the title. */
  icon?: ReactNode;
  /** Page / feature title. */
  title: string;
  /** One-liner describing the feature. */
  description?: string;
  /** Planned features rendered as an unchecked checklist. */
  features?: string[];
  /** Version badge text — shown as "Available in {versionBadge}". */
  versionBadge?: string;
  /** Extra container className. */
  className?: string;
}

/**
 * Reusable placeholder card for features planned for future releases.
 *
 * Renders a dashed-border card with icon, title, description,
 * feature checklist, and version badge.  Used by all "coming soon" pages.
 */
export function ComingSoon({
  icon,
  title,
  description,
  features,
  versionBadge = "v1.0",
  className,
}: ComingSoonProps) {
  return (
    <Card
      className={cn("border-dashed", className)}
      data-testid="coming-soon-card"
    >
      <CardContent className="flex flex-col items-center justify-center gap-4 py-10 text-center">
        {/* Icon */}
        <div className="flex size-12 items-center justify-center rounded-lg bg-muted text-muted-foreground">
          {icon ?? <RocketIcon className="size-6" />}
        </div>

        {/* Title + description */}
        <div className="space-y-1">
          <h3 className="text-base font-semibold text-foreground">{title}</h3>
          {description && (
            <p className="mx-auto max-w-md text-sm text-muted-foreground">
              {description}
            </p>
          )}
        </div>

        {/* Feature checklist */}
        {features && features.length > 0 && (
          <ul
            className="mx-auto max-w-xs space-y-1 text-left text-sm text-muted-foreground"
            data-testid="feature-list"
          >
            {features.map((feat) => (
              <li key={feat} className="flex items-center gap-2">
                <span className="inline-block size-4 shrink-0 rounded border border-muted-foreground/30" />
                {feat}
              </li>
            ))}
          </ul>
        )}

        {/* Version badge */}
        <Badge variant="secondary" className="text-xs">
          Available in {versionBadge}
        </Badge>
      </CardContent>
    </Card>
  );
}

interface TabPlaceholderProps {
  label: string;
  version?: string;
}

/** Placeholder for a settings tab that isn't built yet. */
export function TabPlaceholder({
  label,
  version = "v1.0",
}: TabPlaceholderProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
      <RocketIcon className="size-6 text-muted-foreground/30" />
      <p className="text-xs text-muted-foreground">
        {label} — coming in {version}
      </p>
    </div>
  );
}
