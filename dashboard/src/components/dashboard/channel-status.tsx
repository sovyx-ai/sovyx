/**
 * Channel status card — connected channels + inline setup for disconnected ones.
 *
 * Connected: shows channel name + "Connected" badge.
 * Disconnected Telegram: paste token → validate → done.
 * Disconnected Signal: brief setup guide (requires external Docker).
 *
 * DASH-09: Channel status indicator for overview page.
 */

import { useState, useEffect, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  WifiIcon,
  WifiOffIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  ExternalLinkIcon,
  Loader2Icon,
  CheckCircle2Icon,
  AlertCircleIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface ChannelInfo {
  name: string;
  type: string;
  connected: boolean;
}

interface TelegramSetupResult {
  ok: boolean;
  bot_username?: string;
  bot_name?: string;
  requires_restart?: boolean;
  error?: string;
}

// ── Telegram inline setup ──

function TelegramSetup({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation("overview");
  const [token, setToken] = useState("");
  const [state, setState] = useState<"input" | "validating" | "success" | "error">("input");
  const [error, setError] = useState("");
  const [botUsername, setBotUsername] = useState("");

  const handleConnect = useCallback(async () => {
    const trimmed = token.trim();
    if (!trimmed) return;

    setState("validating");
    setError("");

    try {
      const res = await api.post<TelegramSetupResult>("/api/channels/telegram/setup", {
        token: trimmed,
      });

      if (res.ok) {
        setBotUsername(res.bot_username ?? "");
        setState("success");
      } else {
        setError(res.error ?? "Invalid token");
        setState("error");
      }
    } catch {
      setError("Could not connect — check your network");
      setState("error");
    }
  }, [token]);

  if (state === "success") {
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-emerald-400">
          <CheckCircle2Icon className="size-4" />
          <span className="text-xs font-medium">
            Connected to @{botUsername}
          </span>
        </div>
        <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
          Restart Sovyx to activate the channel.
        </p>
        <button
          type="button"
          onClick={onDone}
          className="text-[11px] text-[var(--svx-color-brand-primary)] hover:underline"
        >
          Done
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Step 1: Get token */}
      <div className="flex items-center gap-1.5 text-xs text-[var(--svx-color-text-secondary)]">
        <span className="font-medium">1.</span>
        <span>{t("channelSetup.getToken")}</span>
        <a
          href="https://t.me/BotFather"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-0.5 font-medium text-[var(--svx-color-brand-primary)] hover:underline"
        >
          @BotFather
          <ExternalLinkIcon className="size-2.5" />
        </a>
      </div>

      {/* Step 2: Paste token */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-1.5 text-xs text-[var(--svx-color-text-secondary)]">
          <span className="font-medium">2.</span>
          <span>{t("channelSetup.pasteToken")}</span>
        </div>
        <div className="flex gap-2">
          <input
            type="password"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              if (state === "error") setState("input");
            }}
            placeholder="123456:ABC-DEF..."
            className={cn(
              "h-8 flex-1 rounded-md border bg-[var(--svx-color-bg-elevated)] px-2.5 text-xs",
              "text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)]",
              "focus:outline-none focus:ring-1 focus:ring-[var(--svx-color-brand-primary)]",
              state === "error"
                ? "border-[var(--svx-color-error)]"
                : "border-[var(--svx-color-border-default)]",
            )}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleConnect();
            }}
            disabled={state === "validating"}
            autoComplete="off"
            data-testid="telegram-token-input"
          />
          <button
            type="button"
            onClick={() => void handleConnect()}
            disabled={!token.trim() || state === "validating"}
            className={cn(
              "h-8 rounded-md px-3 text-xs font-medium transition-colors",
              "bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)]",
              "hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed",
            )}
            data-testid="telegram-connect-btn"
          >
            {state === "validating" ? (
              <Loader2Icon className="size-3.5 animate-spin" />
            ) : (
              "Connect"
            )}
          </button>
        </div>

        {/* Error message */}
        {state === "error" && (
          <div className="flex items-center gap-1.5 text-[11px] text-[var(--svx-color-error)]">
            <AlertCircleIcon className="size-3" />
            {error}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Signal setup (still guide-based — needs external Docker) ──

function SignalSetup() {
  const { t } = useTranslation("overview");
  return (
    <div className="space-y-2 text-xs text-[var(--svx-color-text-secondary)]">
      <p>{t("channelSetup.signalRequires")}{" "}
        <a
          href="https://github.com/bbernhard/signal-cli-rest-api"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-0.5 font-medium text-[var(--svx-color-brand-primary)] hover:underline"
        >
          signal-cli-rest-api
          <ExternalLinkIcon className="size-2.5" />
        </a>.
      </p>
      <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
        {t("channelSetup.signalConfigure")}
      </p>
    </div>
  );
}

const SETUP_COMPONENTS: Record<string, React.FC<{ onDone: () => void }>> = {
  telegram: TelegramSetup,
  signal: SignalSetup,
};

// ── Main card ──

export function ChannelStatusCard() {
  const { t } = useTranslation("overview");
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);

  const fetchChannels = useCallback(async () => {
    try {
      const resp = await api.get<{ channels: ChannelInfo[] }>("/api/channels");
      setChannels(resp.channels);
    } catch {
      // Silently fail
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchChannels();
  }, [fetchChannels]);

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

  const toggle = (type: string) => setExpanded((prev) => (prev === type ? null : type));

  return (
    <div
      className="rounded-xl border border-[var(--svx-color-border-subtle)] bg-[var(--svx-color-bg-elevated)] p-4"
      data-testid="channel-status-card"
    >
      <h3 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
        {t("channels.title", { defaultValue: "Channels" })}
      </h3>
      <div className="mt-3 space-y-1">
        {channels.map((ch) => {
          const SetupComponent = !ch.connected ? SETUP_COMPONENTS[ch.type] : undefined;
          const isExpanded = expanded === ch.type;

          return (
            <div key={ch.type} data-testid={`channel-${ch.type}`}>
              {/* Channel row */}
              <div
                className={cn(
                  "flex items-center justify-between rounded-md px-2 py-1.5 text-sm",
                  !ch.connected && SetupComponent && "cursor-pointer hover:bg-[var(--svx-color-bg-hover)]",
                )}
                role={!ch.connected && SetupComponent ? "button" : undefined}
                tabIndex={!ch.connected && SetupComponent ? 0 : undefined}
                onClick={() => !ch.connected && SetupComponent && toggle(ch.type)}
                onKeyDown={(e) => {
                  if (!ch.connected && SetupComponent && (e.key === "Enter" || e.key === " ")) {
                    e.preventDefault();
                    toggle(ch.type);
                  }
                }}
              >
                <span className="text-[var(--svx-color-text-secondary)]">{ch.name}</span>
                <span
                  className={cn(
                    "flex items-center gap-1.5 text-xs font-medium",
                    ch.connected ? "text-emerald-400" : "text-[var(--svx-color-text-disabled)]",
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
                      {t("channels.setup", { defaultValue: "Set up" })}
                      {SetupComponent && (
                        isExpanded
                          ? <ChevronUpIcon className="size-3" />
                          : <ChevronDownIcon className="size-3" />
                      )}
                    </>
                  )}
                </span>
              </div>

              {/* Expandable setup area */}
              {!ch.connected && SetupComponent && isExpanded && (
                <div className="mx-2 mb-2 rounded-md border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-3">
                  <SetupComponent onDone={() => {
                    setExpanded(null);
                    void fetchChannels();
                  }} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
