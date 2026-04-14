import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import {
  PluginToolBadge,
  PermissionBadge,
  CategoryBadge,
  PricingBadge,
} from "./plugin-badges";

describe("PluginToolBadge", () => {
  it("renders the tool name", () => {
    render(<PluginToolBadge name="send_email" />);
    expect(screen.getByText("send_email")).toBeInTheDocument();
  });

  it("uses the description as the tooltip title", () => {
    const { container } = render(
      <PluginToolBadge name="send_email" description="Send a transactional email" />,
    );
    const badge = container.firstChild as HTMLElement;
    expect(badge.title).toBe("Send a transactional email");
  });
});

describe("PermissionBadge", () => {
  it("renders the permission name and a risk dot", () => {
    const { container } = render(
      <PermissionBadge permission="network:internet" risk="high" />,
    );
    expect(screen.getByText("network:internet")).toBeInTheDocument();
    expect(container.querySelector("span[aria-hidden='true']")).not.toBeNull();
  });
});

describe("CategoryBadge", () => {
  it("renders with a known category icon", () => {
    render(<CategoryBadge category="finance" />);
    expect(screen.getByText("finance")).toBeInTheDocument();
  });

  it("returns null for empty category", () => {
    const { container } = render(<CategoryBadge category="" />);
    expect(container.firstChild).toBeNull();
  });
});

describe("PricingBadge", () => {
  it("renders a label for known pricing", () => {
    const { container } = render(<PricingBadge pricing="free" />);
    // Label is i18n'd but something must render.
    expect(container.firstChild).not.toBeNull();
    expect((container.firstChild as HTMLElement).textContent).not.toBe("");
  });

  it("falls back to capitalized pricing for unknown values", () => {
    render(<PricingBadge pricing="enterprise" />);
    // Translation fallback uses capitalized value as default — safest check
    // is that the raw or capitalized string appears somewhere.
    expect(
      screen.queryByText(/enterprise/i),
    ).toBeInTheDocument();
  });
});
