/**
 * Smoke tests for the voice-calibration subpackage subcomponents.
 *
 * Each subcomponent renders a focused slice of the wizard UX. These
 * tests verify the render contract (right testid, key strings via
 * i18n) without exercising the orchestrator. The orchestrator-level
 * test (VoiceCalibrationStep.test.tsx) covers the composition.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@/test/test-utils";

import { CancelDialog } from "./_CancelDialog";
import { CapturePrompt } from "./_CapturePrompt";
import { FallbackBanner } from "./_FallbackBanner";
import { FastPathProgress } from "./_FastPathProgress";
import { ProfileReview } from "./_ProfileReview";
import { SlowPathProgress } from "./_SlowPathProgress";

describe("FastPathProgress", () => {
  it("renders the fast-path testid + progress bar percent", () => {
    render(
      <FastPathProgress
        status="Looking up matching profile"
        progressPct={42}
        onCancel={() => {}}
        cancelling={false}
      />,
    );
    expect(
      screen.getByTestId("voice-calibration-fast-path-progress"),
    ).toBeInTheDocument();
    expect(screen.getByText(/42%/)).toBeInTheDocument();
  });

  it("invokes onCancel when the cancel button is clicked", () => {
    const onCancel = vi.fn();
    render(
      <FastPathProgress
        status="X"
        progressPct={50}
        onCancel={onCancel}
        cancelling={false}
      />,
    );
    fireEvent.click(screen.getByTestId("voice-calibration-fast-cancel"));
    expect(onCancel).toHaveBeenCalledOnce();
  });
});

describe("SlowPathProgress", () => {
  it("renders the slow-path testid + 3-stage timeline", () => {
    render(
      <SlowPathProgress
        rawStatus="slow_path_diag"
        status="Running forensic diagnostic"
        progressPct={10}
        onCancel={() => {}}
        cancelling={false}
      />,
    );
    expect(
      screen.getByTestId("voice-calibration-slow-path-progress"),
    ).toBeInTheDocument();
    expect(screen.getByText(/10%/)).toBeInTheDocument();
  });

  it("renders <CapturePrompt speak> when currentPrompt is a speak prompt", () => {
    // v0.30.31 (P3) capture-prompt protocol — the bash diag's "say X"
    // surface flows through state.extras.current_prompt and SlowPathProgress
    // renders the CapturePrompt component inline.
    render(
      <SlowPathProgress
        rawStatus="slow_path_diag"
        status="Running forensic diagnostic"
        progressPct={10}
        onCancel={() => {}}
        cancelling={false}
        currentPrompt={{ type: "speak", phrase: "Sovyx, me ouça" }}
      />,
    );
    expect(
      screen.getByTestId("voice-calibration-capture-prompt-speak"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Sovyx, me ouça/)).toBeInTheDocument();
  });

  it("renders <CapturePrompt silence> when currentPrompt is a silence prompt", () => {
    render(
      <SlowPathProgress
        rawStatus="slow_path_diag"
        status="Running forensic diagnostic"
        progressPct={10}
        onCancel={() => {}}
        cancelling={false}
        currentPrompt={{ type: "silence", phrase: null, seconds: 3 }}
      />,
    );
    expect(
      screen.getByTestId("voice-calibration-capture-prompt-silence"),
    ).toBeInTheDocument();
  });

  it("does NOT render CapturePrompt when currentPrompt is null", () => {
    render(
      <SlowPathProgress
        rawStatus="slow_path_diag"
        status="Running forensic diagnostic"
        progressPct={10}
        onCancel={() => {}}
        cancelling={false}
        currentPrompt={null}
      />,
    );
    expect(
      screen.queryByTestId("voice-calibration-capture-prompt-speak"),
    ).toBeNull();
    expect(
      screen.queryByTestId("voice-calibration-capture-prompt-silence"),
    ).toBeNull();
  });
});

describe("CapturePrompt", () => {
  it("renders speak prompt with the phrase", () => {
    render(<CapturePrompt phrase="Hello Sovyx" />);
    expect(
      screen.getByTestId("voice-calibration-capture-prompt-speak"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Hello Sovyx/)).toBeInTheDocument();
  });

  it("renders silence prompt when silenceSeconds is set", () => {
    render(<CapturePrompt phrase="" silenceSeconds={5} />);
    expect(
      screen.getByTestId("voice-calibration-capture-prompt-silence"),
    ).toBeInTheDocument();
  });
});

describe("ProfileReview", () => {
  it("renders the done banner + winner hid + profile path", () => {
    render(
      <ProfileReview
        triageWinnerHid="H10"
        profilePath="/data/sovyx/default/calibration.json"
        onCompleted={() => {}}
      />,
    );
    expect(
      screen.getByTestId("voice-calibration-profile-review"),
    ).toBeInTheDocument();
    // rc.10 fix #5: HID is mapped to a friendly label via
    // `calibration.hypothesis.H10` instead of being shown raw, so
    // non-technical operators see "Microphone volume was set too low
    // at the system level" instead of "H10".
    expect(
      screen.getByText(/Microphone volume was set too low/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/calibration.json/)).toBeInTheDocument();
  });

  it("invokes onCompleted on confirm click", () => {
    const onCompleted = vi.fn();
    render(
      <ProfileReview
        triageWinnerHid={null}
        profilePath={null}
        onCompleted={onCompleted}
      />,
    );
    const button = screen.getByRole("button");
    fireEvent.click(button);
    expect(onCompleted).toHaveBeenCalledOnce();
  });
});

describe("FallbackBanner", () => {
  it("renders the fallback testid + reason", () => {
    render(
      <FallbackBanner
        fallbackReason="diag_prerequisite_unmet"
        onFallback={() => {}}
      />,
    );
    expect(
      screen.getByTestId("voice-calibration-fallback-banner"),
    ).toBeInTheDocument();
    expect(screen.getByText(/diag_prerequisite_unmet/)).toBeInTheDocument();
  });
});

describe("CancelDialog", () => {
  it("renders the cancel testid + confirm + dismiss", () => {
    render(
      <CancelDialog
        cancelling={false}
        onConfirm={() => {}}
        onDismiss={() => {}}
      />,
    );
    expect(
      screen.getByTestId("voice-calibration-cancel-dialog"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("voice-calibration-cancel-confirm"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("voice-calibration-cancel-dismiss"),
    ).toBeInTheDocument();
  });

  it("invokes onConfirm + onDismiss appropriately", () => {
    const onConfirm = vi.fn();
    const onDismiss = vi.fn();
    render(
      <CancelDialog
        cancelling={false}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(screen.getByTestId("voice-calibration-cancel-dismiss"));
    expect(onDismiss).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByTestId("voice-calibration-cancel-confirm"));
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});
