/**
 * ComingSoon — Reusable placeholder for pages under development.
 *
 * Displays a centered card with icon, title, description, and a
 * "Coming Soon" badge. Used by Emotions, Productivity, and any
 * future pages that are in the roadmap but not yet implemented.
 *
 * Full i18n — zero hardcoded English strings.
 */

import { useTranslation } from "react-i18next";
import type { LucideIcon } from "lucide-react";
import { ClockIcon } from "lucide-react";

interface ComingSoonProps {
  icon: LucideIcon;
  titleKey: string;
  descriptionKey: string;
}

export function ComingSoon({ icon: Icon, titleKey, descriptionKey }: ComingSoonProps) {
  const { t } = useTranslation("common");

  return (
    <div className="flex min-h-[400px] items-center justify-center">
      <div className="max-w-md space-y-6 text-center">
        {/* Icon */}
        <div className="mx-auto flex size-16 items-center justify-center rounded-2xl border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)]">
          <Icon className="size-8 text-[var(--svx-color-accent)]" />
        </div>

        {/* Title */}
        <h1 className="text-2xl font-bold text-[var(--svx-color-text-primary)]">
          {t(titleKey)}
        </h1>

        {/* Badge */}
        <div className="mx-auto flex w-fit items-center gap-1.5 rounded-full border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] px-3 py-1">
          <ClockIcon className="size-3.5 text-[var(--svx-color-text-tertiary)]" />
          <span className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
            {t("comingSoon.badge")}
          </span>
        </div>

        {/* Description */}
        <p className="text-sm leading-relaxed text-[var(--svx-color-text-secondary)]">
          {t(descriptionKey)}
        </p>
      </div>
    </div>
  );
}
