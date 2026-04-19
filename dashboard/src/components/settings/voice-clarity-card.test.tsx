/**
 * VoiceClarityCard tests — Voice Clarity APO detection + one-click bypass.
 *
 * Covers: loading → no-endpoints hidden on non-Windows, clarity-active
 * alert render, successful enable (hot-applied), enable-with-restart,
 * network failure handling, benign "no-issues" message.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@/test/test-utils";
import { VoiceClarityCard } from "./voice-clarity-card";
import type { CaptureDiagnosticsResponse } from "@/types/api";

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

function diagPayload(
  overrides: Partial<CaptureDiagnosticsResponse> = {},
): CaptureDiagnosticsResponse {
  return {
    platform_supported: true,
    active_device_name: "Microfone (Razer BlackShark V2 Pro)",
    active_endpoint: {
      endpoint_id: "{active}",
      endpoint_name: "Microfone (Razer BlackShark V2 Pro)",
      known_apos: ["Windows Voice Clarity"],
      voice_clarity_active: true,
    },
    voice_clarity_active: true,
    any_voice_clarity_active: true,
    endpoints: [
      {
        endpoint_id: "{active}",
        endpoint_name: "Microfone (Razer BlackShark V2 Pro)",
        enumerator: "USB",
        fx_binding_count: 4,
        known_apos: ["Windows Voice Clarity"],
        raw_clsids: [],
        voice_clarity_active: true,
        is_active_device: true,
      },
    ],
    fix_suggestion: "Enable WASAPI exclusive mode.",
    ...overrides,
  };
}

beforeEach(() => {
  mockFetch.mockReset();
  mockToastSuccess.mockReset();
  mockToastError.mockReset();
  sessionStorage.clear();
});

describe("VoiceClarityCard", () => {
  it("renders nothing when no endpoints (non-Windows)", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        diagPayload({
          endpoints: [],
          voice_clarity_active: false,
          any_voice_clarity_active: false,
          active_endpoint: null,
          active_device_name: null,
        }),
      ),
    );
    const { container } = render(<VoiceClarityCard />);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="voice-clarity-card"]')).toBeNull();
    });
  });

  it("shows the clarity alert when detected on active device", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(diagPayload()));
    render(<VoiceClarityCard />);
    await waitFor(() => {
      expect(screen.getByTestId("voice-clarity-alert")).toBeInTheDocument();
    });
    expect(screen.getByText(/Voice Clarity detected/i)).toBeInTheDocument();
    expect(screen.getByText(/Razer BlackShark V2 Pro/)).toBeInTheDocument();
    expect(screen.getByText(/Detected APOs: Windows Voice Clarity/)).toBeInTheDocument();
  });

  it("shows a benign 'no issues' message when clarity inactive", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        diagPayload({
          voice_clarity_active: false,
          any_voice_clarity_active: false,
          active_endpoint: {
            endpoint_id: "{active}",
            endpoint_name: "Built-in Microphone",
            known_apos: [],
            voice_clarity_active: false,
          },
          active_device_name: "Built-in Microphone",
          endpoints: [
            {
              endpoint_id: "{active}",
              endpoint_name: "Built-in Microphone",
              enumerator: "MMDevAPI",
              fx_binding_count: 1,
              known_apos: [],
              raw_clsids: [],
              voice_clarity_active: false,
              is_active_device: true,
            },
          ],
          fix_suggestion: null,
        }),
      ),
    );
    render(<VoiceClarityCard />);
    await waitFor(() => {
      expect(screen.getByText(/No capture-APO issues/i)).toBeInTheDocument();
    });
  });

  it("POSTs to capture-exclusive and toasts success on hot-apply", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse(diagPayload()))
      .mockResolvedValueOnce(
        jsonResponse({
          ok: true,
          enabled: true,
          persisted: true,
          applied_immediately: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          diagPayload({
            voice_clarity_active: false,
            any_voice_clarity_active: false,
          }),
        ),
      );

    render(<VoiceClarityCard />);
    await waitFor(() => {
      expect(screen.getByTestId("enable-exclusive-button")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("enable-exclusive-button"));
    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalledWith(
        "Exclusive mode enabled and applied immediately.",
      );
    });

    // Verify the POST body was { enabled: true }
    const postCall = mockFetch.mock.calls.find(
      (c) => c[1]?.method === "POST",
    );
    expect(postCall).toBeDefined();
    expect(JSON.parse(postCall![1].body as string)).toEqual({ enabled: true });
  });

  it("toasts 'needs restart' when persisted but not applied immediately", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse(diagPayload()))
      .mockResolvedValueOnce(
        jsonResponse({
          ok: true,
          enabled: true,
          persisted: true,
          applied_immediately: false,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(diagPayload()));

    render(<VoiceClarityCard />);
    await waitFor(() => {
      expect(screen.getByTestId("enable-exclusive-button")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("enable-exclusive-button"));
    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalledWith(
        "Exclusive mode saved. Restart the pipeline to apply.",
      );
    });
  });

  it("toasts error on POST failure", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse(diagPayload()))
      .mockResolvedValueOnce(jsonResponse({ error: "boom" }, 500));

    render(<VoiceClarityCard />);
    await waitFor(() => {
      expect(screen.getByTestId("enable-exclusive-button")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("enable-exclusive-button"));
    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalled();
    });
  });
});
