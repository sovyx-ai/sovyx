/**
 * SigningKeyCard — Settings → Voice surface for generating the
 * Ed25519 calibration signing keypair (BT.B.3, v0.32.0).
 *
 * Mission: ``MISSION-voice-v0_32_0-structural-closure-2026-05-08.md``
 * Phase B BT.B.3. Pre-v0.32.0, only the dev-only repo script
 * (``scripts/dev/generate_calibration_signing_key.py``) produced a
 * usable signing key. Operators running shipped Sovyx had no surface
 * to generate the key, gating the :data:`Mode.STRICT` default flip
 * planned for v0.33.0+ on a wizard-driven generator landing first.
 *
 * Status renders one of:
 *   * Loading (status fetch in flight)
 *   * "Not yet generated" + Generate button
 *   * "Generated <fingerprint>" + Regenerate button (with confirm)
 *
 * Operator click → POST /api/voice/calibration/generate-signing-key
 * → on 200, surface fingerprint + path; on 409, show a Regenerate
 * confirmation modal warning the operator that existing signed
 * profiles will need re-signing.
 *
 * Privacy contract: the dashboard NEVER receives the private key
 * bytes. The response carries the public key PEM + paths only;
 * the private key stays on disk under POSIX 0o600 (Windows: NTFS
 * inherited ACL).
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { KeyRoundIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useDashboardStore } from "@/stores/dashboard";

export function SigningKeyCard() {
  const { t } = useTranslation(["settings"]);
  const status = useDashboardStore((s) => s.signingKeyStatus);
  const loadStatus = useDashboardStore((s) => s.loadSigningKeyStatus);
  const generateKey = useDashboardStore((s) => s.generateSigningKey);
  const loading = useDashboardStore((s) => s.calibrationLoading);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  // Load on mount; idempotent.
  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const handleGenerate = useCallback(
    async (force: boolean) => {
      setBusy(true);
      try {
        const result = await generateKey({ force });
        if (result === null) {
          toast.error(t("settings:signingKey.generateFailed"));
          return;
        }
        toast.success(
          t("settings:signingKey.generateSuccess", {
            fingerprint: result.fingerprint_short,
          }),
        );
      } finally {
        setBusy(false);
        setConfirming(false);
      }
    },
    [generateKey, t],
  );

  const exists = status?.exists ?? false;
  const fingerprint = status?.fingerprint_short ?? null;
  const path = status?.public_key_path ?? null;

  return (
    <section
      data-testid="settings-signing-key-card"
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4"
    >
      <header className="flex items-start gap-3">
        <KeyRoundIcon className="size-5 shrink-0 text-[var(--svx-color-text-secondary)]" />
        <div className="flex-1">
          <h2 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {t("settings:signingKey.title")}
          </h2>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("settings:signingKey.description")}
          </p>
        </div>
      </header>

      <div className="mt-4 flex items-center justify-between gap-4">
        <div
          className="text-xs text-[var(--svx-color-text-tertiary)]"
          data-testid="settings-signing-key-status"
        >
          {status === null ? (
            <span>{t("settings:signingKey.statusLoading")}</span>
          ) : exists ? (
            <span>
              <span className="font-medium text-[var(--svx-color-text-primary)]">
                {t("settings:signingKey.statusGenerated")}
              </span>
              {fingerprint !== null && (
                <span
                  className="ml-2 font-mono text-[var(--svx-color-text-secondary)]"
                  data-testid="settings-signing-key-fingerprint"
                >
                  {fingerprint}
                </span>
              )}
            </span>
          ) : (
            <span className="font-medium text-[var(--svx-color-text-primary)]">
              {t("settings:signingKey.statusNotGenerated")}
            </span>
          )}
        </div>

        {confirming ? (
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setConfirming(false)}
              data-testid="settings-signing-key-cancel"
            >
              {t("settings:signingKey.cancelButton")}
            </Button>
            <Button
              type="button"
              variant="default"
              disabled={busy || loading}
              onClick={() => void handleGenerate(true)}
              data-testid="settings-signing-key-confirm"
            >
              {busy ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                t("settings:signingKey.confirmRegenerateButton")
              )}
            </Button>
          </div>
        ) : exists ? (
          <Button
            type="button"
            variant="outline"
            disabled={busy || loading || status === null}
            onClick={() => setConfirming(true)}
            data-testid="settings-signing-key-regenerate"
          >
            {t("settings:signingKey.regenerateButton")}
          </Button>
        ) : (
          <Button
            type="button"
            variant="default"
            disabled={busy || loading || status === null}
            onClick={() => void handleGenerate(false)}
            data-testid="settings-signing-key-generate"
          >
            {busy ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              t("settings:signingKey.generateButton")
            )}
          </Button>
        )}
      </div>

      {confirming && (
        <p
          className="mt-3 text-[11px] text-[var(--svx-color-status-warning)]"
          data-testid="settings-signing-key-warning"
        >
          {t("settings:signingKey.regenerateWarning")}
        </p>
      )}

      {!confirming && exists && path !== null && (
        <p
          className="mt-3 truncate text-[11px] text-[var(--svx-color-text-tertiary)]"
          data-testid="settings-signing-key-path"
          title={path}
        >
          {t("settings:signingKey.publicKeyPathLabel")} {path}
        </p>
      )}

      {!confirming && !exists && (
        <p className="mt-3 text-[11px] text-[var(--svx-color-text-tertiary)]">
          {t("settings:signingKey.note")}
        </p>
      )}
    </section>
  );
}
