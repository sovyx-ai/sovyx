/**
 * Channel status card — shows connected channels + setup guide for disconnected ones.
 *
 * Connected: shows channel name + "Connected" badge.
 * Disconnected: expandable setup guide with step-by-step instructions.
 *
 * DASH-09: Channel status indicator for overview page.
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  WifiIcon,
  WifiOffIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  ExternalLinkIcon,
  CopyIcon,
  CheckIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface ChannelInfo {
  name: string;
  type: string;
  connected: boolean;
}

/** Inline code with copy button. */
function CopyCode({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API not available
    }
  };

  return (
    <span className="inline-flex items-center gap-1">
      <code className="rounded bg-[var(--svx-color-bg-elevated)] px-1.5 py-0.5 text-[11px] text-[var(--svx-color-text-secondary)]">
        {text}
      </code>
      <button
        type="button"
        onClick={() => void handleCopy()}
        className="text-[var(--svx-color-text-disabled)] hover:text-[var(--svx-color-text-secondary)] transition-colors"
        aria-label={`Copy ${text}`}
      >
        {copied ? (
          <CheckIcon className="size-3 text-emerald-400" />
        ) : (
          <CopyIcon className="size-3" />
        )}
      </button>
    </span>
  );
}

/** Setup guide content per channel type. */
function TelegramSetupGuide() {
  return (
    <div className="mt-2 space-y-2 text-xs text-[var(--svx-color-text-secondary)]">
      <p className="font-medium text-[var(--svx-color-text-primary)]">Connect Telegram</p>
      <ol className="list-inside list-decimal space-y-1.5 pl-1">
        <li>
          Open{" "}
          <a
            href="https://t.me/BotFather"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 text-[var(--svx-color-brand-primary)] hover:underline"
          >
            @BotFather
            <ExternalLinkIcon className="size-2.5" />
          </a>{" "}
          on Telegram
        </li>
        <li>
          Send <CopyCode text="/newbot" /> and follow the prompts
        </li>
        <li>
          Copy the bot token and set the env var:
          <div className="mt-1">
            <CopyCode text="SOVYX_TELEGRAM_TOKEN=your_token_here" />
          </div>
        </li>
        <li>
          Restart Sovyx: <CopyCode text="sovyx restart" />
        </li>
      </ol>
      <p className="text-[10px] text-[var(--svx-color-text-disabled)]">
        Tip: add allowed_users in mind.yaml to restrict who can talk to your Mind.
      </p>
    </div>
  );
}

function SignalSetupGuide() {
  return (
    <div className="mt-2 space-y-2 text-xs text-[var(--svx-color-text-secondary)]">
      <p className="font-medium text-[var(--svx-color-text-primary)]">Connect Signal</p>
      <ol className="list-inside list-decimal space-y-1.5 pl-1">
        <li>
          Run the{" "}
          <a
            href="https://github.com/bbernhard/signal-cli-rest-api"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 text-[var(--svx-color-brand-primary)] hover:underline"
          >
            signal-cli-rest-api
            <ExternalLinkIcon className="size-2.5" />
          </a>{" "}
          Docker container
        </li>
        <li>Register or link a phone number in signal-cli</li>
        <li>
          Configure in <CopyCode text="mind.yaml" />:
          <div className="mt-1 rounded bg-[var(--svx-color-bg-elevated)] p-2 text-[11px] leading-relaxed">
            <div>channels:</div>
            <div className="pl-3">signal:</div>
            <div className="pl-6">phone: &quot;+1234567890&quot;</div>
            <div className="pl-6">api_url: &quot;http://localhost:8080&quot;</div>
          </div>
        </li>
        <li>
          Restart Sovyx: <CopyCode text="sovyx restart" />
        </li>
      </ol>
    </div>
  );
}

const SETUP_GUIDES: Record<string, React.FC> = {
  telegram: TelegramSetupGuide,
  signal: SignalSetupGuide,
};

export function ChannelStatusCard() {
  const { t } = useTranslation("overview");
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);

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

  const toggle = (type: string) => {
    setExpanded((prev) => (prev === type ? null : type));
  };

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
          const GuideComponent = !ch.connected ? SETUP_GUIDES[ch.type] : undefined;
          const isExpanded = expanded === ch.type;

          return (
            <div key={ch.type} data-testid={`channel-${ch.type}`}>
              {/* Channel row */}
              <div
                className={cn(
                  "flex items-center justify-between rounded-md px-2 py-1.5 text-sm",
                  !ch.connected && GuideComponent && "cursor-pointer hover:bg-[var(--svx-color-bg-hover)]",
                )}
                role={!ch.connected && GuideComponent ? "button" : undefined}
                tabIndex={!ch.connected && GuideComponent ? 0 : undefined}
                onClick={() => !ch.connected && GuideComponent && toggle(ch.type)}
                onKeyDown={(e) => {
                  if (!ch.connected && GuideComponent && (e.key === "Enter" || e.key === " ")) {
                    e.preventDefault();
                    toggle(ch.type);
                  }
                }}
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
                      {t("channels.setupGuide", { defaultValue: "Setup guide" })}
                      {GuideComponent && (
                        isExpanded
                          ? <ChevronUpIcon className="size-3" />
                          : <ChevronDownIcon className="size-3" />
                      )}
                    </>
                  )}
                </span>
              </div>

              {/* Expandable setup guide */}
              {!ch.connected && GuideComponent && isExpanded && (
                <div className="mx-2 mb-2 rounded-md border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-3">
                  <GuideComponent />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
