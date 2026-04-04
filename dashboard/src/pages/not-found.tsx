import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import { Home } from "lucide-react";

export default function NotFoundPage() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-center">
      <div className="text-6xl">🔮</div>
      <h1 className="text-3xl font-bold">404</h1>
      <p className="text-muted-foreground">
        This page doesn&apos;t exist in Sovyx&apos;s memory.
      </p>
      <Button render={<Link to="/" />}>
        <Home className="mr-2 size-4" />
        Back to Overview
      </Button>
    </div>
  );
}
