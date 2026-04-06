import { MicIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

const VOICE_FEATURES = [
  "Pipeline status",
  "STT/TTS model selector",
  "Wake word config",
  "Latency gauge",
  "Voice test",
] as const;

export default function VoicePage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Voice Pipeline</h1>
        <p className="text-muted-foreground">
          Talk to your Mind with natural voice.
        </p>
      </div>

      <ComingSoon
        icon={<MicIcon className="size-6" />}
        title="Voice Pipeline"
        description="Talk to your Mind with natural voice. Wake word detection, local STT/TTS, emotional voice modulation."
        features={[...VOICE_FEATURES]}
        versionBadge="v1.0"
      />
    </div>
  );
}
