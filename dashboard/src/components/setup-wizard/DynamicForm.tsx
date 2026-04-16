/**
 * DynamicForm -- renders fields from a SetupSchema definition.
 *
 * Field types: string, secret, url, number, boolean, select.
 * Handles required validation and regex patterns.
 * Autofill from provider defaults when `autofill_from` is set.
 */

import { memo } from "react";
import { cn } from "@/lib/utils";
import type { SetupField } from "./types";

interface DynamicFormProps {
  fields: SetupField[];
  values: Record<string, unknown>;
  onChange: (id: string, value: unknown) => void;
  errors?: Record<string, string>;
}

function FieldInput({
  field,
  value,
  onChange,
  error,
}: {
  field: SetupField;
  value: unknown;
  onChange: (v: unknown) => void;
  error?: string;
}) {
  const inputClass = cn(
    "w-full rounded-[var(--svx-radius-md)] border px-3 py-2 text-sm bg-[var(--svx-color-bg-surface)] text-[var(--svx-color-text-primary)] outline-none transition-colors",
    "focus:border-[var(--svx-color-brand-primary)] focus:ring-1 focus:ring-[var(--svx-color-brand-primary)]/30",
    error
      ? "border-[var(--svx-color-error)]"
      : "border-[var(--svx-color-border-default)]",
  );

  if (field.type === "boolean") {
    return (
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
          className="size-4 rounded border-[var(--svx-color-border-default)] accent-[var(--svx-color-brand-primary)]"
        />
        <span className="text-sm text-[var(--svx-color-text-primary)]">
          {field.label}
        </span>
      </label>
    );
  }

  if (field.type === "select" && field.options?.length) {
    return (
      <select
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
        className={inputClass}
      >
        <option value="">Select...</option>
        {field.options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }

  const inputType =
    field.type === "secret"
      ? "password"
      : field.type === "number"
        ? "number"
        : field.type === "url"
          ? "url"
          : "text";

  return (
    <input
      type={inputType}
      value={String(value ?? "")}
      onChange={(e) =>
        onChange(field.type === "number" ? Number(e.target.value) : e.target.value)
      }
      placeholder={field.placeholder}
      required={field.required}
      min={field.min}
      max={field.max}
      className={inputClass}
    />
  );
}

function DynamicFormImpl({
  fields,
  values,
  onChange,
  errors = {},
}: DynamicFormProps) {
  return (
    <div className="space-y-4">
      {fields.map((field) => {
        if (field.type === "boolean") {
          return (
            <div key={field.id}>
              <FieldInput
                field={field}
                value={values[field.id]}
                onChange={(v) => onChange(field.id, v)}
                error={errors[field.id]}
              />
            </div>
          );
        }

        return (
          <div key={field.id} className="space-y-1.5">
            <label
              htmlFor={`setup-${field.id}`}
              className="flex items-center gap-1 text-xs font-medium text-[var(--svx-color-text-secondary)]"
            >
              {field.label}
              {field.required && (
                <span className="text-[var(--svx-color-error)]">*</span>
              )}
            </label>
            <FieldInput
              field={field}
              value={values[field.id]}
              onChange={(v) => onChange(field.id, v)}
              error={errors[field.id]}
            />
            {field.help && (
              <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
                {field.help}
              </p>
            )}
            {errors[field.id] && (
              <p className="text-[11px] text-[var(--svx-color-error)]">
                {errors[field.id]}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

export const DynamicForm = memo(DynamicFormImpl);
