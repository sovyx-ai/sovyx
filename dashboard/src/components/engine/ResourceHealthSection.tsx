/* Mission H4 §T3.4 — ResourceHealthSection widget.
 *
 * Operator-facing surface for the per-cohort instrumentation introduced
 * by Phase 1.A (SSoT) + Phase 1.B (snapshotter wire). Polls
 * /api/engine/resources every 30 s with exponential backoff on 5xx +
 * renders collapsible rows per cohort section (process / asyncio /
 * to_thread / lock_dict / onnx / gc / tracemalloc / exception_cohort).
 *
 * Mounts inside ``voice-health.tsx`` alongside the QuarantineSection
 * + FailoverHistorySection so operators have one place to see the
 * engine's runtime resource state.
 *
 * Mirrors the C3 FailoverHistorySection pattern — uses ``useApiPoller``
 * for backoff + ``isDegraded`` indicator on poller failure.
 */

import { ChevronDownIcon, ChevronRightIcon, Loader2Icon } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useApiPoller } from "@/hooks/use-api-poller";
import { EngineResourcesResponseSchema } from "@/types/schemas";

const POLL_INTERVAL_MS = 30_000;

// Section order = canonical FieldSpec.section ordering used by the
// Python SSoT _HEALTH_SNAPSHOT_FIELDS mapping.
const SECTION_ORDER = [
  "process",
  "asyncio",
  "to_thread",
  "lock_dict",
  "onnx",
  "gc",
  "tracemalloc",
  "exception_cohort",
] as const;

// Map cohort sections → the field keys they own (mirrors
// _HEALTH_SNAPSHOT_FIELDS[k].section grouping).
const SECTION_FIELDS: Record<(typeof SECTION_ORDER)[number], readonly string[]> = {
  process: [
    "process.rss_bytes",
    "process.vms_bytes",
    "process.cpu_percent",
    "process.num_threads",
    "process.num_handles_or_fds",
    "process.open_files_count",
    "process.connections_count",
  ],
  asyncio: [
    "asyncio.task_count",
    "asyncio.running_count",
    "asyncio.pending_count",
  ],
  to_thread: [
    "to_thread.pool_size",
    "to_thread.queue_depth",
    "to_thread.max_workers",
    "to_thread.dispatch_count_total",
    "to_thread.dispatch_count_per_label",
  ],
  lock_dict: [
    "lock_dict.total_cardinality",
    "lock_dict.per_owner",
    "lock_dict.instance_count",
  ],
  onnx: ["onnx.session_count", "onnx.session_labels"],
  gc: ["gc.collections_by_gen", "gc.objects_count"],
  tracemalloc: [
    "tracemalloc.is_tracing",
    "tracemalloc.current_kb",
    "tracemalloc.peak_kb",
  ],
  exception_cohort: [
    "exception_cohort.retained_bytes_estimate",
    "exception_cohort.distinct_group_id_count",
    "exception_cohort.last_observation_monotonic",
  ],
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "number") {
    return value.toLocaleString();
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return "[]";
    }
    return `[${value.slice(0, 5).map(String).join(", ")}${value.length > 5 ? ", …" : ""}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) {
      return "{}";
    }
    return `{${entries
      .slice(0, 3)
      .map(([k, v]) => `${k}: ${String(v)}`)
      .join(", ")}${entries.length > 3 ? ", …" : ""}}`;
  }
  return String(value);
}

interface SectionRowProps {
  section: (typeof SECTION_ORDER)[number];
  cohorts: Record<string, unknown>;
}

function SectionRow({ section, cohorts }: SectionRowProps) {
  const { t } = useTranslation("voice");
  const [open, setOpen] = useState(false);
  const fields = SECTION_FIELDS[section];
  const presentFields = fields.filter((f) => f in cohorts);

  return (
    <div
      className="rounded border border-[var(--svx-color-border)] bg-[var(--svx-color-surface)]"
      data-testid={`resource-section-${section}`}
    >
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="flex w-full items-center justify-between px-3 py-2 text-left transition-colors hover:bg-[var(--svx-color-surface-hover)]"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2 font-mono text-xs">
          {open ? (
            <ChevronDownIcon className="size-3" />
          ) : (
            <ChevronRightIcon className="size-3" />
          )}
          {t(`resources.sections.${section}.title`)}
        </span>
        <span className="font-mono text-[10px] text-[var(--svx-color-text-tertiary)]">
          {presentFields.length} {t("resources.fieldsLabel")}
        </span>
      </button>
      {open && (
        <div className="border-t border-[var(--svx-color-border)] px-3 py-2">
          <p className="mb-2 text-[11px] text-[var(--svx-color-text-tertiary)]">
            {t(`resources.sections.${section}.description`)}
          </p>
          <dl className="space-y-1">
            {presentFields.map((field) => (
              <div
                key={field}
                className="flex items-baseline justify-between gap-3 text-xs"
                data-testid={`resource-field-${field}`}
              >
                <dt className="font-mono text-[var(--svx-color-text-secondary)]">
                  {field}
                </dt>
                <dd className="font-mono text-[var(--svx-color-text-primary)] break-all text-right">
                  {formatValue(cohorts[field])}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </div>
  );
}

export function ResourceHealthSection() {
  const { t } = useTranslation("voice");

  const { data: snapshot, error: pollerError } = useApiPoller<
    typeof EngineResourcesResponseSchema,
    import("zod").z.infer<typeof EngineResourcesResponseSchema>
  >({
    endpoint: "/api/engine/resources",
    schema: EngineResourcesResponseSchema,
    baselineIntervalMs: POLL_INTERVAL_MS,
    enabled: true,
    warnTag: "engine.resources.poller.degraded",
  });

  const cohorts = snapshot?.cohorts ?? {};
  const observedAt = snapshot?.observed_at_unix;
  const isDegraded = pollerError === "degraded";

  const observedAtLabel = useMemo(() => {
    if (typeof observedAt !== "number") return "—";
    return new Date(observedAt * 1000).toLocaleTimeString();
  }, [observedAt]);

  return (
    <section
      aria-labelledby="resource-health-heading"
      className="space-y-3"
      data-testid="resource-health-section"
    >
      <div className="flex items-baseline justify-between">
        <h2
          id="resource-health-heading"
          className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
        >
          {t("resources.title")}
        </h2>
        <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
          {observedAtLabel}
        </span>
      </div>
      <p className="text-xs text-[var(--svx-color-text-tertiary)]">
        {t("resources.subtitle")}
      </p>
      {!snapshot && !isDegraded && (
        <div
          className="flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]"
          data-testid="resource-health-loading"
        >
          <Loader2Icon className="size-3.5 animate-spin" />
          {t("resources.loading")}
        </div>
      )}
      {isDegraded && (
        <div
          className="rounded border border-[var(--svx-color-warning-border)] bg-[var(--svx-color-warning-bg)] px-3 py-2 text-xs text-[var(--svx-color-warning-text)]"
          data-testid="resource-health-degraded"
        >
          {t("resources.degraded")}
        </div>
      )}
      {snapshot && (
        <div className="space-y-2" data-testid="resource-health-sections">
          {SECTION_ORDER.map((section) => (
            <SectionRow key={section} section={section} cohorts={cohorts} />
          ))}
        </div>
      )}
    </section>
  );
}
