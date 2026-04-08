/**
 * Channel status card — shows which channels are connected.
 *
 * DASH-09: Channel status indicator for overview page.
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { WifiIcon, WifiOffIcon } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface ChannelInfo {
  name: string;
  type: string;
  connected: boolean;
}

export function ChannelStatusCard() {
  const { t } = useTranslation("overview");
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function fetchChannels() {
      try {
        const resp = await api.get<{ channels: ChannelInfo[] }>("/api/channels");
        if (!cancelled) {
          setChannels(resp.channels);
        }
      } catch {
        // Silently fail — card just shows loading/empty
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void fetchChannels();
    return () => { cancelled = true; };
  }, []);

  if (loading) {
    return (
      <div className="rounded-xl border border-[var(--svx-color-border-subtle)] bg-[var(--svx-color-bg-elevated)] p-4">
        <h3 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
          {t("channels.title", { defaultValue: "Channels" })}
        </h3>
        <div className="mt-3 space-y-2">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-6 animate-pulse rounded bg-[var(--svx-color-bg-surface)]" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div
      className="rounded-xl border border-[var(--svx-color-border-subtle)] bg-[var(--svx-color-bg-elevated)] p-4"
      data-testid="channel-status-card"
    >
      <h3 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
        {t("channels.title", { defaultValue: "Channels" })}
      </h3>
      <div className="mt-3 space-y-2">
        {channels.map((ch) => (
          <div
            key={ch.type}
            className="flex items-center justify-between text-sm"
            data-testid={`channel-${ch.type}`}
          >
            <span className="text-[var(--svx-color-text-secondary)]">
              {ch.name}
            </span>
            <span
              className={cn(
                "flex items-center gap-1.5 text-xs font-medium",
                ch.connected
                  ? "text-emerald-400"
                  : "text-[var(--svx-color-text-disabled)]",
              )}
            >
              {ch.connected ? (
                <>
                  <WifiIcon className="size-3" />
                  {t("channels.connected", { defaultValue: "Connected" })}
                </>
              ) : (
                <>
                  <WifiOffIcon className="size-3" />
                  {t("channels.disconnected", { defaultValue: "Not connected" })}
                </>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
