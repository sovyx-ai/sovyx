/**
 * Training components tests — Mission v0.30.0 §T1.6.
 *
 * Covers:
 * * TrainWakeWordModal — render condition, form prefill, submit
 *   happy + error paths, close behavior.
 * * TrainingJobsPanel — empty state, in-flight, complete, failed,
 *   cancelled rendering; cancel button click.
 *
 * The slice itself is exercised in slices/training.test.ts; here we
 * just verify component-level behavior.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@/test/test-utils";

import type { WakeWordPerMindStatus, TrainingJobStatus } from "@/types/api";

import { useDashboardStore } from "@/stores/dashboard";
import { TrainWakeWordModal } from "./TrainWakeWordModal";
import { TrainingJobsPanel } from "./TrainingJobsPanel";

const BROKEN_ENTRY: WakeWordPerMindStatus = {
  mind_id: "lucia",
  wake_word: "Lúcia",
  voice_language: "pt-BR",
  wake_word_enabled: true,
  runtime_registered: false,
  model_path: null,
  resolution_strategy: "none",
  matched_name: null,
  phoneme_distance: null,
  last_error: "No ONNX model resolved for wake word 'Lúcia' ...",
};

function _resetTrainingState() {
  useDashboardStore.setState({
    trainingJobs: [],
    currentTrainingJob: null,
    trainingLoading: false,
    trainingError: null,
    trainingWs: null,
  });
}

beforeEach(() => {
  _resetTrainingState();
  vi.restoreAllMocks();
});

// ── TrainWakeWordModal ───────────────────────────────────────────────

describe("TrainWakeWordModal — render condition", () => {
  it("renders nothing when open=false", () => {
    const { container } = render(
      <TrainWakeWordModal
        entry={BROKEN_ENTRY}
        open={false}
        onClose={() => {}}
        onStarted={() => {}}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the modal when open=true", () => {
    render(
      <TrainWakeWordModal
        entry={BROKEN_ENTRY}
        open={true}
        onClose={() => {}}
        onStarted={() => {}}
      />,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Train wake word")).toBeInTheDocument();
  });
});

describe("TrainWakeWordModal — prefilled fields", () => {
  it("displays wake_word + mind_id + voice_language from the entry", () => {
    render(
      <TrainWakeWordModal
        entry={BROKEN_ENTRY}
        open={true}
        onClose={() => {}}
        onStarted={() => {}}
      />,
    );
    // Read-only summary block.
    expect(screen.getByText("Lúcia")).toBeInTheDocument();
    expect(screen.getByText("lucia")).toBeInTheDocument();
    expect(screen.getByText("pt-BR")).toBeInTheDocument();
  });
});

describe("TrainWakeWordModal — close behavior", () => {
  it("calls onClose when the × button is clicked", () => {
    const onClose = vi.fn();
    render(
      <TrainWakeWordModal
        entry={BROKEN_ENTRY}
        open={true}
        onClose={onClose}
        onStarted={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText("Close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when the Cancel button is clicked", () => {
    const onClose = vi.fn();
    render(
      <TrainWakeWordModal
        entry={BROKEN_ENTRY}
        open={true}
        onClose={onClose}
        onStarted={() => {}}
      />,
    );
    fireEvent.click(screen.getByText("Cancel"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("TrainWakeWordModal — submit error display", () => {
  it("renders the trainingError banner when slice has error", () => {
    useDashboardStore.setState({
      trainingError: "Trainer backend unavailable: install [training] extras",
    });
    render(
      <TrainWakeWordModal
        entry={BROKEN_ENTRY}
        open={true}
        onClose={() => {}}
        onStarted={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/Trainer backend unavailable/)).toBeInTheDocument();
  });
});

describe("TrainWakeWordModal — Start button disabled state", () => {
  it("disables Start when negatives_dir is empty", () => {
    render(
      <TrainWakeWordModal
        entry={BROKEN_ENTRY}
        open={true}
        onClose={() => {}}
        onStarted={() => {}}
      />,
    );
    const startBtn = screen.getByText("Start training");
    expect(startBtn).toBeDisabled();
  });

  it("enables Start when negatives_dir has content", () => {
    render(
      <TrainWakeWordModal
        entry={BROKEN_ENTRY}
        open={true}
        onClose={() => {}}
        onStarted={() => {}}
      />,
    );
    const negDirInput = screen.getByLabelText(
      /Negatives directory/,
    ) as HTMLInputElement;
    fireEvent.change(negDirInput, { target: { value: "/data/negatives" } });

    const startBtn = screen.getByText("Start training");
    expect(startBtn).not.toBeDisabled();
  });
});

// ── TrainingJobsPanel ────────────────────────────────────────────────

describe("TrainingJobsPanel — empty state", () => {
  it("renders nothing when currentTrainingJob is null", () => {
    const { container } = render(<TrainingJobsPanel />);
    expect(container.firstChild).toBeNull();
  });
});

describe("TrainingJobsPanel — in-flight state", () => {
  it("renders progress bar + samples counter for synthesizing job", () => {
    useDashboardStore.setState({
      currentTrainingJob: {
        summary: {
          job_id: "lucia",
          wake_word: "Lúcia",
          mind_id: "lucia",
          language: "pt-BR",
          status: "synthesizing" as TrainingJobStatus,
          progress: 0.5,
          samples_generated: 100,
          target_samples: 200,
          started_at: "2026-05-03T00:00:00Z",
          updated_at: "2026-05-03T00:01:00Z",
          completed_at: "",
          output_path: "",
          error_summary: "",
          cancelled_signalled: false,
        },
        history: [],
        history_truncated: false,
      },
    });
    render(<TrainingJobsPanel />);
    expect(screen.getByTestId("training-jobs-panel")).toBeInTheDocument();
    expect(screen.getByText("Lúcia")).toBeInTheDocument();
    expect(screen.getByText(/100 . 200 samples/)).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument();
    // In-flight = Cancel button visible (not Dismiss).
    expect(screen.getByText("Cancel training")).toBeInTheDocument();
  });
});

describe("TrainingJobsPanel — terminal states", () => {
  function _seedTerminalJob(
    status: TrainingJobStatus,
    extras: Partial<{
      output_path: string;
      error_summary: string;
      cancelled_signalled: boolean;
    }> = {},
  ): void {
    useDashboardStore.setState({
      currentTrainingJob: {
        summary: {
          job_id: "lucia",
          wake_word: "Lúcia",
          mind_id: "lucia",
          language: "pt-BR",
          status,
          progress: 1.0,
          samples_generated: 200,
          target_samples: 200,
          started_at: "2026-05-03T00:00:00Z",
          updated_at: "2026-05-03T00:30:00Z",
          completed_at: "2026-05-03T00:30:00Z",
          output_path: extras.output_path ?? "",
          error_summary: extras.error_summary ?? "",
          cancelled_signalled: extras.cancelled_signalled ?? false,
        },
        history: [],
        history_truncated: false,
      },
    });
  }

  it("renders complete state with output_path + completeHint", () => {
    _seedTerminalJob("complete", {
      output_path: "/data/wake_word_models/pretrained/lucia.onnx",
    });
    render(<TrainingJobsPanel />);
    expect(screen.getByText("Complete")).toBeInTheDocument();
    expect(
      screen.getByText("/data/wake_word_models/pretrained/lucia.onnx"),
    ).toBeInTheDocument();
    expect(screen.getByText(/trained model is now in/)).toBeInTheDocument();
    expect(screen.getByText("Dismiss")).toBeInTheDocument();
  });

  it("renders failed state with error disclosure", () => {
    _seedTerminalJob("failed", {
      error_summary: "Kokoro synthesis crashed: out of memory",
    });
    render(<TrainingJobsPanel />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("View error details")).toBeInTheDocument();
    // The error_summary is inside <details> (in-DOM even pre-expand).
    expect(
      screen.getByText("Kokoro synthesis crashed: out of memory"),
    ).toBeInTheDocument();
  });

  it("renders cancelled state with Dismiss", () => {
    _seedTerminalJob("cancelled");
    render(<TrainingJobsPanel />);
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
    expect(screen.getByText("Dismiss")).toBeInTheDocument();
  });
});

describe("TrainingJobsPanel — cancel button click", () => {
  it("disables button + shows Cancelling… when cancelled_signalled=true", () => {
    useDashboardStore.setState({
      currentTrainingJob: {
        summary: {
          job_id: "lucia",
          wake_word: "Lúcia",
          mind_id: "lucia",
          language: "pt-BR",
          status: "synthesizing" as TrainingJobStatus,
          progress: 0.5,
          samples_generated: 100,
          target_samples: 200,
          started_at: "2026-05-03T00:00:00Z",
          updated_at: "2026-05-03T00:01:00Z",
          completed_at: "",
          output_path: "",
          error_summary: "",
          cancelled_signalled: true,
        },
        history: [],
        history_truncated: false,
      },
    });
    render(<TrainingJobsPanel />);
    expect(screen.getByText("Cancelling…")).toBeInTheDocument();
    expect(screen.getByText("Cancelling…")).toBeDisabled();
  });
});

describe("TrainingJobsPanel — Dismiss clears currentTrainingJob", () => {
  it("clears currentTrainingJob when Dismiss is clicked on a complete job", async () => {
    useDashboardStore.setState({
      currentTrainingJob: {
        summary: {
          job_id: "lucia",
          wake_word: "Lúcia",
          mind_id: "lucia",
          language: "pt-BR",
          status: "complete" as TrainingJobStatus,
          progress: 1.0,
          samples_generated: 200,
          target_samples: 200,
          started_at: "2026-05-03T00:00:00Z",
          updated_at: "2026-05-03T00:30:00Z",
          completed_at: "2026-05-03T00:30:00Z",
          output_path: "/path/lucia.onnx",
          error_summary: "",
          cancelled_signalled: false,
        },
        history: [],
        history_truncated: false,
      },
    });
    render(<TrainingJobsPanel />);
    fireEvent.click(screen.getByText("Dismiss"));
    await waitFor(() => {
      expect(useDashboardStore.getState().currentTrainingJob).toBeNull();
    });
  });
});
