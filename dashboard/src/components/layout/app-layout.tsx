import { Outlet, useLocation } from "react-router";
import { AnimatePresence } from "framer-motion";
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

export function AppLayout() {
  const location = useLocation();

  // Connect WebSocket at layout level (stays alive across page navigations)
  useWebSocket();

  return (
    <TooltipProvider>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <header className="flex h-12 shrink-0 items-center gap-2 border-b px-2 sm:px-4">
            <SidebarTrigger className="-ml-1" />
            <Separator orientation="vertical" className="mr-2 h-4" />
            <Breadcrumb />
            <div className="ml-auto flex items-center gap-2">
              <Button
                variant="ghost"
                size="icon"
                className="relative size-8 text-muted-foreground"
                disabled
                title="Notifications — coming in v1.0"
              >
                <BellIcon className="size-4" />
              </Button>
              <kbd className="hidden items-center gap-1 rounded border border-border/50 bg-secondary/50 px-1.5 py-0.5 font-code text-[10px] text-muted-foreground md:inline-flex">
                ⌘K
              </kbd>
            </div>
          </header>
          <main className="flex-1 overflow-auto p-4 md:p-6">
            <AnimatePresence mode="wait">
              <PageTransition key={location.pathname}>
                <Outlet />
              </PageTransition>
            </AnimatePresence>
          </main>
        </SidebarInset>
      </SidebarProvider>
      <Toaster />
      <CommandPalette />
    </TooltipProvider>
  );
}
