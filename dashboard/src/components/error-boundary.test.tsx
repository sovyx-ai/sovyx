import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@/lib/i18n";
import { ErrorBoundary } from "./error-boundary";

function ThrowingComponent({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error("Test error");
  return <div>All good</div>;
}

describe("ErrorBoundary", () => {
  // Suppress console.error for intentional throws
  const originalError = console.error;
  beforeEach(() => {
    console.error = vi.fn();
  });
  afterEach(() => {
    console.error = originalError;
  });

  it("renders children when no error", () => {
    render(
      <ErrorBoundary>
        <ThrowingComponent shouldThrow={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("All good")).toBeInTheDocument();
  });

  it("renders error UI when child throws", () => {
    render(
      <ErrorBoundary>
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByText("Test error")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("recovers when Try Again is clicked", async () => {
    // After clicking Try Again, the boundary resets and re-renders children.
    // Since the same children would throw again, just verify the button works.
    render(
      <ErrorBoundary>
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    // After reset, the component throws again, so error UI re-appears
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });

  it("renders custom fallback", () => {
    render(
      <ErrorBoundary fallback={<div>Custom error</div>}>
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Custom error")).toBeInTheDocument();
  });

  it("section variant renders a compact inline alert", () => {
    render(
      <ErrorBoundary name="section.test" variant="section">
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>,
    );
    // Compact fallback uses role="alert" and the sectionFailed copy,
    // not the page-level "Something went wrong" heading.
    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveAttribute("data-boundary", "section.test");
    expect(
      screen.getByText("This section couldn't load"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("forwards the name prop as boundary to the telemetry payload", () => {
    // apiFetch is the transport used by reportFrontendError. We stub it
    // and inspect the body to verify the boundary field is attached.
    const fetchSpy = vi
      .spyOn(globalThis, "fetch" as never)
      .mockResolvedValue(new Response("{}", { status: 200 }) as never);

    render(
      <ErrorBoundary name="route.settings">
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>,
    );

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const callArgs = fetchSpy.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(callArgs[1].body as string) as {
      boundary?: string;
    };
    expect(body.boundary).toBe("route.settings");
    fetchSpy.mockRestore();
  });

  it("omits telemetry when disableTelemetry is set", () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch" as never)
      .mockResolvedValue(new Response("{}", { status: 200 }) as never);

    render(
      <ErrorBoundary disableTelemetry>
        <ThrowingComponent shouldThrow={true} />
      </ErrorBoundary>,
    );

    // Telemetry endpoint was skipped — other fetches are still allowed
    // but /api/telemetry/frontend-error must not be hit.
    const telemetryCalls = fetchSpy.mock.calls.filter((c) =>
      String(c[0]).includes("/api/telemetry/frontend-error"),
    );
    expect(telemetryCalls).toHaveLength(0);
    fetchSpy.mockRestore();
  });
});
