import { useTranslation } from "react-i18next";
import { HomeIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function HomePage() {
  const { t } = useTranslation("home");
  const features = t("features", { returnObjects: true }) as Record<string, string>;

  return (
    <ComingSoon
      icon={<HomeIcon className="size-8" />}
      title={t("title")}
      description={t("description")}
      features={Object.values(features)}
      version="v1.0"
    />
  );
}
