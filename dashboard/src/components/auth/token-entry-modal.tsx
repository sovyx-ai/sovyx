import { useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { setToken } from "@/lib/api";
import { useDashboardStore } from "@/stores/dashboard";
import { Loader2Icon, KeyIcon, CheckCircleIcon, XCircleIcon } from "lucide-react";

type ValidationState = "idle" | "validating" | "valid" | "invalid";

const API_BASE = import.meta.env.VITE_API_URL ?? "";

export function TokenEntryModal() {
  const { t } = useTranslation("common");
  const showTokenModal = useDashboardStore((s) => s.showTokenModal);
  const setShowTokenModal = useDashboardStore((s) => s.setShowTokenModal);
  const setAuthenticated = useDashboardStore((s) => s.setAuthenticated);

  const [token, setTokenValue] = useState("");
  const [state, setState] = useState<ValidationState>("idle");
  const [errorMsg, setErrorMsg] = useState("");

  const validate = useCallback(async () => {
    const trimmed = token.trim();
    if (!trimmed) return;

    setState("validating");
    setErrorMsg("");

    try {
      const res = await fetch(`${API_BASE}/api/status`, {
        headers: { Authorization: `Bearer ${trimmed}` },
      });

      if (res.ok) {
        setState("valid");
        setToken(trimmed);
        setAuthenticated(true);

        // Close modal after brief success feedback
        setTimeout(() => {
          setShowTokenModal(false);
          setTokenValue("");
          setState("idle");
        }, 600);
      } else if (res.status === 401) {
        setState("invalid");
        setErrorMsg(t("errors.unauthorized"));
      } else {
        setState("invalid");
        setErrorMsg(`${t("errors.generic")} (${res.status})`);
      }
    } catch {
      setState("invalid");
      setErrorMsg(t("errors.network"));
    }
  }, [token, t, setAuthenticated, setShowTokenModal]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && token.trim()) {
      void validate();
    }
  };

  return (
    <Dialog open={showTokenModal} onOpenChange={() => { /* prevent closing without token */ }}>
      <DialogContent
        className="sm:max-w-md"
        showCloseButton={false}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyIcon className="size-5 text-[var(--svx-color-brand-primary)]" />
            {t("app.name")}
          </DialogTitle>
          <DialogDescription>
            {t("auth.description")}{" "}
            <code className="font-code rounded bg-[var(--svx-color-bg-elevated)] px-1.5 py-0.5 text-xs">
              {t("auth.command")}
            </code>
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 pt-2">
          <div className="flex gap-2">
            <Input
              type="password"
              placeholder={t("auth.placeholder")}
              value={token}
              onChange={(e) => {
                setTokenValue(e.target.value);
                if (state === "invalid") setState("idle");
              }}
              onKeyDown={handleKeyDown}
              disabled={state === "validating" || state === "valid"}
              className="font-code text-sm"
              autoFocus
            />
            <Button
              onClick={() => void validate()}
              disabled={!token.trim() || state === "validating" || state === "valid"}
              className="shrink-0"
            >
              {state === "validating" && (
                <Loader2Icon className="mr-2 size-4 animate-spin" />
              )}
              {state === "valid" && (
                <CheckCircleIcon className="mr-2 size-4 text-[var(--svx-color-success)]" />
              )}
              {state === "valid" ? t("auth.connected") : t("auth.connect")}
            </Button>
          </div>

          {state === "invalid" && (
            <div className="flex items-center gap-2 text-sm text-[var(--svx-color-error)]">
              <XCircleIcon className="size-4 shrink-0" />
              {errorMsg}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
