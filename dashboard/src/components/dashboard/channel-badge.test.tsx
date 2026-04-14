import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChannelBadge } from "./channel-badge";

describe("ChannelBadge", () => {
  it("renders known channel with friendly label", () => {
    render(<ChannelBadge channel="telegram" />);
    expect(screen.getByText(/Telegram/)).toBeInTheDocument();
  });

  it("is case-insensitive for known channels", () => {
    render(<ChannelBadge channel="TELEGRAM" />);
    expect(screen.getByText(/Telegram/)).toBeInTheDocument();
  });

  it("falls back to the raw channel label for unknown channels", () => {
    render(<ChannelBadge channel="matrix" />);
    expect(screen.getByText(/matrix/)).toBeInTheDocument();
  });

  it("applies a title attribute with the channel label for tooltips", () => {
    const { container } = render(<ChannelBadge channel="signal" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.title).toBe("Signal");
  });
});
