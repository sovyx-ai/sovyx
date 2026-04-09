/**
 * Tests for WelcomeBanner component.
 *
 * Covers: step visual states (pending/active/done), progress bar,
 * dismiss action, ARIA attributes, transitions, edge cases.
 */
import { render, screen, fireEvent } from "@/test/test-utils";
import { WelcomeBanner } from "./welcome-banner";
import type { StepState } from "@/hooks/use-onboarding";

// ── Helpers ──

interface BannerProps {
  step1?: StepState;
  step2?: StepState;
  step3?: StepState;
  completedCount?: number;
  onDismiss?: () => void;
}

function renderBanner({
  step1 = "pending",
  step2 = "pending",
  step3 = "pending",
  completedCount = 0,
  onDismiss = vi.fn(),
}: BannerProps = {}) {
  const dismiss = onDismiss;
  const result = render(
    <WelcomeBanner
      step1={step1}
      step2={step2}
      step3={step3}
      completedCount={completedCount}
      onDismiss={dismiss}
    />,
  );
  return { ...result, dismiss };
}

// ════════════════════════════════════════════════════════
// BASIC RENDERING
// ════════════════════════════════════════════════════════
describe("basic rendering", () => {
  it("renders the banner with data-testid", () => {
    renderBanner();
    expect(screen.getByTestId("welcome-banner")).toBeInTheDocument();
  });

  it("renders all 3 steps", () => {
    renderBanner();
    expect(screen.getByTestId("welcome-step-1")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-step-2")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-step-3")).toBeInTheDocument();
  });

  it("renders welcome title", () => {
    renderBanner();
    expect(screen.getByText("Welcome to Sovyx")).toBeInTheDocument();
  });

  it("renders progress bar", () => {
    renderBanner();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// STEP STATES: PENDING
// ════════════════════════════════════════════════════════
describe("pending state", () => {
  it("step has data-state=pending", () => {
    renderBanner({ step1: "pending" });
    expect(screen.getByTestId("welcome-step-1")).toHaveAttribute("data-state", "pending");
  });

  it("button is NOT visible when pending", () => {
    renderBanner({ step1: "pending", step2: "pending" });
    // Settings button only shows for active step
    expect(screen.queryByText("Go to Settings")).not.toBeInTheDocument();
  });

  it("description is visible when pending", () => {
    renderBanner({ step1: "pending" });
    expect(screen.getByText(/Add your OpenAI/)).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// STEP STATES: ACTIVE
// ════════════════════════════════════════════════════════
describe("active state", () => {
  it("step has data-state=active", () => {
    renderBanner({ step1: "done", step2: "active", completedCount: 1 });
    expect(screen.getByTestId("welcome-step-2")).toHaveAttribute("data-state", "active");
  });

  it("action button IS visible when active", () => {
    renderBanner({ step1: "done", step2: "active", completedCount: 1 });
    expect(screen.getByText("Open Chat")).toBeInTheDocument();
  });

  it("has aria-current=step", () => {
    renderBanner({ step1: "done", step2: "active", completedCount: 1 });
    expect(screen.getByTestId("welcome-step-2")).toHaveAttribute("aria-current", "step");
  });

  it("description is visible when active", () => {
    renderBanner({ step1: "done", step2: "active", completedCount: 1 });
    expect(screen.getByText(/start a conversation/i)).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// STEP STATES: DONE
// ════════════════════════════════════════════════════════
describe("done state", () => {
  it("step has data-state=done", () => {
    renderBanner({ step1: "done", step2: "active", completedCount: 1 });
    expect(screen.getByTestId("welcome-step-1")).toHaveAttribute("data-state", "done");
  });

  it("shows Done badge", () => {
    renderBanner({ step1: "done", step2: "active", completedCount: 1 });
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("has aria-label with completed", () => {
    renderBanner({ step1: "done", step2: "active", completedCount: 1 });
    const step = screen.getByTestId("welcome-step-1");
    // i18n key "welcome.stepDone" = "Done" in locale file
    expect(step.getAttribute("aria-label")).toContain("Done");
  });

  it("action button NOT visible when done", () => {
    renderBanner({ step1: "done", step2: "active", completedCount: 1 });
    // Step 1's "Go to Settings" should not show
    expect(screen.queryByText("Go to Settings")).not.toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// PROGRESS BAR
// ════════════════════════════════════════════════════════
describe("progress bar", () => {
  it("shows 0 of 3 when empty", () => {
    renderBanner({ completedCount: 0 });
    expect(screen.getByText("0 of 3")).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "3");
  });

  it("shows 2 of 3 when partially complete", () => {
    renderBanner({ step1: "done", step2: "done", step3: "active", completedCount: 2 });
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "2");
  });

  it("shows All done ✓ when complete", () => {
    renderBanner({ step1: "done", step2: "done", step3: "done", completedCount: 3 });
    expect(screen.getByText("All done ✓")).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "3");
  });

  it("has aria-valuemin=0", () => {
    renderBanner();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuemin", "0");
  });
});

// ════════════════════════════════════════════════════════
// DISMISS
// ════════════════════════════════════════════════════════
describe("dismiss", () => {
  it("renders dismiss button", () => {
    renderBanner();
    expect(screen.getByTestId("welcome-dismiss")).toBeInTheDocument();
  });

  it("calls onDismiss when clicked", () => {
    const { dismiss } = renderBanner();
    fireEvent.click(screen.getByTestId("welcome-dismiss"));
    expect(dismiss).toHaveBeenCalledTimes(1);
  });

  it("dismiss button has accessible label", () => {
    renderBanner();
    const btn = screen.getByTestId("welcome-dismiss");
    expect(btn).toHaveAttribute("aria-label", "Dismiss setup guide");
  });
});

// ════════════════════════════════════════════════════════
// FULL PROGRESSION
// ════════════════════════════════════════════════════════
describe("full progression", () => {
  it("all pending: 3 steps visible, no Done badges, 0/3 progress", () => {
    renderBanner({ step1: "pending", step2: "pending", step3: "pending", completedCount: 0 });
    expect(screen.queryByText("Done")).not.toBeInTheDocument();
    expect(screen.getByText("0 of 3")).toBeInTheDocument();
  });

  it("step1 done: 1 Done badge, step2 active with button, 1/3 progress", () => {
    renderBanner({ step1: "done", step2: "active", step3: "pending", completedCount: 1 });
    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.getByText("Open Chat")).toBeInTheDocument();
    expect(screen.getByText("1 of 3")).toBeInTheDocument();
  });

  it("step1+2 done: 2 Done badges, step3 active, 2/3 progress", () => {
    renderBanner({ step1: "done", step2: "done", step3: "active", completedCount: 2 });
    const badges = screen.getAllByText("Done");
    expect(badges).toHaveLength(2);
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
  });

  it("all done: 3 Done badges, All done ✓", () => {
    renderBanner({ step1: "done", step2: "done", step3: "done", completedCount: 3 });
    const badges = screen.getAllByText("Done");
    expect(badges).toHaveLength(3);
    expect(screen.getByText("All done ✓")).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// EDGE CASES
// ════════════════════════════════════════════════════════
describe("edge cases", () => {
  it("step 3 has no action button even when active", () => {
    renderBanner({ step1: "done", step2: "done", step3: "active", completedCount: 2 });
    // Step 3 has no action (watch your mind grow — passive step)
    // Only buttons should be dismiss + step2's done state (no chat button since step2 is done)
    const links = screen.queryAllByRole("link");
    // No action links from steps since step1 and step2 are done, step3 has no action
    expect(links).toHaveLength(0);
  });

  it("all data-testids preserved for test stability", () => {
    renderBanner({ step1: "done", step2: "active", step3: "pending", completedCount: 1 });
    expect(screen.getByTestId("welcome-banner")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-step-1")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-step-2")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-step-3")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-dismiss")).toBeInTheDocument();
  });
});
