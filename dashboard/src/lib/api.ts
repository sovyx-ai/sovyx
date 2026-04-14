/**
 * Sovyx Dashboard — API client
 *
 * Thin fetch wrapper with:
 * - Bearer auth (token kept in `sessionStorage` + in-memory fallback;
 *   never in `localStorage`, to reduce XSS token-theft blast radius)
 * - 401 → clear token + show auth modal
 * - AbortSignal support for cancellation
 * - Content-Type only on requests with body
 * - Typed ApiError for non-2xx responses
 * - Optional zod schema validation on response body (safeParse + warn —
 *   catches backend contract drift without hard-failing production)
 */

import type { ZodType } from "zod";

export const BASE_URL = import.meta.env.VITE_API_URL ?? "";

const TOKEN_STORAGE_KEY = "sovyx_token";
/** Legacy keys that may hold a token from pre-hardening builds. */
const LEGACY_STORAGE_KEYS = ["sovyx_token"] as const;

// In-memory fallback — survives within the tab lifetime when sessionStorage
// is disabled (e.g. privacy mode, embedded contexts).
let memoryToken: string | null = null;

function getSessionStorage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

/** Pull any token left in `localStorage` by old builds into sessionStorage. */
function migrateLegacyToken(): void {
  try {
    for (const key of LEGACY_STORAGE_KEYS) {
      const legacy = window.localStorage?.getItem(key);
      if (legacy) {
        getSessionStorage()?.setItem(TOKEN_STORAGE_KEY, legacy);
        window.localStorage.removeItem(key);
      }
    }
  } catch {
    // localStorage unavailable — nothing to migrate.
  }
}
migrateLegacyToken();

function getToken(): string | null {
  const stored = getSessionStorage()?.getItem(TOKEN_STORAGE_KEY) ?? null;
  return stored ?? memoryToken;
}

export function setToken(token: string): void {
  memoryToken = token;
  getSessionStorage()?.setItem(TOKEN_STORAGE_KEY, token);
}

export function clearToken(): void {
  memoryToken = null;
  getSessionStorage()?.removeItem(TOKEN_STORAGE_KEY);
  // Also scrub any lingering legacy localStorage entry.
  try {
    for (const key of LEGACY_STORAGE_KEYS) {
      window.localStorage?.removeItem(key);
    }
  } catch {
    // ignore
  }
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

/**
 * Extra options accepted by every api.* method on top of RequestInit.
 *
 * `schema` is optional: when present, the response JSON is validated
 * through `schema.safeParse(...)`. On mismatch we log a console warning
 * with the issue list (visible in staging / dev tools) but still return
 * the raw payload — the backend is the source of truth and feature
 * failure from a single added field would be worse than a quiet warning.
 */
export interface ApiOptions extends RequestInit {
  schema?: ZodType;
}

/**
 * Low-level fetch wrapper that injects the current auth token into the
 * request headers without parsing the response. Use this when you need
 * the raw `Response` — e.g. for binary downloads (Blob), multipart
 * uploads (FormData), or for probing with a candidate token before
 * calling `setToken()`.
 *
 * `overrideToken`:
 *   - `undefined` (default): pull token from session/memory like `api.*`.
 *   - `null`: no Authorization header is sent.
 *   - a string: that exact value is used as the Bearer token.
 *
 * 401 responses are returned as-is — callers decide whether to treat
 * them as "bad token" vs. "session expired".
 */
export async function apiFetch(
  path: string,
  init: RequestInit = {},
  overrideToken?: string | null,
): Promise<Response> {
  const token = overrideToken === undefined ? getToken() : overrideToken;
  const headers: Record<string, string> = {
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  return fetch(`${BASE_URL}${path}`, { ...init, headers });
}

function validateResponse(path: string, schema: ZodType, data: unknown): void {
  const result = schema.safeParse(data);
  if (!result.success) {
    console.warn(
      `[api] response schema mismatch for ${path}`,
      result.error.issues,
    );
  }
}

async function request<T>(
  path: string,
  options: ApiOptions = {},
): Promise<T> {
  const { schema, ...init } = options;
  const token = getToken();
  const headers: Record<string, string> = {
    ...((init.headers as Record<string, string>) ?? {}),
  };

  // Only set Content-Type when there's a body (POLISH-12: not on GET/DELETE)
  if (init.body) {
    headers["Content-Type"] = "application/json";
  }

  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
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

  const data = (await response.json()) as unknown;
  if (schema) {
    validateResponse(path, schema, data);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string, options?: ApiOptions) =>
    request<T>(path, options),

  post: <T>(path: string, body?: unknown, options?: ApiOptions) =>
    request<T>(path, {
      ...options,
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),

  put: <T>(path: string, body?: unknown, options?: ApiOptions) =>
    request<T>(path, {
      ...options,
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
    }),

  delete: <T>(path: string, options?: ApiOptions) =>
    request<T>(path, { ...options, method: "DELETE" }),
};
