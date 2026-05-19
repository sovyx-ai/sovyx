/* Vitest unit tests for Mission H4 §T3.4 ResourceHealthSection widget. */

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ResourceHealthSection } from "./ResourceHealthSection";

// Mock react-i18next so tests don't need full i18n setup.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

// Mock useApiPoller hook to inject controlled data.
const mockPollerState: {
  data: unknown;
  error: string | null;
} = { data: null, error: null };

vi.mock("@/hooks/use-api-poller", () => ({
  useApiPoller: () => mockPollerState,
}));

describe("ResourceHealthSection", () => {
  beforeEach(() => {
    mockPollerState.data = null;
    mockPollerState.error = null;
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading state when no data yet", () => {
    mockPollerState.data = null;
    mockPollerState.error = null;
    render(<ResourceHealthSection />);
    expect(screen.getByTestId("resource-health-loading")).toBeInTheDocument();
  });

  it("shows degraded state on poller error", () => {
    mockPollerState.data = null;
    mockPollerState.error = "degraded";
    render(<ResourceHealthSection />);
    expect(screen.getByTestId("resource-health-degraded")).toBeInTheDocument();
  });

  it("renders 8 cohort sections when snapshot present", () => {
    mockPollerState.data = {
      observed_at_unix: 1716143280,
      cohorts: {
        "process.rss_bytes": 100_000_000,
        "asyncio.task_count": 5,
        "to_thread.pool_size": 4,
        "lock_dict.total_cardinality": 42,
        "onnx.session_count": 4,
        "gc.objects_count": 50000,
        "tracemalloc.is_tracing": false,
        "exception_cohort.retained_bytes_estimate": 0,
      },
      canonical_field_count: 28,
      legacy_alias_count: 1,
    };
    render(<ResourceHealthSection />);
    expect(screen.getByTestId("resource-health-sections")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-process")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-asyncio")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-to_thread")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-lock_dict")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-onnx")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-gc")).toBeInTheDocument();
    expect(screen.getByTestId("resource-section-tracemalloc")).toBeInTheDocument();
    expect(
      screen.getByTestId("resource-section-exception_cohort"),
    ).toBeInTheDocument();
  });
});
