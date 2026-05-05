/**
 * VoiceSetupWizard — Mission v0.30.0 §T2 (D4).
 *
 * 5-step microphone setup wizard. Mounted in onboarding (v0.30.0
 * §T2.2) + in pages/voice.tsx as an opt-in entry point. Backend
 * endpoints already exist (Phase 7 / T7.21-T7.24); T2 ships only
 * the React frontend.
 *
 * State machine (useReducer):
 *   "devices" → "record" → "results" → "save" → done
 *                ↑                       ↓
 *                └─ retry ─── results ───┘
 *
 * Each transition is enforced by the reducer's discriminated-union
 * step + action shape — no implicit string-comparison drift.
 */
import type { JSX } from "react";
import { useEffect, useReducer, useRef } from "react";
import { useTranslation } from "react-i18next";

import { api } from "@/lib/api";
import type {
  WizardDevicesResponse,
  WizardDeviceInfo,
  WizardTestResultResponse,
  WizardDiagnosticResponse,
} from "@/types/api";
import {
  WizardDevicesResponseSchema,
  WizardDiagnosticResponseSchema,
  WizardTestResultResponseSchema,
} from "@/types/schemas";

// ── State machine ────────────────────────────────────────────────────

type WizardStep = "devices" | "record" | "results" | "save" | "done";

interface WizardState {
  step: WizardStep;
  devices: WizardDeviceInfo[];
  selectedDeviceId: string | null;
  testResult: WizardTestResultResponse | null;
  diagnostic: WizardDiagnosticResponse | null;
  loading: boolean;
  error: string | null;
}

const _INITIAL_STATE: WizardState = {
  step: "devices",
  devices: [],
  selectedDeviceId: null,
  testResult: null,
  diagnostic: null,
  loading: false,
  error: null,
};

type WizardAction =
  | { type: "loading"; on: boolean }
  | { type: "error"; message: string }
  | { type: "devicesLoaded"; devices: WizardDeviceInfo[] }
  | { type: "selectDevice"; deviceId: string }
  | { type: "testStarted" }
  | { type: "testFinished"; result: WizardTestResultResponse }
  | { type: "diagnosticLoaded"; diagnostic: WizardDiagnosticResponse }
  | { type: "advanceToSave" }
  | { type: "advanceToDone" }
  | { type: "retry" }
  | { type: "reset" };

function _reducer(state: WizardState, action: WizardAction): WizardState {
  switch (action.type) {
    case "loading":
      return { ...state, loading: action.on };
    case "error":
      return { ...state, error: action.message, loading: false };
    case "devicesLoaded":
      return { ...state, devices: action.devices, loading: false, error: null };
    case "selectDevice":
      return { ...state, selectedDeviceId: action.deviceId, step: "record" };
    case "testStarted":
      return { ...state, loading: true, error: null };
    case "testFinished":
      return {
        ...state,
        testResult: action.result,
        loading: false,
        step: "results",
      };
    case "diagnosticLoaded":
      return { ...state, diagnostic: action.diagnostic };
    case "advanceToSave":
      return { ...state, step: "save" };
    case "advanceToDone":
      return { ...state, step: "done" };
    case "retry":
      return { ...state, step: "record", testResult: null, error: null };
    case "reset":
      return _INITIAL_STATE;
  }
}

// ── Top-level component ──────────────────────────────────────────────

interface VoiceSetupWizardProps {
  /** Called when the operator finishes the wizard (Save → Done). */
  onComplete?: (deviceId: string | null) => void;
  /** Called when the operator dismisses the wizard mid-flow. */
  onCancel?: () => void;
}

