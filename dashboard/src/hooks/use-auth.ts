/**
 * Auth initialization hook.
 *
 * On mount: checks if a token exists in localStorage.
 * - If yes → validates against /api/status
 * - If no → shows token entry modal
 *
 * Called once in App.tsx.
 */
import { useEffect } from "react";
import { useDashboardStore } from "@/stores/dashboard";

const API_BASE = import.meta.env.VITE_API_URL ?? "";

export function useAuth(): { ready: boolean } {
  const authenticated = useDashboardStore((s) => s.authenticated);
  const setAuthenticated = useDashboardStore((s) => s.setAuthenticated);
  const setShowTokenModal = useDashboardStore((s) => s.setShowTokenModal);

  useEffect(() => {
    const token = localStorage.getItem("sovyx_token");

    if (!token) {
      setShowTokenModal(true);
      return;
    }

    // Validate existing token
    fetch(`${API_BASE}/api/status`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => {
        if (res.ok) {
          setAuthenticated(true);
        } else {
          // Token expired or invalid
          localStorage.removeItem("sovyx_token");
          setShowTokenModal(true);
        }
      })
      .catch(() => {
        // Server unreachable — still allow with existing token
        // (will retry on reconnect via WebSocket)
        setAuthenticated(true);
      });
  }, [setAuthenticated, setShowTokenModal]);

  return { ready: authenticated };
}
