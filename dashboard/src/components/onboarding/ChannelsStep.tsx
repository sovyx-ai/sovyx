import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LoaderIcon,
  CheckCircle2Icon,
  XCircleIcon,
  ExternalLinkIcon,
  SendIcon,
  MessageSquareIcon,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";

interface ChannelsStepProps {
  mindName: string;
  onConfigured: () => void;
  onSkip: () => void;
}

export function ChannelsStep({ mindName, onConfigured, onSkip }: ChannelsStepProps) {
  const { t } = useTranslation("onboarding");
  const [token, setToken] = useState("");
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean;
    message: string;
    botName?: string;
    hotStarted?: boolean;
  } | null>(null);

  const handleConnect = useCallback(async () => {
    if (!token.trim()) return;
    setTesting(true);
    setResult(null);
    try {
      const resp = await api.post<{
        ok: boolean;
        bot_username: string;
        bot_name: string;
        hot_started: boolean;
      }>("/api/onboarding/channel/telegram", { token: token.trim() });

      if (resp.ok) {
        setResult({
          ok: true,
          message: t("channels.telegramConnectedTo", {
            username: resp.bot_username,
          }),
          botName: resp.bot_name,
          hotStarted: resp.hot_started,
        });
      }
    } catch (err) {
      let msg = t("channels.telegramConnectionFailed");
      if (err instanceof ApiError) {
        try {
          const body = JSON.parse(err.message) as { error?: string };
          msg = body.error ?? msg;
        } catch {
          msg = err.message;
        }
      }
      setResult({ ok: false, message: msg });
    } finally {
      setTesting(false);
    }
  }, [token, t]);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
          {t("channels.title")}
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          {t("channels.subtitle")}
        </p>
      </div>

      {/* Telegram card */}
      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-5 space-y-4">
        <div className="flex items-center gap-2">
          <SendIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
          <h3 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {t("channels.telegramTitle")}
          </h3>
        </div>
        <p className="text-xs text-[var(--svx-color-text-secondary)]">
          {t("channels.telegramInstructionsLead", { mindName })}
          {" "}
          <a
            href="https://t.me/BotFather"
            target="_blank"
            rel="noopener noreferrer"
            className="text-[var(--svx-color-brand-primary)] hover:underline"
          >
            @BotFather
            <ExternalLinkIcon className="ml-0.5 inline size-3" />
          </a>
          {" "}{t("channels.telegramInstructionsTail")}
        </p>

        <div>
          <label className="mb-1 block text-xs font-medium text-[var(--svx-color-text-secondary)]">
            {t("channels.telegramTokenLabel")}
          </label>
          <input
            type="password"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              setResult(null);
            }}
            placeholder={t("channels.telegramTokenPlaceholder")}
            className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 font-mono text-sm text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)]"
          />
        </div>

        {result && (
          <div
            className={`flex items-center gap-2 rounded-[var(--svx-radius-md)] px-3 py-2 text-xs ${
              result.ok
                ? "bg-[var(--svx-color-success)]/10 text-[var(--svx-color-success)]"
                : "bg-[var(--svx-color-error)]/10 text-[var(--svx-color-error)]"
            }`}
          >
            {result.ok ? (
              <CheckCircle2Icon className="size-3.5 shrink-0" />
            ) : (
              <XCircleIcon className="size-3.5 shrink-0" />
            )}
            <span>
              {result.message}
              {result.ok && result.hotStarted && t("channels.telegramConnectedSuffixActive")}
              {result.ok && !result.hotStarted && t("channels.telegramConnectedSuffixDeferred")}
            </span>
          </div>
        )}

        {!result?.ok && (
          <Button
            onClick={handleConnect}
            disabled={!token.trim() || testing}
            size="sm"
          >
            {testing && <LoaderIcon className="mr-1.5 size-3.5 animate-spin" />}
            {testing ? t("channels.telegramConnectingButton") : t("channels.telegramConnectButton")}
          </Button>
        )}
      </div>

      {/* Signal card (coming soon) */}
      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)]/60 bg-[var(--svx-color-bg-surface)]/60 p-5 opacity-50">
        <div className="flex items-center gap-2">
          <MessageSquareIcon className="size-4 text-[var(--svx-color-text-tertiary)]" />
          <h3 className="text-sm font-semibold text-[var(--svx-color-text-tertiary)]">
            {t("channels.signalTitle")}
          </h3>
          <span className="rounded-full bg-[var(--svx-color-bg-elevated)] px-2 py-0.5 text-[10px] text-[var(--svx-color-text-tertiary)]">
            {t("channels.signalComingSoon")}
          </span>
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onSkip}
          className="text-xs text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-secondary)]"
        >
          {t("channels.skipForNow")}
        </button>
        <Button onClick={onConfigured}>
          {t("channels.continueButton")}
        </Button>
      </div>
    </div>
  );
}
