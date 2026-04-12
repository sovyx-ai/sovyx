/**
 * Productivity page — Coming Soon.
 *
 * Will display productivity metrics: focus time tracking, task
 * completion patterns, decision fatigue detection, journaling
 * correlation analysis, and weekly/monthly trend reports.
 *
 * Roadmap: post-v1.0
 */

import { BarChart3Icon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function ProductivityPage() {
  return (
    <ComingSoon
      icon={BarChart3Icon}
      titleKey="productivity.title"
      descriptionKey="productivity.description"
    />
  );
}
