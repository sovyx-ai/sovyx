/**
 * Emotions page — Coming Soon.
 *
 * Will display emotional state tracking: valence/arousal history,
 * amygdala alerts, emotional patterns across conversations, and
 * mood correlations with productivity and decision quality.
 *
 * Roadmap: post-v1.0
 */

import { HeartIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function EmotionsPage() {
  return (
    <ComingSoon
      icon={HeartIcon}
      titleKey="emotions.title"
      descriptionKey="emotions.description"
    />
  );
}
