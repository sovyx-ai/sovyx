import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import "@/lib/i18n";
import { PluginCard } from "./plugin-card";
import type { PluginInfo } from "@/types/api";

function makePlugin(overrides: Partial<PluginInfo> = {}): PluginInfo {
  return {
    name: "finance",
    version: "1.2.3",
    description: "Track expenses",
    status: "active",
    tools_count: 4,
    tools: [],
    permissions: [],
    health: { consecutive_failures: 0, disabled: false, last_error: "", active_tasks: 0 },
    category: "finance",
    tags: [],
    icon_url: "",
    pricing: "free",
    ...overrides,
  };
}

// Minimal store mock — PluginCard uses enablePlugin/disablePlugin/reloadPlugin
// from the store inside QuickActions. Tests here focus on the card itself.
vi.mock("@/stores/dashboard", () => ({
  useDashboardStore: (selector: (s: Record<string, unknown>) => unknown) => {
    const state: Record<string, unknown> = {
      enablePlugin: vi.fn(async () => true),
      disablePlugin: vi.fn(async () => true),
      reloadPlugin: vi.fn(async () => true),
    };
    return typeof selector === "function" ? selector(state) : state;
  },
}));

describe("PluginCard", () => {
  it("renders plugin name, version and description", () => {
    render(<PluginCard plugin={makePlugin()} />);
    // Plugin name appears both as heading and as category label — query the heading
    expect(screen.getByRole("heading", { name: "finance" })).toBeInTheDocument();
    expect(screen.getByText("v1.2.3")).toBeInTheDocument();
    expect(screen.getByText("Track expenses")).toBeInTheDocument();
  });

  it("fires onClick when the card is clicked", () => {
    const onClick = vi.fn();
    render(<PluginCard plugin={makePlugin()} onClick={onClick} />);
    fireEvent.click(screen.getByRole("article"));
    expect(onClick).toHaveBeenCalled();
  });

  it("shows the auto-disable warning when health has consecutive failures", () => {
    const healthy = makePlugin();
    const unhealthy = makePlugin({
      health: { consecutive_failures: 3, disabled: false, last_error: "", active_tasks: 0 },
    });
    const { container: ok } = render(<PluginCard plugin={healthy} />);
    const { container: bad } = render(<PluginCard plugin={unhealthy} />);
    // The warning box uses a bg-warning class; healthy has no such extra block
    const okWarn = ok.querySelectorAll("div[class*='svx-color-warning']");
    const badWarn = bad.querySelectorAll("div[class*='svx-color-warning']");
    expect(badWarn.length).toBeGreaterThan(okWarn.length);
  });
});
