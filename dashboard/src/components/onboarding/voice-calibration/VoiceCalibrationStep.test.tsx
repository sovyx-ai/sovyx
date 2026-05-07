/**
 * Orchestrator-level tests for ``VoiceCalibrationStep`` (the host
 * component composed of the subcomponents tested in
 * ``subcomponents.test.tsx``).
 *
 * Coverage as of rc.15:
 * * LOW.3 — the resolved mind_id is surfaced as a small label next
 *   to the title when ``currentCalibrationJob.mind_id !== "default"``.
 *   Default-mind operators don't see the label (single-mind UX
 *   stays clean); multi-mind operators see ``Mind: <name>`` so they
 *   can confirm the backend resolver (rc.12) landed the right mind.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { render, screen } from "@/test/test-utils";

import { useDashboardStore } from "@/stores/dashboard";

import { VoiceCalibrationStep } from "./VoiceCalibrationStep";

beforeEach(() => {
  useDashboardStore.setState({
    currentCalibrationJob: null,
    calibrationPreview: null,
    calibrationLoading: false,
    calibrationError: null,
  });
});

describe("VoiceCalibrationStep — mind label (rc.15 LOW.3)", () => {
  it("hides the mind label when no calibration job is active", () => {
    render(
      <VoiceCalibrationStep
        mindId="default"
        onCompleted={vi.fn()}
        onFallback={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("voice-calibration-mind-label"),
    ).not.toBeInTheDocument();
  });

  it("hides the mind label when the active job's mind_id is the default sentinel", () => {
    useDashboardStore.setState({
      currentCalibrationJob: {
        job_id: "default",
        mind_id: "default",
        status: "slow_path_diag",
        progress: 0.1,
        current_stage_message: "Running detailed audio test",
        created_at_utc: "2026-05-07T00:00:00Z",
        updated_at_utc: "2026-05-07T00:00:01Z",
        profile_path: null,
        triage_winner_hid: null,
        error_summary: null,
        fallback_reason: null,
        extras: null,
      },
    });
    render(
      <VoiceCalibrationStep
        mindId="default"
        onCompleted={vi.fn()}
        onFallback={vi.fn()}
      />,
    );
    // Single-mind UX stays clean — no badge clutter when the mind
    // is the (universal) default.
    expect(
      screen.queryByTestId("voice-calibration-mind-label"),
    ).not.toBeInTheDocument();
  });

  it("shows ``Mind: <name>`` label when the active job's mind_id is non-default (rc.12 resolver landed)", () => {
    useDashboardStore.setState({
      currentCalibrationJob: {
        job_id: "meu-mind",
        mind_id: "meu-mind",
        status: "slow_path_diag",
        progress: 0.1,
        current_stage_message: "Running detailed audio test",
        created_at_utc: "2026-05-07T00:00:00Z",
        updated_at_utc: "2026-05-07T00:00:01Z",
        profile_path: null,
        triage_winner_hid: null,
        error_summary: null,
        fallback_reason: null,
        extras: null,
      },
    });
    render(
      <VoiceCalibrationStep
        mindId="default"
        onCompleted={vi.fn()}
        onFallback={vi.fn()}
      />,
    );
    const label = screen.getByTestId("voice-calibration-mind-label");
    expect(label).toBeInTheDocument();
    // The label cites the resolved mind name so the operator sees
    // the rc.12 backend resolver landed the right value (instead of
    // the literal ``"default"`` the frontend hardcoded in the POST).
    expect(label.textContent).toContain("meu-mind");
  });
});
