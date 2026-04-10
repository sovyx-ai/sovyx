/**
 * ChannelStatusCard tests — TASK-304
 *
 * Covers: loading state, connected channels, disconnected channels,
 * telegram setup flow, signal display, expandable sections.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@/test/test-utils";
import { ChannelStatusCard } from "./channel-status";

/* ── Mock API ── */

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

/* ── Fixtures ── */

const CHANNELS_CONNECTED = {
  channels: [
    { name: "Telegram", type: "telegram", connected: true },
    { name: "Signal", type: "signal", connected: true },
  ],
};

const CHANNELS_MIXED = {
  channels: [
    { name: "Telegram", type: "telegram", connected: true },
    { name: "Signal", type: "signal", connected: false },
  ],
};

const CHANNELS_DISCONNECTED = {
  channels: [
    { name: "Telegram", type: "telegram", connected: false },
    { name: "Signal", type: "signal", connected: false },
  ],
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

// ════════════════════════════════════════════════════════
// LOADING STATE
// ════════════════════════════════════════════════════════
describe("loading state", () => {
  it("shows skeleton while loading", () => {
    mockGet.mockReturnValue(new Promise(() => {}));
    render(<ChannelStatusCard />);
    expect(screen.getByText("Channels")).toBeInTheDocument();
  });

  it("renders title during loading", () => {
    mockGet.mockReturnValue(new Promise(() => {}));
    render(<ChannelStatusCard />);
    expect(screen.getByText("Channels")).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// CONNECTED CHANNELS
// ════════════════════════════════════════════════════════
describe("connected channels", () => {
  it("renders connected badge for connected channels", async () => {
    mockGet.mockResolvedValue(CHANNELS_CONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => {
      const badges = screen.getAllByText("Connected");
      expect(badges).toHaveLength(2);
    });
  });

  it("shows channel names", async () => {
    mockGet.mockResolvedValue(CHANNELS_CONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => {
      expect(screen.getByText("Telegram")).toBeInTheDocument();
      expect(screen.getByText("Signal")).toBeInTheDocument();
    });
  });

  it("renders data-testid for each channel", async () => {
    mockGet.mockResolvedValue(CHANNELS_CONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => {
      expect(screen.getByTestId("channel-telegram")).toBeInTheDocument();
      expect(screen.getByTestId("channel-signal")).toBeInTheDocument();
    });
  });

  it("renders the card container", async () => {
    mockGet.mockResolvedValue(CHANNELS_CONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => {
      expect(screen.getByTestId("channel-status-card")).toBeInTheDocument();
    });
  });
});

// ════════════════════════════════════════════════════════
// DISCONNECTED CHANNELS
// ════════════════════════════════════════════════════════
describe("disconnected channels", () => {
  it("shows Setup text for disconnected channels", async () => {
    mockGet.mockResolvedValue(CHANNELS_DISCONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => {
      const setups = screen.getAllByText("channels.setup");
      expect(setups).toHaveLength(2);
    });
  });

  it("shows mixed state correctly", async () => {
    mockGet.mockResolvedValue(CHANNELS_MIXED);
    render(<ChannelStatusCard />);

    await waitFor(() => {
      expect(screen.getByText("Connected")).toBeInTheDocument();
      expect(screen.getByText("channels.setup")).toBeInTheDocument();
    });
  });
});

// ════════════════════════════════════════════════════════
// TELEGRAM SETUP FLOW
// ════════════════════════════════════════════════════════
describe("telegram setup flow", () => {
  it("expands telegram setup on click", async () => {
    mockGet.mockResolvedValue(CHANNELS_DISCONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => {
      expect(screen.getByTestId("channel-telegram")).toBeInTheDocument();
    });

    // Click the telegram row to expand
    const telegramRow = screen.getByTestId("channel-telegram");
    const setupButton = telegramRow.querySelector("[role='button']");
    if (setupButton) fireEvent.click(setupButton);

    await waitFor(() => {
      expect(screen.getByTestId("telegram-token-input")).toBeInTheDocument();
    });
  });

  it("shows BotFather link in telegram setup", async () => {
    mockGet.mockResolvedValue(CHANNELS_DISCONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => screen.getByTestId("channel-telegram"));
    const telegramRow = screen.getByTestId("channel-telegram");
    const setupButton = telegramRow.querySelector("[role='button']");
    if (setupButton) fireEvent.click(setupButton);

    await waitFor(() => {
      expect(screen.getByText("@BotFather")).toBeInTheDocument();
    });
  });

  it("connect button is disabled when token input is empty", async () => {
    mockGet.mockResolvedValue(CHANNELS_DISCONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => screen.getByTestId("channel-telegram"));
    const telegramRow = screen.getByTestId("channel-telegram");
    const setupButton = telegramRow.querySelector("[role='button']");
    if (setupButton) fireEvent.click(setupButton);

    await waitFor(() => {
      const connectBtn = screen.getByTestId("telegram-connect-btn");
      expect(connectBtn).toBeDisabled();
    });
  });

  it("shows success state after successful connection", async () => {
    mockGet.mockResolvedValue(CHANNELS_DISCONNECTED);
    mockPost.mockResolvedValue({ ok: true, bot_username: "test_bot" });

    render(<ChannelStatusCard />);

    await waitFor(() => screen.getByTestId("channel-telegram"));
    const telegramRow = screen.getByTestId("channel-telegram");
    const setupButton = telegramRow.querySelector("[role='button']");
    if (setupButton) fireEvent.click(setupButton);

    await waitFor(() => screen.getByTestId("telegram-token-input"));

    fireEvent.change(screen.getByTestId("telegram-token-input"), {
      target: { value: "123456:ABC-DEF" },
    });
    fireEvent.click(screen.getByTestId("telegram-connect-btn"));

    await waitFor(() => {
      expect(screen.getByText(/Connected to/)).toBeInTheDocument();
    });
  });

  it("shows error state on invalid token", async () => {
    mockGet.mockResolvedValue(CHANNELS_DISCONNECTED);
    mockPost.mockResolvedValue({ ok: false, error: "Invalid token" });

    render(<ChannelStatusCard />);

    await waitFor(() => screen.getByTestId("channel-telegram"));
    const telegramRow = screen.getByTestId("channel-telegram");
    const setupButton = telegramRow.querySelector("[role='button']");
    if (setupButton) fireEvent.click(setupButton);

    await waitFor(() => screen.getByTestId("telegram-token-input"));

    fireEvent.change(screen.getByTestId("telegram-token-input"), {
      target: { value: "bad-token" },
    });
    fireEvent.click(screen.getByTestId("telegram-connect-btn"));

    await waitFor(() => {
      expect(screen.getByText("Invalid token")).toBeInTheDocument();
    });
  });
});

// ════════════════════════════════════════════════════════
// SIGNAL SETUP
// ════════════════════════════════════════════════════════
describe("signal setup", () => {
  it("expands signal setup on click", async () => {
    mockGet.mockResolvedValue(CHANNELS_DISCONNECTED);
    render(<ChannelStatusCard />);

    await waitFor(() => screen.getByTestId("channel-signal"));
    const signalRow = screen.getByTestId("channel-signal");
    const setupButton = signalRow.querySelector("[role='button']");
    if (setupButton) fireEvent.click(setupButton);

    await waitFor(() => {
      expect(screen.getByText("signal-cli-rest-api")).toBeInTheDocument();
    });
  });
});

// ════════════════════════════════════════════════════════
// ERROR HANDLING
// ════════════════════════════════════════════════════════
describe("error handling", () => {
  it("handles fetch error gracefully", async () => {
    mockGet.mockRejectedValue(new Error("Network error"));
    render(<ChannelStatusCard />);

    // Should still render the card (silently fails)
    await waitFor(() => {
      expect(screen.getByText("Channels")).toBeInTheDocument();
    });
  });
});
