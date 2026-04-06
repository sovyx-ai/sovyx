import { useTranslation } from "react-i18next";
import { PuzzleIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function PluginsPage() {
  const { t } = useTranslation("plugins");
  const features = t("features", { returnObjects: true }) as Record<string, string>;

  return (
    <ComingSoon
      icon={<PuzzleIcon className="size-8" />}
      title={t("title")}
      description={t("description")}
      features={Object.values(features)}
      version="v1.0"
    />
  );
}
