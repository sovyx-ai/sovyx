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

  it("shows fresh-engine state when all metrics are zero", () => {
    useDashboardStore.setState({
      status: {
        version: "0.5.0",
        uptime_seconds: 10,
        mind_name: "FreshMind",
        active_conversations: 0,
        memory_concepts: 0,
        memory_episodes: 0,
        llm_calls_today: 0,
        llm_cost_today: 0,
        tokens_today: 0,
        messages_today: 0,
      },
      connected: true,
    });
    render(<OverviewPage />);
    // Should show fresh subtitle
    expect(screen.getByText(/bring it to life/i)).toBeInTheDocument();
    // Messages and LLM Cost cards show dash instead of "0"
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBe(2);
    // Brain card should show "Empty"
    expect(screen.getByText("Empty")).toBeInTheDocument();
    // Should show contextual hints
    expect(screen.getByText(/awaiting first message/i)).toBeInTheDocument();
    expect(screen.getByText(/learns from conversation/i)).toBeInTheDocument();
  });

  it("shows normal values when engine has activity", () => {
    // Dismiss onboarding to avoid duplicate metric text from MindAliveCard
    useDashboardStore.setState({ onboardingDismissed: true });
    render(<OverviewPage />);
    // Should show normal subtitle
    expect(screen.getByText(/at a glance/i)).toBeInTheDocument();
    // Should show actual numbers, not fresh labels
    expect(screen.getByText("5")).toBeInTheDocument(); // messages
    expect(screen.getByText("150")).toBeInTheDocument(); // concepts
  });

  it("shows skeletons when status is null", () => {
    useDashboardStore.setState({ status: null });
    render(<OverviewPage />);
    const skeletons = screen.getAllByRole("group", { name: "Loading" });
    expect(skeletons).toHaveLength(4);
  });
});
