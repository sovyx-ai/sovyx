import { cn } from "@/lib/utils";

const CHANNEL_CONFIG: Record<string, { icon: string; label: string; color: string }> = {
  telegram: { icon: "✈️", label: "Telegram", color: "text-[var(--svx-color-info)]" },
  discord: { icon: "💬", label: "Discord", color: "text-[var(--svx-color-brand-primary)]" },
  signal: { icon: "🔒", label: "Signal", color: "text-[var(--svx-color-info)]" },
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
