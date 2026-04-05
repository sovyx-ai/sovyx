/**
 * Deterministic letter avatar — generates a consistent color based on ID hash.
 * Colors use HEX values aligned with the Sovyx design system.
 */

import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";

const AVATAR_COLORS = [
  "#22C55E",  // success green
  "#3B82F6",  // info blue
  "#8B5CF6",  // brand violet
  "#EC4899",  // pink
  "#F59E0B",  // warning amber
  "#22D3EE",  // accent cyan
  "#FB923C",  // orange
  "#A78BFA",  // brand muted
] as const;

function hashString(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

interface LetterAvatarProps {
  name: string;
  size?: number;
  className?: string;
}

export function LetterAvatar({ name, size = 32, className }: LetterAvatarProps) {
  const letter = (name[0] ?? "?").toUpperCase();
  const color = AVATAR_COLORS[hashString(name) % AVATAR_COLORS.length];

  return (
    <div
      className={className}
      style={{
        width: size,
        height: size,
        borderRadius: "var(--svx-radius-full)",
        backgroundColor: color,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: size * 0.45,
        fontWeight: 600,
        color: "var(--svx-color-text-inverse)",
        flexShrink: 0,
      }}
    >
      {letter}
    </div>
  );
}

/** Mind avatar — crystal ball emoji on brand background. */
export function MindAvatar({ className }: { className?: string }) {
  return (
    <Avatar className={cn("size-8", className)}>
      <AvatarFallback className="bg-primary text-primary-foreground text-xs">
        🔮
      </AvatarFallback>
    </Avatar>
  );
}
