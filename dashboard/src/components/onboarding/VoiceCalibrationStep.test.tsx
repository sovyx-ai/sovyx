/**
 * VoiceCalibrationStep component tests -- v0.30.18 patch 2.
 *
 * Validates the idle phase: renders the start button, the "Use simple
 * setup" explicit fallback button, and forwards each click to the
 * right callback. Phase transitions (running / terminal) are exercised
 * by the slice tests + the v0.30.18 E2E integration test (C4).
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider } from "react-i18next";
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { VoiceCalibrationStep } from "./VoiceCalibrationStep";
import { useDashboardStore } from "@/stores/dashboard";

// ── i18n test setup -- load the english voice.json so calibration.*
//    keys resolve. Other namespaces fall back to the key string,
//    which is what production behaviour would surface anyway.
const _i18nReady = (async () => {
  const enVoice = await import("../../locales/en/voice.json");
  await i18n.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    ns: ["voice"],
    defaultNS: "voice",
    resources: {
      en: { voice: enVoice.default ?? enVoice },
    },
    interpolation: { escapeValue: false },
  });
})();

beforeEach(async () => {
  await _i18nReady;
  // Reset slice state between tests.
  useDashboardStore.setState({
    currentCalibrationJob: null,
    calibrationPreview: null,
    calibrationLoading: false,
    calibrationError: null,
    calibrationWs: null,
  });
});

function renderStep(props?: {
  onCompleted?: () => void;
  onFallback?: () => void;
}): { onCompleted: ReturnType<typeof vi.fn>; onFallback: ReturnType<typeof vi.fn> } {
  const onCompleted = props?.onCompleted ?? vi.fn();
  const onFallback = props?.onFallback ?? vi.fn();
  // Stub fetchCalibrationPreview so the component's mount effect
  // doesn't hit the network during test render.
  useDashboardStore.setState({
    fetchCalibrationPreview: vi.fn().mockResolvedValue(null),
  } as Partial<ReturnType<typeof useDashboardStore.getState>>);
  render(
    <I18nextProvider i18n={i18n}>
      <VoiceCalibrationStep
        mindId="default"
        onCompleted={onCompleted as () => void}
        onFallback={onFallback as () => void}
      />
    </I18nextProvider>,
  );
  return {
    onCompleted: onCompleted as ReturnType<typeof vi.fn>,
    onFallback: onFallback as ReturnType<typeof vi.fn>,
  };
}

// ─────────────────────────────────────────────────────────────────

describe("VoiceCalibrationStep — idle phase", () => {
  it("renders title + Start + Use simple setup buttons", () => {
    renderStep();
    expect(screen.getByText("Voice calibration")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Start calibration/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Use simple setup/i }),
    ).toBeInTheDocument();
  });

  it("clicking Use simple setup invokes onFallback", async () => {
    const { onFallback, onCompleted } = renderStep();
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: /Use simple setup/i }),
    );
    expect(onFallback).toHaveBeenCalledTimes(1);
    expect(onCompleted).not.toHaveBeenCalled();
  });

  it("clicking Start invokes startCalibration via the store", async () => {
    const startCalibrationMock = vi.fn().mockResolvedValue(null);
    useDashboardStore.setState({
      startCalibration: startCalibrationMock,
    } as Partial<ReturnType<typeof useDashboardStore.getState>>);
    renderStep();
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: /Start calibration/i }),
    );
    expect(startCalibrationMock).toHaveBeenCalledWith({ mind_id: "default" });
  });

  it("renders the preview block when calibrationPreview is set", () => {
    useDashboardStore.setState({
      calibrationPreview: {
        fingerprint_hash: "a".repeat(64),
        audio_stack: "pipewire",
        system_vendor: "Sony",
        system_product: "VAIO",
        recommendation: "slow_path",
      },
    });
    renderStep();
    expect(screen.getByText(/Sony VAIO/)).toBeInTheDocument();
    expect(screen.getByText(/pipewire/)).toBeInTheDocument();
  });
});
