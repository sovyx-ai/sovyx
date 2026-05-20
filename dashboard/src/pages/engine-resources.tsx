/**
 * Mission H4 §4.8 ADR-D8 + v0.49.25 — Engine Resources page.
 *
 * Dedicated route at /engine/resources hosting the H4 widgets:
 * - ResourceHealthSection (live per-cohort snapshot)
 * - Section anchors for #lock-dicts / #onnx / #exception-cohort /
 *   #threads / #heap referenced by the ADR-D8 per-cohort chip
 *   mapping (see _resource_cohort_governor._chips_for_reason).
 *
 * Pre-v0.49.25 the widget was tucked inside /voice/health which
 * forced the cohort chips to deep-link a route that did not exist
 * (operator click → SPA 404). v0.49.25 promotes the widget to its
 * own route so the chip URLs resolve correctly.
 *
 * The widget itself remains mounted (additionally) on /voice/health
 * for backward compatibility with the v0.49.20..v0.49.24 location.
 */

import { useTranslation } from "react-i18next";

import { ResourceHealthSection } from "@/components/engine/ResourceHealthSection";

export default function EngineResourcesPage() {
  const { t } = useTranslation("voice");

  return (
    <div
      className="space-y-6 p-4 md:p-6"
      data-testid="engine-resources-page"
    >
      <header className="space-y-1">
        <h1 className="text-2xl font-bold text-[var(--svx-color-text-primary)]">
          {t("resources.title")}
        </h1>
        <p className="text-sm text-[var(--svx-color-text-tertiary)]">
          {t("resources.subtitle")}
        </p>
      </header>

      <ResourceHealthSection />

      {/*
        ADR-D8 cohort-chip anchors. These empty divs exist so the
        per-cohort chip targets (e.g. `/engine/resources#lock-dicts`)
        scroll-to a stable DOM hash even when the section auto-
        collapses inside ResourceHealthSection's collapsible rows.
        The anchors live BELOW the widget so the URL fragment lands
        the operator near the data they wanted.
      */}
      <div className="space-y-2">
        <div id="heap" data-testid="anchor-heap" aria-hidden="true" />
        <div id="threads" data-testid="anchor-threads" aria-hidden="true" />
        <div id="lock-dicts" data-testid="anchor-lock-dicts" aria-hidden="true" />
        <div id="onnx" data-testid="anchor-onnx" aria-hidden="true" />
        <div
          id="exception-cohort"
          data-testid="anchor-exception-cohort"
          aria-hidden="true"
        />
      </div>
    </div>
  );
}
