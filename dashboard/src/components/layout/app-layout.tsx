import { Outlet } from "react-router";
import { SidebarProvider, SidebarInset, SidebarTrigger } from "@/components/ui/sidebar";
import { Separator } from "@/components/ui/separator";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { AppSidebar } from "./app-sidebar";
import { useWebSocket } from "@/hooks/use-websocket";

export function AppLayout() {
  // Connect WebSocket at layout level (stays alive across page navigations)
  useWebSocket();

  return (
    <TooltipProvider>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
            <SidebarTrigger className="-ml-1" />
            <Separator orientation="vertical" className="mr-2 h-4" />
            <PageBreadcrumb />
          </header>
          <main className="flex-1 overflow-auto p-4 md:p-6">
            <Outlet />
          </main>
        </SidebarInset>
      </SidebarProvider>
      <Toaster />
    </TooltipProvider>
  );
}

function PageBreadcrumb() {
  // Simple breadcrumb — will be enhanced per-page later
  return (
    <nav className="text-sm text-muted-foreground">
      <span className="font-medium text-foreground">Sovyx Dashboard</span>
    </nav>
  );
}
