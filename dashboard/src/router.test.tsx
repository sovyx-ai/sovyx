/**
 * Router test — exercises the lazy-loaded route + ErrorBoundary +
 * Suspense fallback wiring without spinning up the full AppLayout
 * (which would pull every page into the test bundle).
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Suspense, lazy } from "react";
import { ErrorBoundary } from "@/components/error-boundary";

// Reuse the exact PageWrapper semantics from router.tsx locally so we
// can exercise them without pulling in createBrowserRouter (which
// requires a real history).
function PageWrapper({ children }: { children: React.ReactNode }) {
  return (
    <ErrorBoundary>
      <Suspense fallback={<div data-testid="route-fallback">Loading…</div>}>
        {children}
      </Suspense>
    </ErrorBoundary>
  );
}

describe("router page wrapper", () => {
  it("shows the Suspense fallback while the lazy chunk resolves", async () => {
    let resolveLazy: (value: { default: React.ComponentType }) => void = () => {};
    const Lazy = lazy(
      () =>
        new Promise<{ default: React.ComponentType }>((resolve) => {
          resolveLazy = resolve;
        }),
    );

    render(
      <PageWrapper>
        <Lazy />
      </PageWrapper>,
    );
    expect(screen.getByTestId("route-fallback")).toBeInTheDocument();

    resolveLazy({ default: () => <div>resolved page</div> });
    expect(await screen.findByText("resolved page")).toBeInTheDocument();
  });

  it("catches render errors from a lazy page via the ErrorBoundary", async () => {
    const Explode = lazy(() =>
      Promise.resolve({
        default: function Explode(): React.ReactElement {
          throw new Error("boom");
        },
      }),
    );

    // Suppress the expected React error log so the test output stays clean.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <PageWrapper>
        <Explode />
      </PageWrapper>,
    );
    // ErrorBoundary i18n'd fallback shows the "Try again" button
    expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
    spy.mockRestore();
  });
});

// vi import for mockImplementation used above
import { vi } from "vitest";
