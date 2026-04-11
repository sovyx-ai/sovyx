/**
 * Plugins page — plugin management dashboard.
 *
 * Lists installed plugins with status, health, tools, and permissions.
 * Supports search, filtering, sorting, and detail view.
 *
 * TASK-453: Route + skeleton + empty page shell.
 * TASK-454+: PluginCard, grid, filters, detail panel.
 */

import { useTranslation } from "react-i18next";

export default function PluginsPage() {
  const { t } = useTranslation("plugins");

  return (
    <div className="space-y-6 animate-page-in">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-[var(--svx-color-text-primary)]">
          {t("title")}
        </h1>
        <p className="text-sm text-[var(--svx-color-text-secondary)]">
          {t("subtitle")}
        </p>
      </div>

      {/* Content will be built in TASK-454+ */}
      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-8 text-center">
        <p className="text-sm text-[var(--svx-color-text-tertiary)]">
          {t("empty.subtitle")}
        </p>
      </div>
    </div>
  );
}
