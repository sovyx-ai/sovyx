/**
 * LogFilterBar — left-pane filter sidebar for the Logs page.
 *
 * Owns its own *input* state for the free-text fields (search, logger)
 * so the user can type without round-tripping every keystroke through
 * the URL search params. Changes propagate to the parent via
 * ``onChange`` after a 300 ms debounce. Single-action selectors
 * (level, datetime pickers, saga_id) propagate immediately — no
 * debounce, no surprise.
 *
 * Props are intentionally narrow so the host page can drive this
 * component from URL state, Zustand, or anywhere else.
 *
 * Aligned with IMPL-OBSERVABILITY-001 §16 Task 10.8.
 */

import {
  memo,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { SearchIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];

export interface LogFilterState {
  q: string;
  level: LogLevel | null;
  logger: string;
  saga_id: string;
  since: string;
  until: string;
}

const LEVEL_COLORS: Record<LogLevel, string> = {
  DEBUG: "text-[var(--svx-color-text-tertiary)]",
  INFO: "text-[var(--svx-color-success)]",
  WARNING: "text-[var(--svx-color-warning)]",
  ERROR: "text-[var(--svx-color-error)]",
  CRITICAL: "text-[var(--svx-color-error)]",
};

const DEBOUNCE_MS = 300;

interface LogFilterBarProps {
  filters: LogFilterState;
  onChange: (patch: Partial<LogFilterState>) => void;
  onReset: () => void;
  /** Loggers seen in the current result set — used for autocomplete. */
  knownLoggers?: string[];
  className?: string;
}

function LogFilterBarImpl({
  filters,
  onChange,
  onReset,
  knownLoggers = [],
  className,
}: LogFilterBarProps) {
  const { t } = useTranslation(["logs"]);
  const datalistId = useId();

  // Local mirror so the input is responsive while the URL/Zustand
  // update is debounced. Re-syncs whenever the canonical filter
  // changes from the outside (e.g. browser back button, reset).
  const [localQ, setLocalQ] = useState(filters.q);
  const [localLogger, setLocalLogger] = useState(filters.logger);

  useEffect(() => setLocalQ(filters.q), [filters.q]);
  useEffect(() => setLocalLogger(filters.logger), [filters.logger]);

  // Debounced propagation. We keep one timer per field so a slow
  // typer in the search box doesn't drop a still-pending logger
  // edit, and vice versa.
  const qTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loggerTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (qTimerRef.current) clearTimeout(qTimerRef.current);
      if (loggerTimerRef.current) clearTimeout(loggerTimerRef.current);
    },
    [],
  );

  const handleQChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const value = e.target.value;
      setLocalQ(value);
      if (qTimerRef.current) clearTimeout(qTimerRef.current);
      qTimerRef.current = setTimeout(() => onChange({ q: value }), DEBOUNCE_MS);
    },
    [onChange],
  );

  const handleLoggerChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const value = e.target.value;
      setLocalLogger(value);
      if (loggerTimerRef.current) clearTimeout(loggerTimerRef.current);
      loggerTimerRef.current = setTimeout(
        () => onChange({ logger: value }),
        DEBOUNCE_MS,
      );
    },
    [onChange],
  );

  const handleLevelClick = useCallback(
    (level: LogLevel | null) => {
      onChange({ level: filters.level === level ? null : level });
    },
    [filters.level, onChange],
  );

  const handleSagaChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => onChange({ saga_id: e.target.value }),
    [onChange],
  );
  const handleSinceChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => onChange({ since: e.target.value }),
    [onChange],
  );
  const handleUntilChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => onChange({ until: e.target.value }),
    [onChange],
  );

  // Deduplicate logger suggestions; cap at 50 so the datalist stays
  // snappy on noisy systems.
  const suggestions = useMemo(() => {
    if (knownLoggers.length === 0) return [];
    const unique = Array.from(new Set(knownLoggers)).sort();
    return unique.slice(0, 50);
  }, [knownLoggers]);

  return (
    <aside
      className={cn(
        "flex w-72 shrink-0 flex-col gap-3 overflow-y-auto rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-3",
        className,
      )}
    >
      <div>
        <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
          {t("filters.search")}
        </label>
        <div className="relative mt-1">
          <SearchIcon className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-[var(--svx-color-text-secondary)]" />
          <Input
            value={localQ}
            onChange={handleQChange}
            placeholder={t("filters.searchPlaceholder")}
            className="h-8 pl-7 text-xs"
          />
        </div>
      </div>

      <div>
        <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
          {t("filters.level")}
        </label>
        <div className="mt-1 grid grid-cols-3 gap-1">
          <button
            type="button"
            onClick={() => handleLevelClick(null)}
            className={cn(
              "rounded-[var(--svx-radius-sm)] border px-2 py-1 text-[10px] font-medium transition-colors",
              filters.level === null
                ? "border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-primary)]"
                : "border-[var(--svx-color-border-strong)] hover:bg-[var(--svx-color-bg-elevated)]",
            )}
          >
            {t("filters.allLevels")}
          </button>
          {LOG_LEVELS.map((level) => (
            <button
              key={level}
              type="button"
              onClick={() => handleLevelClick(level)}
              className={cn(
                "rounded-[var(--svx-radius-sm)] border px-2 py-1 text-[10px] font-medium transition-colors",
                filters.level === level
                  ? "border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-bg-elevated)]"
                  : "border-[var(--svx-color-border-strong)] hover:bg-[var(--svx-color-bg-elevated)]",
                LEVEL_COLORS[level],
              )}
            >
              {level}
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
          {t("filters.logger")}
        </label>
        <Input
          value={localLogger}
          onChange={handleLoggerChange}
          placeholder="sovyx.brain"
          className="mt-1 h-8 text-xs"
          list={suggestions.length > 0 ? datalistId : undefined}
          autoComplete="off"
        />
        {suggestions.length > 0 && (
          <datalist id={datalistId}>
            {suggestions.map((logger) => (
              <option key={logger} value={logger} />
            ))}
          </datalist>
        )}
      </div>

      <div>
        <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
          {t("filters.sagaId")}
        </label>
        <Input
          value={filters.saga_id}
          onChange={handleSagaChange}
          placeholder="saga-uuid"
          className="mt-1 h-8 text-xs"
        />
      </div>

      <div>
        <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
          {t("filters.since")}
        </label>
        <Input
          type="datetime-local"
          value={filters.since}
          onChange={handleSinceChange}
          className="mt-1 h-8 text-xs"
        />
      </div>

      <div>
        <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
          {t("filters.until")}
        </label>
        <Input
          type="datetime-local"
          value={filters.until}
          onChange={handleUntilChange}
          className="mt-1 h-8 text-xs"
        />
      </div>

      <Button
        variant="ghost"
        size="sm"
        className="mt-auto h-7 gap-1.5 text-xs"
        onClick={onReset}
      >
        <XIcon className="size-3.5" />
        {t("filters.reset")}
      </Button>
    </aside>
  );
}

export const LogFilterBar = memo(LogFilterBarImpl);
