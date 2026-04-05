import { useLocation, Link } from "react-router";
import {
  LayoutDashboard,
  MessageSquare,
  Brain,
  ScrollText,
  Settings,
  Activity,
  PuzzleIcon,
  ChevronsUpDownIcon,
  InfoIcon,
} from "lucide-react";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import { useDashboardStore } from "@/stores/dashboard";

const NAV_ITEMS = [
  { title: "Overview", icon: LayoutDashboard, path: "/" },
  { title: "Conversations", icon: MessageSquare, path: "/conversations" },
  { title: "Brain Explorer", icon: Brain, path: "/brain" },
  { title: "Logs", icon: ScrollText, path: "/logs" },
  { title: "Settings", icon: Settings, path: "/settings" },
  { title: "About", icon: InfoIcon, path: "/about" },
] as const;

function ConnectionDot() {
  const connected = useDashboardStore((s) => s.connected);
  return (
    <span
      className={connected ? "status-dot-green" : "status-dot-red"}
      title={connected ? "Connected" : "Disconnected"}
    />
  );
}

export function AppSidebar() {
  const location = useLocation();
  const status = useDashboardStore((s) => s.status);

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" render={<Link to="/" />}>
              <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                <Activity className="size-4" />
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold">🔮 Sovyx</span>
                <span className="truncate text-xs text-muted-foreground">
                  {status?.mind_name ?? "Loading..."}
                </span>
              </div>
              <ChevronsUpDownIcon className="ml-auto size-4 text-muted-foreground/50" />
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Navigation</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV_ITEMS.map((item) => (
                <SidebarMenuItem key={item.path}>
                  <SidebarMenuButton
                    render={<Link to={item.path} />}
                    isActive={location.pathname === item.path}
                    tooltip={item.title}
                  >
                    <item.icon />
                    <span>{item.title}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {/* v1.0 Placeholders */}
        <SidebarGroup>
          <SidebarGroupLabel>Coming Soon</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton disabled tooltip="Plugins — v1.0">
                  <PuzzleIcon />
                  <span className="text-muted-foreground">Plugins</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="sm">
              <ConnectionDot />
              <span className="text-xs text-muted-foreground">
                {status
                  ? `Up ${formatUptime(status.uptime_seconds)}`
                  : "Connecting..."}
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <div className="px-2 py-1">
              <span className="text-[10px] text-muted-foreground/50">
                Sovyx v0.5.0-dev
              </span>
            </div>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  const mins = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}
