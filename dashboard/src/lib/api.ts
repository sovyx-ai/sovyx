/**
 * Sovyx Dashboard — API client
 * Thin fetch wrapper with Bearer auth and error handling.
 */

const BASE_URL = import.meta.env.VITE_API_URL ?? "";

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

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options.headers as Record<string, string>) ?? {}),
  };

  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers,
  });

  if (!response.ok) {
    // 401 → clear token, show auth modal
    if (response.status === 401) {
      clearToken();
      // Lazy import to avoid circular deps
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
  get: <T>(path: string) => request<T>(path),

  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),

  put: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
    }),

  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
