import { useLocation, Link } from "react-router";
import {
  LayoutDashboard,
  MessageSquare,
  Brain,
  ScrollText,
  Settings,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

interface RouteInfo {
  label: string;
  icon: LucideIcon;
}

const ROUTE_MAP: Record<string, RouteInfo> = {
  "/": { label: "Overview", icon: LayoutDashboard },
  "/conversations": { label: "Conversations", icon: MessageSquare },
  "/brain": { label: "Brain Explorer", icon: Brain },
  "/logs": { label: "Logs", icon: ScrollText },
  "/settings": { label: "Settings", icon: Settings },
};

export function Breadcrumb() {
  const location = useLocation();
  const route = ROUTE_MAP[location.pathname];

  if (!route) {
    return (
      <nav className="flex items-center gap-2 text-sm" aria-label="Breadcrumb">
        <Link to="/" className="text-muted-foreground hover:text-foreground transition-colors">
          Sovyx
        </Link>
        <span className="text-muted-foreground" aria-hidden="true">/</span>
        <span className="text-foreground" aria-current="page">Not Found</span>
      </nav>
    );
  }

  const Icon = route.icon;

  return (
    <nav className="flex items-center gap-2 text-sm" aria-label="Breadcrumb">
      {location.pathname !== "/" && (
        <>
          <Link
            to="/"
            className="text-muted-foreground transition-colors hover:text-foreground"
          >
            Sovyx
          </Link>
          <span className="text-muted-foreground" aria-hidden="true">/</span>
        </>
      )}
      <div className="flex items-center gap-1.5">
        <Icon className="size-3.5 text-muted-foreground" aria-hidden="true" />
        <span className="font-medium text-foreground" aria-current="page">{route.label}</span>
      </div>
    </nav>
  );
}
