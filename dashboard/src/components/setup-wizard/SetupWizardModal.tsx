/**
 * SetupWizardModal -- generic plugin setup wizard.
 *
 * Schema-driven: reads setup_schema from the API, renders a form,
 * validates via test_connection, and saves via configure endpoint.
 * Zero plugin-specific UI code.
 *
 * Flow:
 *   1. Fetch schema from GET /api/setup/{name}/schema
 *   2. If providers exist, show ProviderSelect first
 *   3. Render DynamicForm from fields
 *   4. TestConnectionButton validates before save
 *   5. Save via POST /api/setup/{name}/configure
 *   6. Close modal + toast success
 */

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { SettingsIcon, LoaderIcon } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { ProviderSelect } from "./ProviderSelect";
import { DynamicForm } from "./DynamicForm";
import { TestConnectionButton } from "./TestConnectionButton";
import type { SetupProvider, SetupSchema, SetupSchemaResponse } from "./types";

interface SetupWizardModalProps {
  pluginName: string;
  pluginDescription?: string;
  trigger?: React.ReactNode;
}

export function SetupWizardModal({
  pluginName,
  pluginDescription,
  trigger,
}: SetupWizardModalProps) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [schema, setSchema] = useState<SetupSchema | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [selectedProvider, setSelectedProvider] = useState<string | null>(null);

  // Fetch schema when modal opens
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    api
      .get<SetupSchemaResponse>(`/api/setup/${pluginName}/schema`)
      .then((data) => {
        setSchema(data.setup_schema);
        // Pre-fill with current config
        if (data.current_config && typeof data.current_config === "object") {
          setValues({ ...data.current_config });
        }
        // Apply defaults from schema fields
        if (data.setup_schema?.fields) {
          const defaults: Record<string, unknown> = {};
          for (const f of data.setup_schema.fields) {
            if (f.default != null && !(f.id in (data.current_config ?? {}))) {
              defaults[f.id] = f.default;
            }
          }
          setValues((prev) => ({ ...defaults, ...prev }));
        }
      })
      .catch(() => {
        toast.error(`Failed to load setup for ${pluginName}`);
        setOpen(false);
      })
      .finally(() => setLoading(false));
  }, [open, pluginName]);

  const handleProviderSelect = useCallback(
    (provider: SetupProvider) => {
      setSelectedProvider(provider.id);

      if (provider.defaults) {
        setValues((prev) => ({ ...prev, ...provider.defaults }));
      }
    },
    [],
  );

  const handleFieldChange = useCallback((id: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [id]: value }));
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const result = await api.post<{ ok: boolean; error?: string }>(
        `/api/setup/${pluginName}/configure`,
        { config: values },
      );
      if (result.ok) {
        toast.success(`${pluginName} configured successfully`);
        setOpen(false);
      } else {
        toast.error(result.error ?? "Configuration failed");
      }
    } catch {
      toast.error("Failed to save configuration");
    } finally {
      setSaving(false);
    }
  }, [pluginName, values]);

  // Check if required fields are filled
  const requiredFilled =
    schema?.fields
      .filter((f) => f.required)
      .every((f) => {
        const v = values[f.id];
        return v != null && v !== "";
      }) ?? false;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          (trigger as React.ReactElement) ?? (
            <Button variant="outline" size="sm">
              <SettingsIcon className="mr-1.5 size-3.5" />
              Configure
            </Button>
          )
        }
      />
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Set up {pluginName}</DialogTitle>
          {pluginDescription && (
            <DialogDescription>{pluginDescription}</DialogDescription>
          )}
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center py-8">
            <LoaderIcon className="size-5 animate-spin text-[var(--svx-color-text-tertiary)]" />
          </div>
        ) : schema ? (
          <div className="space-y-5 py-2">
            {schema.providers && schema.providers.length > 0 && (
              <ProviderSelect
                providers={schema.providers}
                selected={selectedProvider}
                onSelect={handleProviderSelect}
              />
            )}

            <DynamicForm
              fields={schema.fields}
              values={values}
              onChange={handleFieldChange}
            />

            {schema.test_connection && (
              <TestConnectionButton
                pluginName={pluginName}
                config={values as Record<string, unknown>}
                disabled={!requiredFilled}
              />
            )}
          </div>
        ) : (
          <p className="py-4 text-center text-sm text-[var(--svx-color-text-tertiary)]">
            This plugin does not require configuration.
          </p>
        )}

        <DialogFooter showCloseButton>
          {schema && (
            <Button
              onClick={handleSave}
              disabled={!requiredFilled || saving}
              className="min-w-[100px]"
            >
              {saving ? (
                <LoaderIcon className="mr-2 size-3.5 animate-spin" />
              ) : null}
              {saving ? "Saving..." : "Save & Enable"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