export function VoiceSetupWizard({
  onComplete,
  onCancel,
}: VoiceSetupWizardProps): JSX.Element {
  const { t } = useTranslation("voice");
  const [state, dispatch] = useReducer(_reducer, _INITIAL_STATE);

  // ── A/B telemetry (Mission v0.30.1 §T1.2) ─────────────────────────
  // Refs avoid re-rendering on telemetry-only updates. ``stepStartRef``
  // tracks (step, t_start) so a transition emits dwell for the prior
  // step. ``completedRef`` flips true on reaching ``done`` — used by
  // the unmount cleanup to discriminate completion vs abandonment.
  const stepStartRef = useRef<{ step: WizardStep; t: number }>({
    step: _INITIAL_STATE.step,
    t: performance.now(),
  });
  const completedRef = useRef<boolean>(false);

  // Emit step_dwell on every step transition. Best-effort POST: a
  // network failure must not break the wizard's UX, so we swallow.
  useEffect(() => {
    const previous = stepStartRef.current;
    if (previous.step === state.step) return;
    const duration = Math.round(performance.now() - previous.t);
    void api
      .post("/api/voice/wizard/telemetry", {
        event: "step_dwell",
        step: previous.step,
        duration_ms: duration,
      })
      .catch(() => {
        // Telemetry is best-effort; swallow so wizard UX is unaffected.
      });
    stepStartRef.current = { step: state.step, t: performance.now() };
    if (state.step === "done") {
      completedRef.current = true;
      void api
        .post("/api/voice/wizard/telemetry", {
          event: "completion",
          outcome: "completed",
          exit_step: "done",
        })
        .catch(() => {});
    }
  }, [state.step]);

  // Emit abandonment on unmount when the wizard never reached done.
  useEffect(() => {
    return () => {
      if (completedRef.current) return;
      const exitStep = stepStartRef.current.step;
      void api
        .post("/api/voice/wizard/telemetry", {
          event: "completion",
          outcome: "abandoned",
          exit_step: exitStep,
        })
        .catch(() => {});
    };
  }, []);

  // Step 1: fetch devices on mount.
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      dispatch({ type: "loading", on: true });
      try {
        const data = await api.get<WizardDevicesResponse>(
          "/api/voice/wizard/devices",
          { schema: WizardDevicesResponseSchema },
        );
        if (!cancelled) {
          dispatch({ type: "devicesLoaded", devices: data.devices });
        }
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : "Failed to load devices";
          dispatch({ type: "error", message });
        }
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  // Step 4: fetch APO diagnostic alongside results display.
  useEffect(() => {
    if (state.step !== "results" || state.diagnostic !== null) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await api.get<WizardDiagnosticResponse>(
          "/api/voice/wizard/diagnostic",
          { schema: WizardDiagnosticResponseSchema },
        );
        if (!cancelled) {
          dispatch({ type: "diagnosticLoaded", diagnostic: data });
        }
      } catch {
        // Diagnostic failure is non-fatal — wizard continues without
        // the APO recommendations panel. Operator sees raw test results.
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [state.step, state.diagnostic]);

  const handleStartRecording = async (): Promise<void> => {
    if (state.selectedDeviceId === null) return;
    dispatch({ type: "testStarted" });
    try {
      const data = await api.post<WizardTestResultResponse>(
        "/api/voice/wizard/test-record",
        {
          device_id: state.selectedDeviceId,
          duration_seconds: 3.0,
        },
        { schema: WizardTestResultResponseSchema },
      );
      dispatch({ type: "testFinished", result: data });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Recording failed";
      dispatch({ type: "error", message });
    }
  };

  const handleSave = (): void => {
    // Save selection — for v0.30.0 the wizard's selection is
    // application-scope (not yet persisted to mind.yaml; that wire-up
    // is left for v0.30.x patches once the wizard's ratification UX
    // is validated by D22 browser pilot).
    dispatch({ type: "advanceToDone" });
    onComplete?.(state.selectedDeviceId);
  };

  return (
    <div
      data-testid="voice-setup-wizard"
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4"
    >
      <header className="mb-3 flex items-center justify-between">
        <h2 className="text-base font-semibold text-[var(--svx-color-text-primary)]">
          {t("wizard.title")}
        </h2>
        {onCancel !== undefined && state.step !== "done" && (
          <button
            type="button"
            onClick={onCancel}
            className="text-xs text-[var(--svx-color-text-tertiary)] hover:underline"
          >
            {t("wizard.cancel")}
          </button>
        )}
      </header>

      {/* Step pill */}
      <div className="mb-3 text-xs text-[var(--svx-color-text-tertiary)]">
        {t(`wizard.steps.${state.step}`)}
      </div>

      {/* Error banner */}
      {state.error !== null && (
        <div
          role="alert"
          className="mb-3 rounded border border-[var(--svx-color-danger)] bg-[var(--svx-color-danger-soft)] p-2 text-xs text-[var(--svx-color-danger)]"
        >
          {state.error}
        </div>
      )}

      {/* Step content */}
      {state.step === "devices" && (
        <_DevicesStep
          devices={state.devices}
          loading={state.loading}
          onSelect={(id) => dispatch({ type: "selectDevice", deviceId: id })}
          t={t}
        />
      )}
      {state.step === "record" && (
        <_RecordStep
          selectedDevice={state.devices.find(
            (d) => d.device_id === state.selectedDeviceId,
          )}
          loading={state.loading}
          onStart={handleStartRecording}
          t={t}
        />
      )}
      {state.step === "results" && state.testResult !== null && (
        <_ResultsStep
          result={state.testResult}
          diagnostic={state.diagnostic}
          onRetry={() => dispatch({ type: "retry" })}
          onAdvance={() => dispatch({ type: "advanceToSave" })}
          t={t}
        />
      )}
      {state.step === "save" && (
        <_SaveStep
          deviceId={state.selectedDeviceId}
          onSave={handleSave}
          t={t}
        />
      )}
      {state.step === "done" && (
        <_DoneStep
          onClose={() => onCancel?.()}
          t={t}
        />
      )}
    </div>
  );
}

// ── Sub-components (one file, inline for v0.30.0 simplicity) ─────────

type TFunction = (key: string, opts?: Record<string, unknown>) => string;

function _DevicesStep({
  devices,
  loading,
  onSelect,
  t,
}: {
  devices: WizardDeviceInfo[];
  loading: boolean;
  onSelect: (deviceId: string) => void;
  t: TFunction;
}): JSX.Element {
  if (loading) {
    return <div className="text-sm text-[var(--svx-color-text-secondary)]">{t("wizard.devices.loading")}</div>;
  }
  if (devices.length === 0) {
    return <div className="text-sm text-[var(--svx-color-text-tertiary)]">{t("wizard.devices.empty")}</div>;
  }
  return (
    <div className="space-y-2">
      <p className="text-sm text-[var(--svx-color-text-secondary)]">{t("wizard.devices.intro")}</p>
      <ul className="flex flex-col gap-2">
        {devices.map((d) => (
          <li key={d.device_id}>
            <button
              type="button"
              onClick={() => onSelect(d.device_id)}
              className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-2 text-left text-sm hover:border-[var(--svx-color-accent)]"
            >
              <div className="font-medium">{d.friendly_name || d.name}</div>
              <div className="font-mono text-xs text-[var(--svx-color-text-tertiary)]">
                {d.max_input_channels}ch · {d.default_sample_rate}Hz · {d.diagnosis_hint}
              </div>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function _RecordStep({
  selectedDevice,
  loading,
  onStart,
  t,
}: {
  selectedDevice: WizardDeviceInfo | undefined;
  loading: boolean;
  onStart: () => Promise<void>;
  t: TFunction;
}): JSX.Element {
  return (
    <div className="space-y-3">
      <p className="text-sm text-[var(--svx-color-text-secondary)]">
        {t("wizard.record.intro")}
      </p>
      {selectedDevice !== undefined && (
        <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-surface-secondary)] p-2 text-xs">
          <div className="text-[var(--svx-color-text-tertiary)]">{t("wizard.record.selected")}</div>
          <div className="mt-0.5 font-mono">{selectedDevice.friendly_name || selectedDevice.name}</div>
        </div>
      )}
      <button
        type="button"
        onClick={() => void onStart()}
        disabled={loading}
        className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-accent)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
      >
        {loading ? t("wizard.record.recording") : t("wizard.record.start")}
      </button>
    </div>
  );
}

function _ResultsStep({
  result,
  diagnostic,
  onRetry,
  onAdvance,
  t,
}: {
  result: WizardTestResultResponse;
  diagnostic: WizardDiagnosticResponse | null;
  onRetry: () => void;
  onAdvance: () => void;
  t: TFunction;
}): JSX.Element {
  const okDiagnoses = ["ok"];
  const isOk = okDiagnoses.includes(result.diagnosis);
  return (
    <div className="space-y-3">
      <div
        className={`rounded-[var(--svx-radius-md)] p-2 text-xs ${
          isOk
            ? "bg-[var(--svx-color-success-soft)] text-[var(--svx-color-success)]"
            : "bg-[var(--svx-color-warning-soft)] text-[var(--svx-color-warning)]"
        }`}
      >
        <div className="font-semibold">{t(`wizard.results.diagnosis.${result.diagnosis}`)}</div>
        {/*
          Mission MISSION-voice-linux-silent-mic-remediation-2026-05-04
          §Phase 2 T2.7 — render the backend's diagnosis_hint below
          the diagnosis label. Pre-T2.7 the hint was emitted by the
          backend but never rendered, so operators only saw the short
          i18n label (e.g. "Nenhum áudio capturado") with no actionable
          recipe. The hint carries platform-aware shell commands when
          relevant (Linux+PipeWire ALSA mixer + WirePlumber recipes),
          so operators have a concrete next action right next to the
          failure verdict.
          whitespace-pre-line preserves the newlines the backend
          embeds in the multi-step recipe.
        */}
        {result.diagnosis_hint && !isOk && (
          <p className="mt-1.5 whitespace-pre-line text-[11px] leading-relaxed opacity-90">
            {result.diagnosis_hint}
          </p>
        )}
      </div>
      <dl className="grid grid-cols-2 gap-2 text-xs">
        <dt className="text-[var(--svx-color-text-tertiary)]">{t("wizard.results.rms")}</dt>
        <dd className="font-mono">{result.level_rms_dbfs?.toFixed(1) ?? "—"} dBFS</dd>
        <dt className="text-[var(--svx-color-text-tertiary)]">{t("wizard.results.peak")}</dt>
        <dd className="font-mono">{result.level_peak_dbfs?.toFixed(1) ?? "—"} dBFS</dd>
        <dt className="text-[var(--svx-color-text-tertiary)]">{t("wizard.results.snr")}</dt>
        <dd className="font-mono">{result.snr_db?.toFixed(1) ?? "—"} dB</dd>
        <dt className="text-[var(--svx-color-text-tertiary)]">{t("wizard.results.clipping")}</dt>
        <dd className="font-mono">{result.clipping_detected ? t("wizard.results.yes") : t("wizard.results.no")}</dd>
      </dl>
      {diagnostic !== null && diagnostic.recommendations.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-[var(--svx-color-text-secondary)]">
            {t("wizard.results.recommendations")}
          </summary>
          <ul className="mt-1.5 list-disc pl-5 text-[var(--svx-color-text-tertiary)]">
            {diagnostic.recommendations.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="flex justify-end gap-2 border-t border-[var(--svx-color-border)] pt-2">
        <button
          type="button"
          onClick={onRetry}
          className="rounded-[var(--svx-radius-md)] px-3 py-1 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)]"
        >
          {t("wizard.results.retry")}
        </button>
        <button
          type="button"
          onClick={onAdvance}
          className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-accent)] px-3 py-1 text-xs font-medium text-white"
        >
          {t("wizard.results.advance")}
        </button>
      </div>
    </div>
  );
}

function _SaveStep({
  deviceId,
  onSave,
  t,
}: {
  deviceId: string | null;
  onSave: () => void;
  t: TFunction;
}): JSX.Element {
  return (
    <div className="space-y-3">
      <p className="text-sm text-[var(--svx-color-text-secondary)]">
        {t("wizard.save.intro")}
      </p>
      {deviceId !== null && (
        <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-surface-secondary)] p-2 font-mono text-xs">
          {deviceId}
        </div>
      )}
      <button
        type="button"
        onClick={onSave}
        className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-accent)] px-3 py-1.5 text-sm font-medium text-white"
      >
        {t("wizard.save.button")}
      </button>
    </div>
  );
}

function _DoneStep({
  onClose,
  t,
}: {
  onClose: () => void;
  t: TFunction;
}): JSX.Element {
  return (
    <div className="space-y-3">
      <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-success-soft)] p-2 text-sm text-[var(--svx-color-success)]">
        {t("wizard.done.message")}
      </div>
      <button
        type="button"
        onClick={onClose}
        className="rounded-[var(--svx-radius-md)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)]"
      >
        {t("wizard.done.close")}
      </button>
    </div>
  );
}
