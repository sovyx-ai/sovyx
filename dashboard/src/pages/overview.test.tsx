/**
 * Overview page tests — POLISH-16.
 *
 * Tests that overview renders stat cards and sections from store data.
 */

import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@/test/test-utils";
import OverviewPage from "./overview";
import { useDashboardStore } from "@/stores/dashboard";

beforeEach(() => {
  // Seed store with mock status
  useDashboardStore.setState({
    status: {
      version: "0.5.0",
      uptime_seconds: 3600,
      mind_name: "TestMind",
      active_conversations: 3,
      memory_concepts: 150,
      memory_episodes: 42,
      llm_calls_today: 88,
      llm_cost_today: 0.42,
      tokens_today: 12000,
      messages_today: 5,
    },
    healthChecks: [
      { name: "database", status: "green", latency_ms: 2, message: "OK" },
      { name: "llm_provider", status: "green", latency_ms: 50, message: "OK" },
    ],
  });
});

describe("OverviewPage", () => {
  it("renders the overview heading", () => {
    render(<OverviewPage />);
    // i18n key "title" in overview namespace
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  });

  it("renders stat cards section", () => {
    render(<OverviewPage />);
    // Concepts count should be in the page
    expect(screen.getByText("150")).toBeInTheDocument();
  });

  it("renders health grid section", () => {
    render(<OverviewPage />);
    // Health checks should be visible
    expect(screen.getByText(/database/i)).toBeInTheDocument();
  });
});
