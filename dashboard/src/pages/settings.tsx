import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { SaveIcon, Loader2Icon } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useDashboardStore } from "@/stores/dashboard";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { TabPlaceholder } from "@/components/coming-soon";
import type { Settings } from "@/types/api";

type LogLevel = Settings["log_level"];
const LOG_LEVELS: LogLevel[] = ["DEBUG", "INFO", "WARNING", "ERROR"];

export default function SettingsPage() {
  const { t } = useTranslation(["settings", "common"]);
  const settings = useDashboardStore((s) => s.settings);
  const setSettings = useDashboardStore((s) => s.setSettings);

  const [form, setForm] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  // Fetch settings
  const fetchSettings = useCallback(async () => {
    try {
      setLoading(true);
      const data = await api.get<Settings>("/api/settings");
      setSettings(data);
      setForm(data);
    } catch {
      // 401 handled
    } finally {
      setLoading(false);
    }
  }, [setSettings]);

  useEffect(() => {
    void fetchSettings();
  }, [fetchSettings]);

  // Track dirty state
  const updateField = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    if (!form) return;
    const next = { ...form, [key]: value };
    setForm(next);
    setDirty(JSON.stringify(next) !== JSON.stringify(settings));
  };

  // Save
  const handleSave = async () => {
    if (!form || !dirty) return;
    try {
      setSaving(true);
      const res = await api.put<{ ok: boolean; error?: string }>("/api/settings", {
        log_level: form.log_level,
        telemetry_enabled: form.telemetry_enabled,
        relay_enabled: form.relay_enabled,
      });
      if (res.ok) {
        setSettings(form);
        setDirty(false);
        toast.success(t("toast.saved"));
      } else {
        toast.error(res.error ?? t("toast.error"));
      }
    } catch {
      toast.error(t("toast.error"));
    } finally {
      setSaving(false);
    }
  };

  if (loading || !form) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
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

      {/* Logging */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">{t("sections.logging.title")}</CardTitle>
          <CardDescription className="text-xs">{t("sections.logging.description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Log Level */}
          <div className="space-y-2">
            <Label className="text-xs">{t("fields.logLevel")}</Label>
            <div className="flex rounded-md border border-[var(--svx-color-border-strong)]">
              {LOG_LEVELS.map((level) => (
                <button
                  key={level}
                  type="button"
                  onClick={() => updateField("log_level", level)}
                  className={`flex-1 px-3 py-1.5 text-xs font-medium transition-colors ${
                    level === form.log_level
                      ? "bg-primary text-primary-foreground"
                      : "hover:bg-secondary/50"
                  }`}
                >
                  {level}
                </button>
              ))}
            </div>
          </div>

          {/* Log Format (read-only) */}
          <div className="space-y-2">
            <Label className="text-xs">{t("fields.logFormat")}</Label>
            <Input value={form.log_format} disabled className="h-8 text-xs opacity-60" />
          </div>

          {/* Log File (read-only) */}
          <div className="space-y-2">
            <Label className="text-xs">{t("fields.logFile")}</Label>
            <Input
              value={form.log_file ?? "stdout"}
              disabled
              className="h-8 font-code text-xs opacity-60"
            />
          </div>
        </CardContent>
      </Card>

      {/* Engine */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">{t("sections.engine.title")}</CardTitle>
          <CardDescription className="text-xs">{t("sections.engine.description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Data Dir (read-only) */}
          <div className="space-y-2">
            <Label className="text-xs">{t("fields.dataDir")}</Label>
            <Input
              value={form.data_dir}
              disabled
              className="h-8 font-code text-xs opacity-60"
            />
          </div>

          {/* Telemetry */}
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-xs">{t("fields.telemetry")}</Label>
              <p className="text-[10px] text-muted-foreground">{t("fields.telemetryDescription")}</p>
            </div>
            <Switch
              checked={form.telemetry_enabled}
              onCheckedChange={(checked) => updateField("telemetry_enabled", checked)}
            />
          </div>

          {/* Relay */}
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-xs">{t("fields.relay")}</Label>
              <p className="text-[10px] text-muted-foreground">{t("fields.relayDescription")}</p>
            </div>
            <Switch
              checked={form.relay_enabled}
              onCheckedChange={(checked) => updateField("relay_enabled", checked)}
            />
          </div>
        </CardContent>
      </Card>

      {/* API (read-only) */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">{t("sections.api.title")}</CardTitle>
          <CardDescription className="text-xs">{t("sections.api.description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <Label className="text-xs">{t("fields.apiEnabled")}</Label>
            <Switch checked={form.api_enabled} disabled />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label className="text-xs">{t("fields.apiHost")}</Label>
              <Input
                value={form.api_host}
                disabled
                className="h-8 font-code text-xs opacity-60"
              />
            </div>
            <div className="space-y-2">
              <Label className="text-xs">{t("fields.apiPort")}</Label>
              <Input
                value={String(form.api_port)}
                disabled
                className="h-8 font-code text-xs opacity-60"
              />
            </div>
          </div>
        </CardContent>
      </Card>
      {/* v1.0 Placeholder Tabs */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[
          "Mind Configuration",
          "Channels",
          "API Keys",
          "Providers",
          "Plugins",
          "Privacy",
          "Export / Import",
          "Webhooks",
        ].map((tab) => (
          <Card key={tab} className="border-dashed">
            <CardContent className="py-6">
              <TabPlaceholder label={tab} />
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
