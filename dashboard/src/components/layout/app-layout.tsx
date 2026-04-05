import { useEffect } from "react";
import { Outlet, useLocation } from "react-router";
import { SidebarProvider, SidebarInset, SidebarTrigger } from "@/components/ui/sidebar";
import { Separator } from "@/components/ui/separator";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { AppSidebar } from "./app-sidebar";
import { Breadcrumb } from "./breadcrumb";
import { PageTransition } from "./page-transition";
import { BellIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useWebSocket } from "@/hooks/use-websocket";
import { CommandPalette } from "@/components/command-palette";

/** Maps route paths to page titles (WCAG 2.4.2). */
const ROUTE_TITLES: Record<string, string> = {
  "/": "Overview — Sovyx",
  "/conversations": "Conversations — Sovyx",
  "/brain": "Brain — Sovyx",
  "/logs": "Logs — Sovyx",
  "/settings": "Settings — Sovyx",
  "/about": "About — Sovyx",
  "/voice": "Voice — Sovyx",
  "/emotions": "Emotions — Sovyx",
  "/productivity": "Productivity — Sovyx",
  "/plugins": "Plugins — Sovyx",
  "/home": "Home — Sovyx",
};

export function AppLayout() {
  const location = useLocation();

  // Connect WebSocket at layout level (stays alive across page navigations)
  useWebSocket();

  // Update document.title per route (WCAG 2.4.2 — Page Titled)
  useEffect(() => {
    document.title = ROUTE_TITLES[location.pathname] ?? "Sovyx";
  }, [location.pathname]);

  return (
    <TooltipProvider>
      <a href="#main-content" className="skip-nav">
        Skip to main content
      </a>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <header
            className="flex h-12 shrink-0 items-center gap-2 border-b border-[var(--svx-color-border-subtle)] px-2 sm:px-4"
            role="banner"
          >
            <SidebarTrigger className="-ml-1" aria-label="Toggle sidebar" />
            <Separator orientation="vertical" className="mr-2 h-4" aria-hidden="true" />
            <Breadcrumb />
            <div className="ml-auto flex items-center gap-2">
              <Button
                variant="ghost"
                size="icon"
                className="relative size-8 text-muted-foreground"
                disabled
                aria-label="Notifications — coming in v1.0"
              >
                <BellIcon className="size-4" aria-hidden="true" />
              </Button>
              <kbd
                className="hidden items-center gap-1 rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-subtle)] px-1.5 py-0.5 font-code text-[10px] text-[var(--svx-color-text-tertiary)] md:inline-flex"
                aria-label="Press Command+K to open command palette"
              >
                ⌘K
              </kbd>
            </div>
          </header>
          <main id="main-content" className="flex-1 overflow-auto p-4 md:p-6" role="main">
            <PageTransition key={location.pathname}>
              <Outlet />
            </PageTransition>
          </main>
        </SidebarInset>
      </SidebarProvider>
      <Toaster />
      <CommandPalette />
    </TooltipProvider>
  );
}
