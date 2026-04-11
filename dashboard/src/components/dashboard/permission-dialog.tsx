/**
 * Permission approval dialog — security-first install/audit gate.
 *
 * Shows all permissions a plugin requests with risk levels,
 * descriptions, and impact warnings. Deno/Android-style honest UX.
 *
 * Used for:
 * 1. Install flow (future marketplace) — approve before install
 * 2. Permission audit — view granted permissions in detail panel
 *
 * TASK-461
 */

import { useTranslation } from "react-i18next";
import { ShieldIcon, ShieldAlertIcon, GlobeIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogClose,
} from "@/components/ui/dialog";
import type { PluginPermission, PermissionRisk } from "@/types/api";

// ── Risk Summary ──

function RiskSummary({
  permissions,
}: {
  permissions: PluginPermission[];
}) {
  const { t } = useTranslation("plugins");

  const highCount = permissions.filter((p) => p.risk === "high").length;
  const mediumCount = permissions.filter((p) => p.risk === "medium").length;
  const lowCount = permissions.filter((p) => p.risk === "low").length;

  const hasHigh = highCount > 0;
  const hasMedium = mediumCount > 0;

  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-[var(--svx-radius-md)] px-3 py-2 text-xs",
        hasHigh
          ? "bg-[var(--svx-color-error)]/10 text-[var(--svx-color-error)]"
          : hasMedium
            ? "bg-[var(--svx-color-warning)]/10 text-[var(--svx-color-warning)]"
            : "bg-[var(--svx-color-success)]/10 text-[var(--svx-color-success)]",
      )}
    >
      {hasHigh ? (
        <ShieldAlertIcon className="size-4 shrink-0" />
      ) : (
        <ShieldIcon className="size-4 shrink-0" />
      )}
      <span>
        {permissions.length} permission{permissions.length !== 1 ? "s" : ""}
        {highCount > 0 && (
          <span className="font-semibold"> ({highCount} {t("permission.risk.high").toLowerCase()})</span>
        )}
        {mediumCount > 0 && !highCount && (
          <span className="font-semibold"> ({mediumCount} {t("permission.risk.medium").toLowerCase()})</span>
        )}
      </span>
    </div>
  );
}

// ── Permission Row ──

const RISK_STYLES: Record<PermissionRisk, { dot: string; bg: string; border: string }> = {
  low: {
    dot: "bg-[var(--svx-color-success)]",
    bg: "bg-[var(--svx-color-bg-surface)]",
    border: "border-[var(--svx-color-border-default)]",
  },
  medium: {
    dot: "bg-[var(--svx-color-warning)]",
    bg: "bg-[var(--svx-color-warning)]/5",
    border: "border-[var(--svx-color-warning)]/20",
  },
  high: {
    dot: "bg-[var(--svx-color-error)]",
    bg: "bg-[var(--svx-color-error)]/5",
    border: "border-[var(--svx-color-error)]/20",
  },
};

/** Known high-risk permissions that deserve extra explanation */
const HIGH_RISK_DETAILS: Record<string, string> = {
  "network:internet": "This plugin can make HTTP requests to external servers. Check allowed_domains in the manifest.",
  "brain:write": "This plugin can create, modify, or delete concepts in your Mind's memory.",
  "brain:delete": "This plugin can permanently delete memories and learned concepts.",
  "config:write": "This plugin can modify your Mind's configuration and personality settings.",
};

