import { MicIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function VoicePage() {
  return (
    <ComingSoon
      icon={<MicIcon className="size-8" />}
      title="Voice Pipeline"
      description="Talk to your Mind with natural voice. Wake word detection, local STT/TTS, emotional voice modulation."
      features={[
        "Pipeline status monitor",
        "STT/TTS model selector",
        "Wake word configuration",
        "Latency gauge",
        "Voice test playground",
      ]}
      version="v1.0"
    />
  );
}
