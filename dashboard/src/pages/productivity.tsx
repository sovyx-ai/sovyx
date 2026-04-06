import { useTranslation } from "react-i18next";
import { ListTodoIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function ProductivityPage() {
  const { t } = useTranslation("productivity");
  const features = t("features", { returnObjects: true }) as Record<string, string>;

  return (
    <ComingSoon
      icon={<ListTodoIcon className="size-8" />}
      title={t("title")}
      description={t("description")}
      features={Object.values(features)}
      version="v1.0"
    />
  );
}
