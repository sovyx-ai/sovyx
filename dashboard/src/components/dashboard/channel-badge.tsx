import { cn } from "@/lib/utils";

const CHANNEL_CONFIG: Record<string, { icon: string; label: string; color: string }> = {
  telegram: { icon: "✈️", label: "Telegram", color: "text-[oklch(0.65_0.15_230)]" },
  discord: { icon: "💬", label: "Discord", color: "text-[oklch(0.60_0.18_275)]" },
  signal: { icon: "🔒", label: "Signal", color: "text-[oklch(0.65_0.15_230)]" },
  cli: { icon: "⌨️", label: "CLI", color: "text-muted-foreground" },
};

interface ChannelBadgeProps {
  channel: string;
  className?: string;
}

export function ChannelBadge({ channel, className }: ChannelBadgeProps) {
  const config = CHANNEL_CONFIG[channel.toLowerCase()] ?? {
    icon: "📨",
    label: channel,
    color: "text-muted-foreground",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md bg-secondary/50 px-1.5 py-0.5 text-[10px] font-medium",
        config.color,
        className,
      )}
    >
      {config.icon} {config.label}
    </span>
  );
}
