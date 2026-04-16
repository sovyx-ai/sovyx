import { useCallback, useState } from "react";
import { LoaderIcon } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface PersonalityPreset {
  id: string;
  name: string;
  description: string;
}

const PRESETS: PersonalityPreset[] = [
  { id: "warm", name: "Warm & Friendly", description: "Like a thoughtful friend who always has time for you" },
  { id: "direct", name: "Direct & Concise", description: "Straight to the point. No filler. Maximum signal." },
  { id: "playful", name: "Playful & Creative", description: "Witty, curious, loves exploring ideas together" },
  { id: "professional", name: "Professional", description: "Formal, precise, business-ready" },
];

interface PersonalityStepProps {
  onConfigured: () => void;
  onSkip: () => void;
}

export function PersonalityStep({ onConfigured, onSkip }: PersonalityStepProps) {
  const [selected, setSelected] = useState("warm");
  const [language, setLanguage] = useState(
    () => navigator.language?.split("-")[0] ?? "en",
  );
  const [userName, setUserName] = useState("");
  const [saving, setSaving] = useState(false);

  const handleContinue = useCallback(async () => {
    setSaving(true);
    try {
      await api.post("/api/onboarding/personality", {
        preset: selected,
        language,
        user_name: userName || undefined,
      });
      onConfigured();
    } catch {
      onConfigured();
    } finally {
      setSaving(false);
    }
  }, [selected, language, userName, onConfigured]);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
          Meet Aria
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          How should your companion communicate?
        </p>
      </div>

      {/* Preset cards */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            onClick={() => setSelected(preset.id)}
            className={cn(
              "rounded-[var(--svx-radius-lg)] border p-4 text-left transition-all",
              selected === preset.id
                ? "border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-brand-primary)]/5"
                : "border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] hover:border-[var(--svx-color-brand-primary)]/40",
            )}
          >
            <div className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
              {preset.name}
            </div>
            <p className="mt-1 text-xs text-[var(--svx-color-text-secondary)]">
              {preset.description}
            </p>
          </button>
        ))}
      </div>

      {/* Language + Name */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-medium text-[var(--svx-color-text-secondary)]">
            Language
          </label>
          <select
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 text-sm text-[var(--svx-color-text-primary)]"
          >
            <option value="en">English</option>
            <option value="pt">Portuguese</option>
            <option value="es">Spanish</option>
            <option value="fr">French</option>
            <option value="de">German</option>
            <option value="it">Italian</option>
            <option value="ja">Japanese</option>
            <option value="ko">Korean</option>
            <option value="zh">Chinese</option>
            <option value="ru">Russian</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-[var(--svx-color-text-secondary)]">
            Your name (optional)
          </label>
          <input
            type="text"
            value={userName}
            onChange={(e) => setUserName(e.target.value)}
            placeholder="How should Aria address you?"
            className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 text-sm text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)]"
          />
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onSkip}
          className="text-xs text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-secondary)]"
        >
          Skip for now
        </button>
        <Button onClick={handleContinue} disabled={saving}>
          {saving && <LoaderIcon className="mr-1.5 size-3.5 animate-spin" />}
          Continue
        </Button>
      </div>
    </div>
  );
}
