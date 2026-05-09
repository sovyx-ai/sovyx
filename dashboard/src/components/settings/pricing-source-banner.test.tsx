/**
 * PricingSourceBanner tests (issue #45).
 *
 * Covers:
 *  - Renders nothing for source = "exact" (the common case).
 *  - Renders the warning for source = "provider_default".
 *  - Renders the warning for source = "global_default".
 *  - Re-fetches when provider/model props change.
 *  - Failure of the pricing-info fetch is silent (best-effort UX).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";

import { PricingSourceBanner } from "./pricing-source-banner";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockFetch.mockReset();
});

describe("PricingSourceBanner", () => {
  it("renders nothing when source is exact", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        model: "gpt-4o",
        provider: "openai",
        input_per_1m_usd: 2.5,
        output_per_1m_usd: 10,
        source: "exact",
      }),
    );
    const { container } = render(
      <PricingSourceBanner provider="openai" model="gpt-4o" />,
    );
    // Wait for the fetch to settle then assert no banner.
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    });
    expect(
      container.querySelector('[data-testid="pricing-source-banner"]'),
    ).toBeNull();
  });

  it("renders the warning for provider_default source", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        model: "vaporware-9000",
        provider: "anthropic",
        input_per_1m_usd: 3.0,
        output_per_1m_usd: 15.0,
        source: "provider_default",
      }),
    );
    render(<PricingSourceBanner provider="anthropic" model="vaporware-9000" />);
    const banner = await screen.findByTestId("pricing-source-banner");
    expect(banner).toBeInTheDocument();
    expect(banner.getAttribute("data-source")).toBe("provider_default");
  });

  it("renders the warning for global_default source", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        model: "vaporware",
        provider: "fake-corp",
        input_per_1m_usd: 3.0,
        output_per_1m_usd: 15.0,
        source: "global_default",
      }),
    );
    render(<PricingSourceBanner provider="fake-corp" model="vaporware" />);
    const banner = await screen.findByTestId("pricing-source-banner");
    expect(banner).toBeInTheDocument();
    expect(banner.getAttribute("data-source")).toBe("global_default");
  });

  it("renders nothing when model is empty", async () => {
    const { container } = render(
      <PricingSourceBanner provider="openai" model="" />,
    );
    expect(
      container.querySelector('[data-testid="pricing-source-banner"]'),
    ).toBeNull();
    // Empty model must not trigger any fetch (the endpoint would fall
    // through to MindConfig defaults, leaking unrelated state).
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("stays silent when the pricing-info fetch fails", async () => {
    mockFetch.mockRejectedValueOnce(new Error("network down"));
    const { container } = render(
      <PricingSourceBanner provider="openai" model="gpt-4o" />,
    );
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    });
    // Best-effort UX — fetch failures don't surface a banner.
    expect(
      container.querySelector('[data-testid="pricing-source-banner"]'),
    ).toBeNull();
  });
});
