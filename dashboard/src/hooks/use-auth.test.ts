/**
 * Tests for useAuth hook.
 *
 * Covers token validation, auth state management, and error handling.
 * Storage is `sessionStorage` (token persists within a tab, not across
 * tabs/restarts) after the P0 XSS-hardening.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { useAuth } from "./use-auth";
import { useDashboardStore } from "@/stores/dashboard";

// Reset store + storages between tests. Clearing both avoids leakage
// from the one-shot localStorage → sessionStorage legacy migration.
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  useDashboardStore.setState({
    authenticated: false,
    showTokenModal: false,
  });
  vi.restoreAllMocks();
});

describe("useAuth", () => {
  it("shows token modal when no token in storage", async () => {
    renderHook(() => useAuth());

    await waitFor(() => {
      expect(useDashboardStore.getState().showTokenModal).toBe(true);
    });
    expect(useDashboardStore.getState().authenticated).toBe(false);
  });

  it("validates existing token against /api/status — success", async () => {
    sessionStorage.setItem("sovyx_token", "valid-token");

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ version: "1.0" }), { status: 200 }),
    );

    renderHook(() => useAuth());

    await waitFor(() => {
      expect(useDashboardStore.getState().authenticated).toBe(true);
    });
    expect(useDashboardStore.getState().showTokenModal).toBe(false);
  });

  it("clears token and shows modal when /api/status returns 401", async () => {
    sessionStorage.setItem("sovyx_token", "expired-token");

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("Unauthorized", { status: 401 }),
    );

    renderHook(() => useAuth());

    await waitFor(() => {
      expect(useDashboardStore.getState().showTokenModal).toBe(true);
    });
    expect(sessionStorage.getItem("sovyx_token")).toBeNull();
    expect(useDashboardStore.getState().authenticated).toBe(false);
  });

  it("clears token and shows modal on 403", async () => {
    sessionStorage.setItem("sovyx_token", "bad-token");

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("Forbidden", { status: 403 }),
    );

    renderHook(() => useAuth());

    await waitFor(() => {
      expect(useDashboardStore.getState().showTokenModal).toBe(true);
    });
    expect(sessionStorage.getItem("sovyx_token")).toBeNull();
  });

  it("fails closed on network error (server unreachable)", async () => {
    // Previous fail-open behaviour trusted the existing token when the
    // server was unreachable — letting stale or compromised tokens bypass
    // revalidation. Fail-closed: clear token and force re-entry.
    sessionStorage.setItem("sovyx_token", "some-token");

    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(
      new TypeError("Failed to fetch"),
    );

    renderHook(() => useAuth());

    await waitFor(() => {
      expect(useDashboardStore.getState().showTokenModal).toBe(true);
    });
    expect(useDashboardStore.getState().authenticated).toBe(false);
    expect(sessionStorage.getItem("sovyx_token")).toBeNull();
  });

  it("returns ready=false initially, then true after auth", async () => {
    sessionStorage.setItem("sovyx_token", "valid-token");

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("{}", { status: 200 }),
    );

    const { result } = renderHook(() => useAuth());

    // Initially not ready
    expect(result.current.ready).toBe(false);

    await waitFor(() => {
      expect(result.current.ready).toBe(true);
    });
  });

  it("sends Authorization header with Bearer token", async () => {
    sessionStorage.setItem("sovyx_token", "my-secret-token");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("{}", { status: 200 }),
    );

    renderHook(() => useAuth());

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalled();
    });

    const [, options] = fetchSpy.mock.calls[0]!;
    const headers = options?.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer my-secret-token");
  });
});
