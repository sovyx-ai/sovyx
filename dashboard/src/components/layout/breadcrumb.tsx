import { useLocation, Link } from "react-router";
import {
  LayoutDashboard,
  MessageSquare,
  Brain,
  ScrollText,
  Settings,
  Mic,
  Info,
  Heart,
  ListTodo,
  Puzzle,
  Home,
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
  "/voice": { label: "Voice Pipeline", icon: Mic },
  "/about": { label: "About", icon: Info },
  "/emotions": { label: "Emotional Intelligence", icon: Heart },
  "/productivity": { label: "Productivity", icon: ListTodo },
  "/plugins": { label: "Plugin Marketplace", icon: Puzzle },
  "/home": { label: "Home Integration", icon: Home },
};

/**
 * Normalize pathname and find matching route.
 * Handles trailing slashes and nested paths (e.g. /conversations/123 → /conversations).
 */
function resolveRoute(pathname: string): RouteInfo | undefined {
  // Strip trailing slash (except root)
  const normalized = pathname.length > 1 && pathname.endsWith("/")
    ? pathname.slice(0, -1)
    : pathname;

  // Exact match first
  if (ROUTE_MAP[normalized]) return ROUTE_MAP[normalized];

  // Prefix match for nested routes (e.g. /conversations/abc → /conversations)
  const base = "/" + normalized.split("/").filter(Boolean)[0];
  return ROUTE_MAP[base];
}

export function Breadcrumb() {
  const location = useLocation();
  const route = resolveRoute(location.pathname);

  if (!route) {
    return (
      <nav className="flex items-center gap-2 text-sm" aria-label="Breadcrumb">
        <Link to="/" className="text-[var(--svx-color-text-secondary)] hover:text-[var(--svx-color-text-primary)] transition-colors">
          Sovyx
        </Link>
        <span className="text-[var(--svx-color-text-secondary)]" aria-hidden="true">/</span>
        <span className="text-[var(--svx-color-text-primary)]" aria-current="page">Not Found</span>
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
            className="text-[var(--svx-color-text-secondary)] transition-colors hover:text-[var(--svx-color-text-primary)]"
          >
            Sovyx
          </Link>
          <span className="text-[var(--svx-color-text-secondary)]" aria-hidden="true">/</span>
        </>
      )}
      <div className="flex items-center gap-1.5">
        <Icon className="size-3.5 text-[var(--svx-color-text-secondary)]" aria-hidden="true" />
        <span className="font-medium text-[var(--svx-color-text-primary)]" aria-current="page">{route.label}</span>
      </div>
    </nav>
  );
}
