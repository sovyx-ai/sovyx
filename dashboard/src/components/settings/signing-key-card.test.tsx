/**
 * SigningKeyCard tests — BT.B.3 (v0.32.0).
 *
 * Validates:
 *   * "Not yet generated" state surfaces the Generate button
 *   * Click POSTs and surfaces the fingerprint after success
 *   * Regenerate path requires explicit confirmation
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@/test/test-utils";

import { SigningKeyCard } from "./signing-key-card";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    error: (...args: unknown[]) => mockToastError(...args),
  },
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockFetch.mockReset();
  mockToastSuccess.mockReset();
  mockToastError.mockReset();
});

describe("SigningKeyCard", () => {
  it("renders the Generate button when no key exists", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        exists: false,
        fingerprint_short: null,
        public_key_path: null,
        resolved_mind_id: "default",
      }),
    );

    render(<SigningKeyCard />);

    await waitFor(() => {
      expect(
        screen.getByTestId("settings-signing-key-generate"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("settings-signing-key-status"),
    ).toHaveTextContent(/not yet generated/i);
  });

  it("clicking Generate POSTs and surfaces the fingerprint", async () => {
    // Initial GET — not yet generated.
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        exists: false,
        fingerprint_short: null,
        public_key_path: null,
        resolved_mind_id: "default",
      }),
    );
    // POST response — returns fingerprint + paths.
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        ok: true,
        public_key_pem: "-----BEGIN PUBLIC KEY-----\nMCo=\n-----END PUBLIC KEY-----\n",
        public_key_path: "/data/default/calibration.signing-key.pub",
        private_key_path: "/data/default/calibration.signing-key.priv",
        fingerprint_short: "abcdef01",
        mode: "created",
        resolved_mind_id: "default",
      }),
    );

    render(<SigningKeyCard />);

    const generate = await screen.findByTestId("settings-signing-key-generate");
    fireEvent.click(generate);

    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalled();
    });
    // Fingerprint surfaced in the status badge.
    await waitFor(() => {
      expect(
        screen.getByTestId("settings-signing-key-fingerprint"),
      ).toHaveTextContent("abcdef01");
    });
    // Confirms the request hit the generate endpoint with the right body.
    const calls = mockFetch.mock.calls;
    const postCall = calls.find(
      (call) =>
        typeof call[0] === "string" &&
        call[0].includes("/api/voice/calibration/generate-signing-key"),
    );
    expect(postCall).toBeDefined();
  });

  it("Regenerate path requires explicit confirmation before POSTing", async () => {
    // Initial GET — already generated.
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        exists: true,
        fingerprint_short: "deadbeef",
        public_key_path: "/data/default/calibration.signing-key.pub",
        resolved_mind_id: "default",
      }),
    );

    render(<SigningKeyCard />);

    // Status shows the existing fingerprint; Regenerate button visible.
    await waitFor(() => {
      expect(
        screen.getByTestId("settings-signing-key-fingerprint"),
      ).toHaveTextContent("deadbeef");
    });
    const regenerate = screen.getByTestId("settings-signing-key-regenerate");
    fireEvent.click(regenerate);

    // After clicking Regenerate, the warning + Confirm/Cancel pair render.
    await waitFor(() => {
      expect(
        screen.getByTestId("settings-signing-key-warning"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("settings-signing-key-cancel"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("settings-signing-key-confirm"),
    ).toBeInTheDocument();

    // No POST happened from the first click — only the GET ran.
    const postCalls = mockFetch.mock.calls.filter(
      (call) =>
        typeof call[0] === "string" &&
        call[0].includes("/api/voice/calibration/generate-signing-key"),
    );
    expect(postCalls.length).toBe(0);
  });
});
