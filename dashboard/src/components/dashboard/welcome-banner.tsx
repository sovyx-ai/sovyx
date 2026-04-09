/**
 * Welcome banner — progressive onboarding guide.
 *
 * Steps reflect live engine state (LLM configured → first message → mind growing).
 * Visual states: pending (dimmed), active (highlighted + action), done (check + collapsed).
 * Progress bar shows completion (0/3 → 3/3).
 * Dismiss button lets users hide the guide at any time.
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
  CheckIcon,
  XIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { StepState } from "@/hooks/use-onboarding";

// ── Step Component ──

interface StepProps {
  step: number;
  state: StepState;
  icon: React.ReactNode;
  title: string;
  description: string;
  action?: React.ReactNode;
}

function WelcomeStep({ step, state, icon, title, description, action }: StepProps) {
  const { t } = useTranslation("overview");

  const isActive = state === "active";
  const isDone = state === "done";

  return (
    <div
      className={`flex gap-4 transition-all duration-[var(--svx-duration-slow)] ease-[var(--svx-ease-out)] ${
        isDone ? "max-h-14 opacity-70" : "max-h-48 opacity-100"
      }`}
      data-testid={`welcome-step-${step}`}
      data-state={state}
      aria-label={
        isDone
          ? `${t(`welcome.step${step}Title`, { defaultValue: title })} — ${t("welcome.stepDone", { defaultValue: "Done" })}`
          : undefined
      }
      aria-current={isActive ? "step" : undefined}
    >
      {/* Icon */}
      <div
        className={`flex size-10 shrink-0 items-center justify-center rounded-xl transition-all duration-[var(--svx-duration-slow)] ease-[var(--svx-ease-out)] ${
          isDone
            ? "bg-[var(--svx-color-success-subtle)] text-[var(--svx-color-success)]"
            : isActive
              ? "bg-[var(--svx-color-brand-subtle)] text-[var(--svx-color-brand-primary)] ring-2 ring-[var(--svx-color-brand-primary)]/30 animate-[pulse-ring_2s_ease-in-out_infinite]"
              : "bg-[var(--svx-color-brand-subtle)] text-[var(--svx-color-brand-primary)] opacity-60"
        }`}
      >
        {isDone ? <CheckIcon className="size-5" /> : icon}
      </div>

      {/* Content */}
      <div className="min-w-0 space-y-1">
        <div className="flex items-center gap-2">
          <span
            className={`text-xs font-medium transition-colors duration-[var(--svx-duration-normal)] ${
              isDone
                ? "text-[var(--svx-color-success)]"
                : isActive
                  ? "text-[var(--svx-color-brand-primary)]"
                  : "text-[var(--svx-color-text-secondary)]"
            }`}
          >
            Step {step}
          </span>
          {isDone && (
            <Badge variant="outline" className="border-[var(--svx-color-success)]/30 text-[var(--svx-color-success)] text-[10px] px-1.5 py-0">
              {t("welcome.stepDone", { defaultValue: "Done" })}
            </Badge>
          )}
        </div>

        <h3
          className={`text-sm transition-all duration-[var(--svx-duration-normal)] ${
            isDone
              ? "font-medium text-[var(--svx-color-text-primary)] line-through decoration-[var(--svx-color-text-secondary)]/30"
              : isActive
                ? "font-semibold text-[var(--svx-color-text-primary)]"
                : "font-medium text-[var(--svx-color-text-secondary)]"
          }`}
        >
          {title}
        </h3>

        {/* Description — hidden when done (collapse animation) */}
        <div
          className={`overflow-hidden transition-all duration-[var(--svx-duration-slow)] ease-[var(--svx-ease-out)] ${
            isDone ? "max-h-0 opacity-0" : "max-h-20 opacity-100"
          }`}
        >
          <p
            className={`text-xs ${
              isActive
                ? "text-[var(--svx-color-text-secondary)]"
                : "text-[var(--svx-color-text-secondary)] opacity-60"
            }`}
          >
            {description}
          </p>
        </div>

        {/* Action — only visible when active */}
        {isActive && action && <div className="pt-1">{action}</div>}
      </div>
    </div>
  );
}

// ── Progress Bar ──

interface ProgressBarProps {
  completed: number;
  total: number;
}

function ProgressBar({ completed, total }: ProgressBarProps) {
  const { t } = useTranslation("overview");
  const allDone = completed === total;
  const percentage = total > 0 ? (completed / total) * 100 : 0;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div
          className="h-2 flex-1 overflow-hidden rounded-full bg-[var(--svx-color-bg-elevated)]"
          role="progressbar"
          aria-valuenow={completed}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label={t("welcome.progress", {
            completed,
            total,
            defaultValue: `${completed} of ${total}`,
          })}
        >
          <div
            className={`h-full rounded-full bg-[var(--svx-color-brand-primary)] transition-[width] duration-500 ease-[var(--svx-ease-out)] ${
              allDone ? "animate-[progress-flash_600ms_ease-in-out_1]" : ""
            }`}
            style={{ width: `${percentage}%` }}
          />
        </div>
        <span className="ml-3 shrink-0 text-xs text-[var(--svx-color-text-secondary)]">
          {allDone
            ? t("welcome.progressDone", { defaultValue: "All done ✓" })
            : t("welcome.progress", {
                completed,
                total,
                defaultValue: `${completed} of ${total}`,
              })}
        </span>
      </div>
    </div>
  );
}

// ── Main Banner ──

interface WelcomeBannerProps {
  step1: StepState;
  step2: StepState;
  step3: StepState;
  completedCount: number;
  onDismiss: () => void;
}

export function WelcomeBanner({
  step1,
  step2,
  step3,
  completedCount,
  onDismiss,
}: WelcomeBannerProps) {
  const { t } = useTranslation("overview");

  return (
    <div
      className="relative rounded-2xl border border-[var(--svx-color-border-subtle)] bg-gradient-to-br from-[var(--svx-color-bg-elevated)] to-[var(--svx-color-bg-surface)] p-6"
      data-testid="welcome-banner"
    >
      {/* Dismiss button */}
      <button
        onClick={onDismiss}
        className="absolute right-4 top-4 rounded-lg p-1 text-[var(--svx-color-text-secondary)] transition-colors duration-[var(--svx-duration-fast)] hover:bg-[var(--svx-color-bg-elevated)] hover:text-[var(--svx-color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--svx-color-brand-primary)]/50"
        aria-label={t("welcome.dismissLabel", { defaultValue: "Dismiss setup guide" })}
        data-testid="welcome-dismiss"
      >
        <XIcon className="size-4" />
      </button>

      {/* Header */}
      <div className="mb-4 pr-8">
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

      {/* Progress bar */}
      <div className="mb-5">
        <ProgressBar completed={completedCount} total={3} />
      </div>

      {/* Steps */}
      <div className="space-y-5">
        <WelcomeStep
          step={1}
          state={step1}
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
          state={step2}
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
              <Button
                size="sm"
                className="gap-1.5 text-xs bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)] hover:bg-[var(--svx-color-brand-hover)]"
              >
                {t("welcome.goChat", { defaultValue: "Open Chat" })}
                <ArrowRightIcon className="size-3" />
              </Button>
            </Link>
          }
        />

        <WelcomeStep
          step={3}
          state={step3}
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
