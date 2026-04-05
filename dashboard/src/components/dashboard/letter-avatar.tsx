import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { cn } from "@/lib/utils";

const AVATAR_COLORS = [
  "oklch(0.70 0.18 160)", // emerald
  "oklch(0.65 0.18 250)", // blue
  "oklch(0.60 0.22 285)", // violet
  "oklch(0.70 0.15 350)", // pink
  "oklch(0.75 0.16 70)",  // amber
  "oklch(0.65 0.15 175)", // teal
  "oklch(0.70 0.18 50)",  // orange
  "oklch(0.70 0.18 130)", // lime
] as const;

function getAvatarColor(name: string): string {
  const hash = name
    .split("")
    .reduce((acc, char) => acc + char.charCodeAt(0), 0);
  return AVATAR_COLORS[hash % AVATAR_COLORS.length] ?? AVATAR_COLORS[0];
}

interface LetterAvatarProps {
  name: string;
  className?: string;
}

export function LetterAvatar({ name, className }: LetterAvatarProps) {
  const color = getAvatarColor(name);
  const initial = name.charAt(0).toUpperCase();

  return (
    <Avatar className={cn("size-8", className)}>
      <AvatarFallback
        style={{ backgroundColor: color, color: "white" }}
        className="text-xs font-medium"
      >
        {initial}
      </AvatarFallback>
    </Avatar>
  );
}

export function MindAvatar({ className }: { className?: string }) {
  return (
    <Avatar className={cn("size-8", className)}>
      <AvatarFallback className="bg-primary text-primary-foreground text-xs">
        🔮
      </AvatarFallback>
    </Avatar>
  );
}
