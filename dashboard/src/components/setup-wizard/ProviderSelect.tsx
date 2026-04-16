/**
 * ProviderSelect -- pick a pre-configured provider (Fastmail, iCloud, etc).
 *
 * When a provider is selected, its `defaults` are merged into the form
 * values so the user doesn't have to type the base URL manually.
 */

import { memo } from "react";
import { cn } from "@/lib/utils";
import type { SetupProvider } from "./types";

interface ProviderSelectProps {
  providers: SetupProvider[];
  selected: string | null;
  onSelect: (provider: SetupProvider) => void;
}

function ProviderSelectImpl({
  providers,
  selected,
  onSelect,
}: ProviderSelectProps) {
  if (!providers.length) return null;

  return (
    <div className="space-y-2">
      <label className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
        Provider
      </label>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {providers.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => onSelect(p)}
            className={cn(
              "rounded-[var(--svx-radius-md)] border px-3 py-2.5 text-left text-xs font-medium transition-all",
              selected === p.id
                ? "border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-brand-primary)]/10 text-[var(--svx-color-brand-primary)]"
                : "border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] text-[var(--svx-color-text-primary)] hover:border-[var(--svx-color-brand-primary)]/40",
            )}
          >
            {p.name}
          </button>
        ))}
      </div>
    </div>
  );
}

export const ProviderSelect = memo(ProviderSelectImpl);
