/**
 * _FallbackBanner -- terminal-state FALLBACK render.
 *
 * Replaces the inline "fallback" branch of the prior monolithic
 * TerminalView. Surfaces the structured fallback reason from the
 * orchestrator + the explicit "Use simple setup" CTA pointing at
 * the legacy device-test wizard.
 *
 * Subcomponent of VoiceCalibrationStep per spec §6.3 (T3.4 split).
 * History: introduced in v0.30.25.
 */

import { useTranslation } from "react-i18next";
import { AlertCircleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

interface FallbackBannerProps {
  fallbackReason: string | null;
  onFallback: () => void;
}

export function FallbackBanner({ fallbackReason, onFallback }: FallbackBannerProps) {
  const { t } = useTranslation("voice");
  return (
    <div className="space-y-4" data-testid="voice-calibration-fallback-banner">
      <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
        <AlertCircleIcon className="size-5 flex-shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">{t("calibration.terminal.fallback.title")}</p>
          <p className="text-xs">
            {t("calibration.terminal.fallback.subtitle", {
              reason: fallbackReason ?? "—",
            })}
          </p>
        </div>
      </div>
      <Button onClick={onFallback} size="lg" variant="outline">
        {t("calibration.button.use_simple_setup")}
      </Button>
    </div>
  );
}
