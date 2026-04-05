import { PuzzleIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function PluginsPage() {
  return (
    <ComingSoon
      icon={<PuzzleIcon className="size-8" />}
      title="Plugin Marketplace"
      description="Extend your Mind with community plugins. Weather, finance, smart home, and hundreds more."
      features={[
        "Search & browse marketplace",
        "One-click install & update",
        "Per-plugin configuration (JSON Schema)",
        "Plugin analytics & usage",
        "Sandbox status & permissions",
      ]}
      version="v1.0"
    />
  );
}
