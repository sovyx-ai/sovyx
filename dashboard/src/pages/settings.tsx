/**
 * Settings page — General settings + 8 placeholder sections.
 *
 * CRITICAL: Only `log_level` is mutable via PUT /api/settings.
 * All other fields (telemetry, relay, api_host, etc.) are READ-ONLY
 * display from GET /api/settings. Showing them as editable would be
 * a lie — the backend ignores them in the PUT payload.
 *
 * Ref: Architecture §3.5, META-07 §1
 */

import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  SaveIcon,
  Loader2Icon,
  UserIcon,
  RadioIcon,
  KeyIcon,
  CpuIcon,
  PuzzleIcon,
  ShieldIcon,
  DownloadIcon,
  WebhookIcon,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useDashboardStore } from "@/stores/dashboard";
import { api, isAbortError } from "@/lib/api";
import { toast } from "sonner";
import type { Settings } from "@/types/api";
import { cn } from "@/lib/utils";

type LogLevel = Settings["log_level"];
const LOG_LEVELS: LogLevel[] = ["DEBUG", "INFO", "WARNING", "ERROR"];

export default function SettingsPage() {
  const { t } = useTranslation(["settings", "common"]);
  const settings = useDashboardStore((s) => s.settings);
  const setSettings = useDashboardStore((s) => s.setSettings);

  const [selectedLevel, setSelectedLevel] = useState<LogLevel>("INFO");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // The ONLY mutable field
  const dirty = settings != null && selectedLevel !== settings.log_level;

  // Fetch settings with AbortController (POLISH-01)
  const fetchSettings = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoading(true);
      const data = await api.get<Settings>("/api/settings", { signal });
      setSettings(data);
      setSelectedLevel(data.log_level);
    } catch (err) {
      if (isAbortError(err)) return;
      toast.error("Failed to load settings");
    } finally {
      setLoading(false);
    }
  }, [setSettings]);

  useEffect(() => {
    const controller = new AbortController();
    void fetchSettings(controller.signal);
    return () => controller.abort();
  }, [fetchSettings]);

  // Save — ONLY sends log_level (the only mutable field)
  const handleSave = async () => {
    if (!dirty) return;
    try {
      setSaving(true);
      const res = await api.put<{ ok: boolean; changes?: Record<string, string>; error?: string }>(
        "/api/settings",
        { log_level: selectedLevel },
      );
      if (res.ok !== false) {
        if (settings) {
          setSettings({ ...settings, log_level: selectedLevel });
        }
        toast.success(t("general.saved"));
      } else {
        toast.error(res.error ?? t("general.saveFailed"));
      }
    } catch {
      toast.error(t("general.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  if (loading || !settings) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-[var(--svx-color-brand-primary)] border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-[var(--svx-color-text-primary)]">
            {t("title")}
          </h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">
            Engine configuration and feature settings
          </p>
        </div>
        <Button
          onClick={() => void handleSave()}
          disabled={!dirty || saving}
          className="gap-2"
        >
          {saving ? (
            <Loader2Icon className="size-4 animate-spin" />
          ) : (
            <SaveIcon className="size-4" />
          )}
          {t("common:actions.save")}
        </Button>
      </div>

      {/* ── General: Log Level (MUTABLE) ── */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          {t("tabs.general")}
        </h2>
        <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
          Runtime-mutable settings. Changes take effect immediately.
        </p>

        <div className="mt-4 space-y-4">
          {/* Log Level — the ONLY mutable field */}
          <div className="space-y-2">
            <Label className="text-xs">{t("general.logLevel")}</Label>
            <div className="flex rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-strong)]">
              {LOG_LEVELS.map((level) => (
                <button
                  key={level}
                  type="button"
                  onClick={() => setSelectedLevel(level)}
                  className={cn(
                    "flex-1 px-3 py-1.5 text-xs font-medium transition-colors",
                    level === selectedLevel
                      ? "bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)]"
                      : "hover:bg-[var(--svx-color-bg-hover)] text-[var(--svx-color-text-secondary)]",
                  )}
                >
                  {level}
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* ── Engine Info (READ-ONLY) ── */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          Engine Configuration
        </h2>
        <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
          Read-only. Edit system.yaml to change these values.
        </p>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <ReadOnlyField label="Data Directory" value={settings.data_dir} mono />
          <ReadOnlyField label="Log Format" value={settings.log_format} />
          <ReadOnlyField label="Log File" value={settings.log_file ?? "stdout"} mono />
          <ReadOnlyField label="Telemetry" value={settings.telemetry_enabled ? "Enabled" : "Disabled"} />
          <ReadOnlyField label="API" value={`${settings.api_host}:${settings.api_port}`} mono />
          <ReadOnlyField label="Relay" value={settings.relay_enabled ? "Enabled" : "Disabled"} />
        </div>
      </section>

      {/* ── 8 Placeholder Sections (v1.0) ── */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <PlaceholderSection icon={UserIcon} title="Mind" version="v0.5" />
        <PlaceholderSection icon={RadioIcon} title="Channels" version="v0.5" />
        <PlaceholderSection icon={KeyIcon} title="API Keys" version="v0.5" />
        <PlaceholderSection icon={CpuIcon} title="LLM Providers" version="v0.5" />
        <PlaceholderSection icon={PuzzleIcon} title="Plugins" version="v1.0" />
        <PlaceholderSection icon={ShieldIcon} title="Privacy" version="v1.0" />
        <PlaceholderSection icon={DownloadIcon} title="Export / Import" version="v1.0" />
        <PlaceholderSection icon={WebhookIcon} title="Webhooks" version="v1.0" />
      </div>
    </div>
  );
}

// ── Read-only field display ──

function ReadOnlyField({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="space-y-1">
      <Label className="text-xs text-[var(--svx-color-text-tertiary)]">{label}</Label>
      <Input
        value={value}
        disabled
        className={cn(
          "h-8 text-xs opacity-60",
          mono && "font-code",
        )}
      />
    </div>
  );
}

// ── Placeholder section card ──

function PlaceholderSection({
  icon: Icon,
  title,
  version,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  version: string;
}) {
  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-dashed border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
      <div className="flex items-center gap-2">
        <Icon className="size-4 text-[var(--svx-color-text-disabled)]" />
        <span className="text-sm font-medium text-[var(--svx-color-text-secondary)]">
          {title}
        </span>
      </div>
      <p className="mt-2 text-xs text-[var(--svx-color-text-disabled)]">
        Available in {version}
      </p>
    </div>
  );
}
