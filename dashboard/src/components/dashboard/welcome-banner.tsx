/**
 * Welcome banner — first-time user guidance.
 *
 * Shown on Overview when the engine has no conversations/concepts yet.
 * Guides through: LLM key → Chat → Watch growth.
 *
 * DASH-08: Welcome screen for dashboard synergy mission.
 */

import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import {
  KeyIcon,
  MessageCircleIcon,
  SparklesIcon,
  ArrowRightIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";

interface StepProps {
  step: number;
  icon: React.ReactNode;
  title: string;
  description: string;
  action?: React.ReactNode;
}

function WelcomeStep({ step, icon, title, description, action }: StepProps) {
  return (
    <div className="flex gap-4" data-testid={`welcome-step-${step}`}>
      <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-[var(--svx-color-brand-subtle)] text-[var(--svx-color-brand-primary)]">
        {icon}
      </div>
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
            Step {step}
          </span>
        </div>
        <h3 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
          {title}
        </h3>
        <p className="text-xs text-[var(--svx-color-text-secondary)]">
          {description}
        </p>
        {action && <div className="pt-1">{action}</div>}
      </div>
    </div>
  );
}

export function WelcomeBanner() {
  const { t } = useTranslation("overview");

  return (
    <div
      className="rounded-2xl border border-[var(--svx-color-border-subtle)] bg-gradient-to-br from-[var(--svx-color-bg-elevated)] to-[var(--svx-color-bg-surface)] p-6"
      data-testid="welcome-banner"
    >
      <div className="mb-6">
        <h2 className="text-xl font-bold text-[var(--svx-color-text-primary)]">
          {t("welcome.title", { defaultValue: "Welcome to Sovyx" })}
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          {t("welcome.subtitle", {
            defaultValue:
              "Your mind engine is ready. Follow these steps to get started.",
          })}
        </p>
      </div>

      <div className="space-y-5">
        <WelcomeStep
          step={1}
          icon={<KeyIcon className="size-5" />}
          title={t("welcome.step1Title", {
            defaultValue: "Configure your LLM key",
          })}
          description={t("welcome.step1Desc", {
            defaultValue:
              "Add your OpenAI, Anthropic, or other LLM API key in Settings.",
          })}
          action={
            <Link to="/settings">
              <Button variant="outline" size="sm" className="gap-1.5 text-xs">
                {t("welcome.goSettings", { defaultValue: "Go to Settings" })}
                <ArrowRightIcon className="size-3" />
              </Button>
            </Link>
          }
        />

        <WelcomeStep
          step={2}
          icon={<MessageCircleIcon className="size-5" />}
          title={t("welcome.step2Title", {
            defaultValue: "Send your first message",
          })}
          description={t("welcome.step2Desc", {
            defaultValue:
              "Open Chat and start a conversation with your mind. It learns from every interaction.",
          })}
          action={
            <Link to="/chat">
              <Button size="sm" className="gap-1.5 text-xs bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)] hover:bg-[var(--svx-color-brand-hover)]">
                {t("welcome.goChat", { defaultValue: "Open Chat" })}
                <ArrowRightIcon className="size-3" />
              </Button>
            </Link>
          }
        />

        <WelcomeStep
          step={3}
          icon={<SparklesIcon className="size-5" />}
          title={t("welcome.step3Title", {
            defaultValue: "Watch your mind grow",
          })}
          description={t("welcome.step3Desc", {
            defaultValue:
              "As you chat, your mind builds concepts, forms memories, and develops its personality.",
          })}
        />
      </div>
    </div>
  );
}
