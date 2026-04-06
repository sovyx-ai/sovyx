import { Link } from "react-router";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Home } from "lucide-react";

export default function NotFoundPage() {
  const { t } = useTranslation("common");

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-center">
      <div className="text-6xl font-bold text-[var(--svx-color-brand-primary)]">S</div>
      <h1 className="text-3xl font-bold">404</h1>
      <p className="text-[var(--svx-color-text-secondary)]">
        {t("errors.notFoundPage")}
      </p>
      <Button render={<Link to="/" />}>
        <Home className="mr-2 size-4" />
        {t("errors.backToOverview")}
      </Button>
    </div>
  );
}
