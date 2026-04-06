import { useTranslation } from "react-i18next";
import { MicIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function VoicePage() {
  const { t } = useTranslation("voice");
  const features = t("features", { returnObjects: true }) as Record<string, string>;

  return (
    <ComingSoon
      icon={<MicIcon className="size-8" />}
      title={t("title")}
      description={t("description")}
      features={Object.values(features)}
      version="v1.0"
    />
  );
}
