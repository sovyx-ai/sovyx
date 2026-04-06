import { cn } from "@/lib/utils";

const CHANNEL_CONFIG: Record<string, { icon: string; label: string; color: string }> = {
  telegram: { icon: "✈️", label: "Telegram", color: "text-[#3B82F6]" },    /* info blue */
  discord: { icon: "💬", label: "Discord", color: "text-[#8B5CF6]" },      /* brand violet */
  signal: { icon: "🔒", label: "Signal", color: "text-[#3B82F6]" },        /* info blue */
  cli: { icon: "⌨️", label: "CLI", color: "text-[var(--svx-color-text-secondary)]" },
  api: { icon: "🔗", label: "API", color: "text-[var(--svx-color-text-secondary)]" },
};

interface ChannelBadgeProps {
  channel: string;
  className?: string;
}

export function ChannelBadge({ channel, className }: ChannelBadgeProps) {
  const config = CHANNEL_CONFIG[channel.toLowerCase()] ?? {
    icon: "📨",
    label: channel,
    color: "text-[var(--svx-color-text-secondary)]",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md bg-[var(--svx-color-bg-elevated)] px-1.5 py-0.5 text-[10px] font-medium",
        config.color,
        className,
      )}
      title={config.label}
    >
      {config.icon} {config.label}
    </span>
  );
}
