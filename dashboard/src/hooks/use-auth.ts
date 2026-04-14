/**
 * Auth initialization hook.
 *
 * On mount: checks if a token exists in session storage (via the
 * centralized `api` client) and validates it against `/api/status`.
 *
 * - If no token → shows token entry modal.
 * - If token valid → authenticated.
 * - If token invalid (401/403) → clears token, shows modal.
 * - If server unreachable → **fail-closed**: shows modal. The previous
 *   fail-open behaviour (trust the existing token on network error)
 *   let stale/compromised tokens bypass revalidation.
 *
 * Called once in App.tsx.
 */
import { useEffect } from "react";
import { useDashboardStore } from "@/stores/dashboard";
import { ApiError, api, clearToken } from "@/lib/api";

export function useAuth(): { ready: boolean } {
  const authenticated = useDashboardStore((s) => s.authenticated);
  const setAuthenticated = useDashboardStore((s) => s.setAuthenticated);
  const setShowTokenModal = useDashboardStore((s) => s.setShowTokenModal);

  useEffect(() => {
    let cancelled = false;

    const validate = async (): Promise<void> => {
      try {
        // `api.get` injects the Authorization header itself and triggers
        // the 401 handler that already clears the token + opens the modal.
        // `retries: 0` — auth probe must fail fast; backoff would delay
        // the "ask the user to re-enter the token" UX by several seconds.
        await api.get("/api/status", { retries: 0 });
        if (!cancelled) {
          setAuthenticated(true);
        }
      } catch (err) {
        if (cancelled) return;
        // Explicit auth failure — token is gone or rejected. Always ask
        // the user to re-enter it rather than continuing authenticated.
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          clearToken();
          setShowTokenModal(true);
          setAuthenticated(false);
          return;
        }
        // Network / server unreachable: fail CLOSED. We cannot prove the
        // token is still valid, so require the user to re-enter it.
        clearToken();
        setShowTokenModal(true);
        setAuthenticated(false);
      }
    };

    void validate();
    return () => {
      cancelled = true;
    };
  }, [setAuthenticated, setShowTokenModal]);

  return { ready: authenticated };
}
