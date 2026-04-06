/**
 * Test utilities — wrapper with MemoryRouter + i18n for page tests.
 *
 * POLISH-16: Shared test harness for all page components.
 */

import { type ReactNode } from "react";
import { render, type RenderOptions } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import "@/lib/i18n"; // Init i18n with all namespaces

interface WrapperProps {
  children: ReactNode;
}

/** Wraps component in MemoryRouter (required for pages using useLocation, Link, etc). */
function AllProviders({ children }: WrapperProps) {
  return <MemoryRouter>{children}</MemoryRouter>;
}

/** Custom render with providers. */
function customRender(ui: React.ReactElement, options?: Omit<RenderOptions, "wrapper">) {
  return render(ui, { wrapper: AllProviders, ...options });
}

// Re-export everything from testing-library
export * from "@testing-library/react";
export { customRender as render };
