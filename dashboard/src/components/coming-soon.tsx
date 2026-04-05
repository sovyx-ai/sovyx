import type { ReactNode } from "react";
import { RocketIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface ComingSoonProps {
  title: string;
  description?: string;
  icon?: ReactNode;
  version?: string;
  className?: string;
}

/** Placeholder card for features planned for future releases. */
export function ComingSoon({
  title,
  description,
  icon,
  version = "v1.0",
  className,
}: ComingSoonProps) {
  return (
    <Card className={cn("border-dashed", className)}>
      <CardContent className="flex flex-col items-center justify-center gap-3 py-10 text-center">
        <div className="text-muted-foreground/30">
          {icon ?? <RocketIcon className="size-10" />}
        </div>
        <div>
          <h3 className="text-sm font-medium text-foreground/70">{title}</h3>
          {description && (
            <p className="mt-1 max-w-xs text-xs text-muted-foreground">{description}</p>
          )}
        </div>
        <Badge variant="secondary" className="text-[10px]">
          Coming in {version}
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
export function TabPlaceholder({ label, version = "v1.0" }: TabPlaceholderProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
      <RocketIcon className="size-6 text-muted-foreground/30" />
      <p className="text-xs text-muted-foreground">
        {label} — coming in {version}
      </p>
    </div>
  );
}
