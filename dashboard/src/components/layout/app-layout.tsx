import { useEffect } from "react";
import { Outlet, useLocation } from "react-router";
import { useTranslation } from "react-i18next";
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
import { NeuralMesh } from "@/components/dashboard/neural-mesh";

/** Maps route paths to nav i18n keys for document.title (WCAG 2.4.2). */
const ROUTE_TITLE_KEYS: Record<string, string> = {
  "/": "nav.overview",
  "/conversations": "nav.conversations",
  "/brain": "nav.brain",
  "/logs": "nav.logs",
  "/settings": "nav.settings",
  "/about": "nav.about",
  "/voice": "nav.voice",
  "/voice/health": "nav.voiceHealth",
  "/voice/platform-diagnostics": "nav.voicePlatformDiagnostics",
};

export function AppLayout() {
  const { t } = useTranslation("common");
  const location = useLocation();

  // Connect WebSocket at layout level (stays alive across page navigations)
  useWebSocket();

  // Update document.title per route (WCAG 2.4.2 — Page Titled)
  useEffect(() => {
    const key = ROUTE_TITLE_KEYS[location.pathname];
    document.title = key ? `${t(key)} — Sovyx` : "Sovyx";
  }, [location.pathname, t]);

  return (
    <TooltipProvider>
      <a href="#main-content" className="skip-nav">
        {t("sidebar.skipToContent", "Skip to main content")}
      </a>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <header
            className="flex h-12 shrink-0 items-center gap-2 border-b border-[var(--svx-color-border-subtle)] px-2 sm:px-4"
            role="banner"
          >
            <SidebarTrigger className="-ml-1" aria-label={t("sidebar.collapse")} />
            <Separator orientation="vertical" className="mr-2 h-4" aria-hidden="true" />
            <Breadcrumb />
            <div className="ml-auto flex items-center gap-2">
              <Button
                variant="ghost"
                size="icon"
                className="relative size-8 text-[var(--svx-color-text-secondary)]"
                disabled
                aria-label={t("sidebar.notifications", "Notifications")}
              >
                <BellIcon className="size-4" aria-hidden="true" />
              </Button>
              <kbd
                className="hidden items-center gap-1 rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-subtle)] px-1.5 py-0.5 font-code text-[10px] text-[var(--svx-color-text-tertiary)] md:inline-flex"
                aria-label={t("sidebar.cmdKLabel", "Press Command+K to open command palette")}
              >
                ⌘K
              </kbd>
            </div>
          </header>
          <main id="main-content" className="relative flex-1 overflow-auto p-4 md:p-6" role="main">
            <NeuralMesh />
            <div className="relative">
              <PageTransition key={location.pathname}>
                <Outlet />
              </PageTransition>
            </div>
          </main>
        </SidebarInset>
      </SidebarProvider>
      <Toaster />
      <CommandPalette />
    </TooltipProvider>
  );
}
