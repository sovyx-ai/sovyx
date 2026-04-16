/**
 * Types for the setup wizard — mirrors backend SetupSchema models.
 */

export interface SetupFieldOption {
  value: string;
  label: string;
}

export interface SetupProvider {
  id: string;
  name: string;
  help_url?: string;
  defaults?: Record<string, string>;
}

export interface SetupField {
  id: string;
  type: "string" | "secret" | "url" | "number" | "boolean" | "select";
  label: string;
  required?: boolean;
  placeholder?: string;
  help?: string;
  help_links?: Record<string, string>;
  validation?: string;
  autofill_from?: string;
  default?: string | number | boolean | null;
  min?: number;
  max?: number;
  options?: SetupFieldOption[];
}

export interface SetupSchema {
  providers?: SetupProvider[];
  fields: SetupField[];
  test_connection?: boolean;
}

export interface SetupSchemaResponse {
  plugin: string;
  setup_schema: SetupSchema | null;
  config_schema: Record<string, unknown> | null;
  current_config: Record<string, unknown>;
}

export interface TestConnectionResult {
  success: boolean;
  message: string;
  details?: Record<string, unknown>;
}
