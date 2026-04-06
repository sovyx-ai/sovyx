/**
 * About page — version, system info, links.
 *
 * POLISH-06: Replaced shadcn Card wrappers with --svx-* token divs.
 * FINAL-03: Full i18n migration — zero hardcoded English.
 *
 * Ref: Architecture §3.6
 */

import { useTranslation } from "react-i18next";
import { ExternalLinkIcon, ShieldIcon, CodeIcon, HeartIcon } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { formatUptime } from "@/lib/format";

export default function AboutPage() {
  const { t } = useTranslation("about");
  const status = useDashboardStore((s) => s.status);

  const links = [
    { label: t("github"), url: "https://github.com/sovyx-ai/sovyx", icon: CodeIcon },
    { label: t("documentation"), url: "https://docs.sovyx.ai", icon: ExternalLinkIcon },
  ] as const;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div className="text-center">
        <h1 className="text-3xl font-bold">🔮 {t("title")}</h1>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          {t("tagline")}
        </p>
      </div>

      {/* Version + License */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="flex items-center gap-2 text-sm font-medium text-[var(--svx-color-text-primary)]">
          <ShieldIcon className="size-4" />
          {t("versionLicense")}
        </h2>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">{t("version")}</dt>
            <dd className="font-code font-medium">v{status?.version ?? "0.1.0"}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">{t("license")}</dt>
            <dd>
              <span className="inline-flex rounded-[var(--svx-radius-full)] bg-[var(--svx-color-bg-elevated)] px-2 py-0.5 font-code text-[10px]">
                {t("licenseValue")}
              </span>
            </dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">{t("dashboard")}</dt>
            <dd className="font-code text-xs">{t("dashboardValue", { version: __REACT_VERSION__ })}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">{t("engineUptime")}</dt>
            <dd className="font-code text-xs">
              {status ? formatUptime(status.uptime_seconds) : "—"}
            </dd>
          </div>
        </dl>
      </section>

      {/* System Info */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">{t("system")}</h2>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">{t("mind")}</dt>
            <dd className="font-medium">{status?.mind_name ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">{t("concepts")}</dt>
            <dd className="font-code text-xs">{status?.memory_concepts?.toLocaleString() ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">{t("episodes")}</dt>
            <dd className="font-code text-xs">{status?.memory_episodes?.toLocaleString() ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase text-[var(--svx-color-text-tertiary)]">{t("llmCallsToday")}</dt>
            <dd className="font-code text-xs">{status?.llm_calls_today?.toLocaleString() ?? "—"}</dd>
          </div>
        </dl>
      </section>

      {/* Links */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">{t("links")}</h2>
        <div className="mt-3 flex flex-wrap gap-3">
          {links.map((link) => (
            <a
              key={link.url}
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)] px-3 py-1.5 text-xs font-medium text-[var(--svx-color-text-secondary)] transition-colors hover:bg-[var(--svx-color-bg-hover)] hover:text-[var(--svx-color-text-primary)]"
            >
              <link.icon className="size-3.5" />
              {link.label}
            </a>
          ))}
        </div>
      </section>

      {/* Footer */}
      <div className="text-center">
        <p className="flex items-center justify-center gap-1 text-xs text-[var(--svx-color-text-secondary)]">
          {t("builtWith")} <HeartIcon className="size-3 text-[var(--svx-color-error)]" /> {t("builtBy")}
        </p>
        <p className="mt-1 text-[10px] text-[var(--svx-color-text-disabled)]">
          {t("sovereignty")}
        </p>
      </div>
    </div>
  );
}


