/**
 * Sovyx Dashboard — API client
 *
 * Thin fetch wrapper with:
 * - Bearer auth (token kept in `sessionStorage` + in-memory fallback;
 *   never in `localStorage`, to reduce XSS token-theft blast radius)
 * - 401 → clear token + show auth modal
 * - AbortSignal support for cancellation + default 30s timeout
 * - Retry with exponential backoff on 429/503/network errors for
 *   idempotent verbs (GET/PUT/DELETE); POST/PATCH must opt in
 * - Content-Type only on requests with body
 * - Typed ApiError for non-2xx responses
 * - Optional zod schema validation on response body (safeParse + warn —
 *   catches backend contract drift without hard-failing production)
 * - Typed query-string helper (`buildQuery`) so callers stop
 *   hand-concatenating URLs
 */

import type { ZodType } from "zod";

export const BASE_URL = import.meta.env.VITE_API_URL ?? "";

/** Default request timeout — pending request is aborted after this window. */
export const DEFAULT_TIMEOUT_MS = 30_000;
/** Default number of retries for idempotent verbs (GET/PUT/DELETE). */
const DEFAULT_IDEMPOTENT_RETRIES = 2;
/** Base delay for retry backoff (ms) — doubled per attempt. */
const DEFAULT_RETRY_BASE_MS = 400;
/** HTTP statuses that are safe to retry. */
const RETRYABLE_STATUSES = new Set([408, 429, 502, 503, 504]);

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

export function getToken(): string | null {
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
 * `schema`: when present, the response JSON is validated through
 *   `schema.safeParse(...)`. On mismatch we log a console warning with
 *   the issue list (visible in staging / dev tools) but still return the
 *   raw payload — the backend is the source of truth and feature
 *   failure from a single added field would be worse than a quiet warning.
 *
 * `timeout`: ms before the request is aborted. Defaults to
 *   `DEFAULT_TIMEOUT_MS` (30s). Pass `0` to disable.
 *
 * `retries`: number of retry attempts on 429/503/network errors. Idempotent
 *   verbs (GET/PUT/DELETE) default to `DEFAULT_IDEMPOTENT_RETRIES`; POST
 *   and PATCH default to 0 — callers with idempotency-keyed endpoints can
 *   opt in explicitly.
 */
export interface ApiOptions extends RequestInit {
  schema?: ZodType;
  timeout?: number;
  retries?: number;
}

/** Values accepted by `buildQuery`. `undefined`/`null` entries are dropped. */
export type QueryValue = string | number | boolean | null | undefined;

/**
 * Serialize a param object to `?k=v&k=v`. Drops `null`/`undefined` entries,
 * coerces numbers/booleans to strings, URL-encodes keys and values. Returns
 * an empty string (not `?`) when every entry is dropped, so it is safe to
 * concatenate unconditionally.
 */
export function buildQuery(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    search.append(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : "";
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

/**
 * Forward an abort on the external signal to our internal controller.
 * Returns a cleanup that detaches the listener so we don't leak.
 */
function forwardAbort(
  external: AbortSignal | null | undefined,
  target: AbortController,
): () => void {
  if (!external) return () => {};
  if (external.aborted) {
    target.abort(external.reason);
    return () => {};
  }
  const onAbort = () => target.abort(external.reason);
  external.addEventListener("abort", onAbort);
  return () => external.removeEventListener("abort", onAbort);
}

/** Honor `Retry-After` (seconds or HTTP-date) when present on 429/503. */
function parseRetryAfter(header: string | null): number | null {
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const asDate = Date.parse(header);
  if (Number.isFinite(asDate)) return Math.max(0, asDate - Date.now());
  return null;
}

function backoffDelay(attempt: number, retryAfter: number | null): number {
  if (retryAfter !== null) return retryAfter;
  // Exponential with light jitter — attempt=0 → ~400ms, attempt=1 → ~800ms, etc.
  const base = DEFAULT_RETRY_BASE_MS * 2 ** attempt;
  return base + Math.floor(Math.random() * 100);
}

function defaultRetries(method: string | undefined): number {
  const verb = (method ?? "GET").toUpperCase();
  return verb === "POST" || verb === "PATCH" ? 0 : DEFAULT_IDEMPOTENT_RETRIES;
}

/** A single network attempt — no retry logic. Returns raw Response. */
async function fetchOnce(
  path: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const detach = forwardAbort(init.signal ?? null, controller);
  const timer =
    timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    return await fetch(`${BASE_URL}${path}`, { ...init, signal: controller.signal });
  } finally {
    if (timer !== null) clearTimeout(timer);
    detach();
  }
}

async function request<T>(
  path: string,
  options: ApiOptions = {},
): Promise<T> {
  const { schema, timeout, retries, ...init } = options;
  const timeoutMs = timeout ?? DEFAULT_TIMEOUT_MS;
  const maxRetries = retries ?? defaultRetries(init.method);
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

  const fullInit: RequestInit = { ...init, headers };

  let attempt = 0;
  let lastErr: unknown = null;
  // Attempts = 1 + maxRetries. Loop condition checks attempt at top.
  while (attempt <= maxRetries) {
    try {
      const response = await fetchOnce(path, fullInit, timeoutMs);

      if (response.ok) {
        const data = (await response.json()) as unknown;
        if (schema) validateResponse(path, schema, data);
        return data as T;
      }

      // 401 → always a hard stop, never retry.
      if (response.status === 401) {
        clearToken();
        const { useDashboardStore } = await import("@/stores/dashboard");
        useDashboardStore.getState().setAuthenticated(false);
        useDashboardStore.getState().setShowTokenModal(true);
        const body = await response.text().catch(() => "Unauthorized");
        throw new ApiError(401, body);
      }

      // Retryable status + attempts remaining → back off and try again.
      if (RETRYABLE_STATUSES.has(response.status) && attempt < maxRetries) {
        const after = parseRetryAfter(response.headers.get("Retry-After"));
        await sleep(backoffDelay(attempt, after));
        attempt += 1;
        continue;
      }

      const body = await response.text().catch(() => "Unknown error");
      throw new ApiError(response.status, body);
    } catch (err) {
      // AbortError propagates immediately — user cancelled or timeout fired
      // and we don't want to retry a cancelled request silently.
      if (isAbortError(err)) throw err;
      // ApiError above already fell through a retry decision; if it
      // reaches here it's a terminal status.
      if (err instanceof ApiError) throw err;
      // Network error (TypeError from fetch) — retry if we can.
      if (attempt < maxRetries) {
        await sleep(backoffDelay(attempt, null));
        attempt += 1;
        lastErr = err;
        continue;
      }
      throw err;
    }
  }
  // Loop exit without return means we exhausted retries on network errors.
  throw lastErr ?? new Error("request failed");
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

  patch: <T>(path: string, body?: unknown, options?: ApiOptions) =>
    request<T>(path, {
      ...options,
      method: "PATCH",
      body: body ? JSON.stringify(body) : undefined,
    }),

  delete: <T>(path: string, options?: ApiOptions) =>
    request<T>(path, { ...options, method: "DELETE" }),
};
