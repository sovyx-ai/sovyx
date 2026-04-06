/**
 * Deterministic letter avatar — generates a consistent color based on ID hash.
 * Colors use HEX values aligned with the Sovyx design system.
 */

import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";

/**
 * Avatar color palette — hex values for inline style={{ backgroundColor }}.
 * Each color is documented with its --svx-* token equivalent.
 * Hex is required here because inline styles need resolved values for
 * deterministic color assignment (hash-based).
 */
const AVATAR_COLORS = [
  "#22C55E",  // --svx-color-success
  "#3B82F6",  // --svx-color-info
  "#8B5CF6",  // --svx-color-brand-primary
  "#EC4899",  // pink (no direct token — accent palette)
  "#F59E0B",  // --svx-color-warning
  "#22D3EE",  // --svx-color-accent-cyan
  "#FB923C",  // orange (no direct token — accent palette)
  "#A78BFA",  // --svx-color-brand-muted
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
