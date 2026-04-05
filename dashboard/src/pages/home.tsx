import { HomeIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function HomePage() {
  return (
    <ComingSoon
      icon={<HomeIcon className="size-8" />}
      title="Home Integration"
      description="Control your smart home through your Mind. Home Assistant integration, routines, and presence detection."
      features={[
        "Home Assistant entity list",
        "Quick actions & routines",
        "Automation status",
        "Camera snapshots",
      ]}
      version="v1.0"
    />
  );
}
