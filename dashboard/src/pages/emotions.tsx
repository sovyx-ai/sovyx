import { useTranslation } from "react-i18next";
import { HeartIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function EmotionsPage() {
  const { t } = useTranslation("emotions");
  const features = t("features", { returnObjects: true }) as Record<string, string>;

  return (
    <ComingSoon
      icon={<HeartIcon className="size-8" />}
      title={t("title")}
      description={t("description")}
      features={Object.values(features)}
      version="v1.0"
    />
  );
}
