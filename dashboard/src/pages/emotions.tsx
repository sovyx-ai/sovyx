import { HeartIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function EmotionsPage() {
  return (
    <ComingSoon
      icon={<HeartIcon className="size-8" />}
      title="Emotional Intelligence"
      description="Your Mind feels. PAD emotional model with 16 distinct emotions, mood timeline, and trigger history."
      features={[
        "Current mood indicator (PAD 3D)",
        "16 emotion wheel",
        "Emotion timeline (24h/7d/30d)",
        "Trigger history & patterns",
      ]}
      version="v1.0"
    />
  );
}