function PermissionRow({
  permission,
  allowedDomains,
}: {
  permission: PluginPermission;
  allowedDomains?: string[];
}) {
  const { t } = useTranslation("plugins");
  const styles = RISK_STYLES[permission.risk] ?? RISK_STYLES.medium;
  const extraDetail = HIGH_RISK_DETAILS[permission.permission];

  return (
    <div
      className={cn(
        "rounded-[var(--svx-radius-md)] border p-3",
        styles.bg,
        styles.border,
      )}
    >
      <div className="flex items-center gap-2">
        <span
          className={cn("size-2 rounded-full shrink-0", styles.dot)}
          aria-hidden="true"
        />
        <span className="text-xs font-medium text-[var(--svx-color-text-primary)]">
          {permission.permission}
        </span>
        <span className="ml-auto text-[10px] text-[var(--svx-color-text-tertiary)]">
          {t(`permission.risk.${permission.risk}`)}
        </span>
      </div>
      <p className="mt-1 pl-4 text-[10px] text-[var(--svx-color-text-secondary)]">
        {permission.description}
      </p>

      {/* Extra warning for high-risk permissions */}
      {extraDetail && permission.risk === "high" && (
        <div className="mt-2 ml-4 rounded bg-[var(--svx-color-error)]/5 px-2 py-1.5 text-[10px] text-[var(--svx-color-error)]">
          ⚠️ {extraDetail}
        </div>
      )}

      {/* Network: show allowed domains */}
      {permission.permission === "network:internet" &&
        allowedDomains &&
        allowedDomains.length > 0 && (
          <div className="mt-2 ml-4 flex items-start gap-1.5">
            <GlobeIcon className="mt-0.5 size-3 shrink-0 text-[var(--svx-color-text-tertiary)]" />
            <div className="text-[10px] text-[var(--svx-color-text-secondary)]">
              <span className="font-medium">Allowed domains:</span>{" "}
              {allowedDomains.join(", ")}
            </div>
          </div>
        )}
    </div>
  );
}

// ── Main Dialog ──

interface PermissionDialogProps {
  open: boolean;
  onClose: () => void;
  onApprove?: () => void;
  pluginName: string;
  permissions: PluginPermission[];
  allowedDomains?: string[];
  /** "install" shows approve button, "audit" is read-only */
  mode?: "install" | "audit";
}

export function PermissionDialog({
  open,
  onClose,
  onApprove,
  pluginName,
  permissions,
  allowedDomains,
  mode = "audit",
}: PermissionDialogProps) {
  const { t } = useTranslation("plugins");

  // Sort: high risk first, then medium, then low
  const sorted = [...permissions].sort((a, b) => {
    const order: Record<PermissionRisk, number> = {
      high: 0,
      medium: 1,
      low: 2,
    };
    return (order[a.risk] ?? 1) - (order[b.risk] ?? 1);
  });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[80vh] overflow-y-auto sm:max-w-md">
        <DialogTitle className="flex items-center gap-2 text-base font-semibold">
          <ShieldIcon className="size-4" />
          {mode === "install"
            ? `Install ${pluginName}`
            : `${pluginName} — ${t("detail.permissions")}`}
        </DialogTitle>
        <DialogDescription className="text-xs text-[var(--svx-color-text-secondary)]">
          {mode === "install"
            ? "Review the permissions this plugin requires before installing."
            : "Permissions granted to this plugin."}
        </DialogDescription>

        {/* Risk summary */}
        {permissions.length > 0 && (
          <RiskSummary permissions={permissions} />
        )}

        {/* Permission list */}
        <div className="space-y-2">
          {sorted.map((p) => (
            <PermissionRow
              key={p.permission}
              permission={p}
              allowedDomains={
                p.permission === "network:internet"
                  ? allowedDomains
                  : undefined
              }
            />
          ))}
        </div>

        {permissions.length === 0 && (
          <p className="py-4 text-center text-xs text-[var(--svx-color-text-tertiary)]">
            {t("detail.noPermissions")}
          </p>
        )}

        {/* Actions */}
        <div className="flex justify-end gap-2 pt-2">
          <DialogClose className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] px-4 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-bg-elevated)]">
            {mode === "install" ? t("actions.disable") : t("actions.disable").replace("Disable", "Close")}
          </DialogClose>
          {mode === "install" && onApprove && (
            <button
              type="button"
              onClick={() => {
                onApprove();
                onClose();
              }}
              className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-brand-primary)] px-4 py-1.5 text-xs font-medium text-white hover:opacity-90"
            >
              Approve &amp; Install
            </button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
