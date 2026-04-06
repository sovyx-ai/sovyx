/**
 * Sovyx Dashboard — API client
 *
 * Thin fetch wrapper with:
 * - Bearer auth (token from localStorage)
 * - 401 → clear token + show auth modal
 * - AbortSignal support for cancellation
 * - Content-Type only on requests with body (POLISH-12 fix)
 * - Typed ApiError for non-2xx responses
 *
 * Ref: POLISH-01, POLISH-12
 */

export const BASE_URL = import.meta.env.VITE_API_URL ?? "";

function getToken(): string | null {
  return localStorage.getItem("sovyx_token");
}

export function setToken(token: string): void {
  localStorage.setItem("sovyx_token", token);
}

export function clearToken(): void {
  localStorage.removeItem("sovyx_token");
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Check if an error is from an aborted fetch (not a real error). */
export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    ...((options.headers as Record<string, string>) ?? {}),
  };

  // Only set Content-Type when there's a body (POLISH-12: not on GET/DELETE)
  if (options.body) {
    headers["Content-Type"] = "application/json";
  }

  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers,
  });

  if (!response.ok) {
    if (response.status === 401) {
      clearToken();
      const { useDashboardStore } = await import("@/stores/dashboard");
      useDashboardStore.getState().setAuthenticated(false);
      useDashboardStore.getState().setShowTokenModal(true);
    }
    const body = await response.text().catch(() => "Unknown error");
    throw new ApiError(response.status, body);
  }

  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string, options?: RequestInit) =>
    request<T>(path, options),

  post: <T>(path: string, body?: unknown, options?: RequestInit) =>
    request<T>(path, {
      ...options,
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),

  put: <T>(path: string, body?: unknown, options?: RequestInit) =>
    request<T>(path, {
      ...options,
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
    }),

  delete: <T>(path: string, options?: RequestInit) =>
    request<T>(path, { ...options, method: "DELETE" }),
};
