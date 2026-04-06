/**
 * AppSidebar — Main navigation sidebar.
 *
 * Uses shadcn Sidebar component with built-in mobile Sheet drawer.
 * 3 nav groups: Core (6 pages), Upcoming (5 placeholder pages), System (about).
 * Footer: connection dot + uptime + version.
 * Mind switcher: placeholder for multi-mind (v0.5).
 *
 * Ref: Architecture §4, DASH-26/27/28
 */

import { useLocation, Link } from "react-router";
import {
  LayoutDashboardIcon,
  MessageSquareIcon,
  BrainIcon,
  ScrollTextIcon,
  SettingsIcon,
  InfoIcon,
  MicIcon,
  HeartIcon,
  ListTodoIcon,
  PuzzleIcon,
  HomeIcon,
  ChevronsUpDownIcon,
  ActivityIcon,
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
import { StatusDot } from "@/components/dashboard/status-dot";
import { useDashboardStore } from "@/stores/dashboard";

const CORE_NAV = [
  { title: "Overview", icon: LayoutDashboardIcon, path: "/" },
  { title: "Conversations", icon: MessageSquareIcon, path: "/conversations" },
  { title: "Brain Explorer", icon: BrainIcon, path: "/brain" },
  { title: "Logs", icon: ScrollTextIcon, path: "/logs" },
  { title: "Settings", icon: SettingsIcon, path: "/settings" },
] as const;

const UPCOMING_NAV = [
  { title: "Voice", icon: MicIcon, path: "/voice" },
  { title: "Emotions", icon: HeartIcon, path: "/emotions" },
  { title: "Productivity", icon: ListTodoIcon, path: "/productivity" },
  { title: "Plugins", icon: PuzzleIcon, path: "/plugins" },
  { title: "Home", icon: HomeIcon, path: "/home" },
] as const;

export function AppSidebar() {
  const location = useLocation();
  const status = useDashboardStore((s) => s.status);
  const connectionState = useDashboardStore((s) => s.connectionState);

  return (
    <Sidebar collapsible="icon" aria-label="Main navigation">
      {/* ── Mind Switcher (DASH-26 placeholder) ── */}
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" render={<Link to="/" />}>
              <div className="flex aspect-square size-8 items-center justify-center rounded-[var(--svx-radius-md)] bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)]">
                <ActivityIcon className="size-4" />
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold text-[var(--svx-color-text-primary)]">
                  🔮 {status?.mind_name ?? "Sovyx"}
                </span>
                <span className="truncate text-xs text-[var(--svx-color-text-tertiary)]">
                  {connectionState === "connected" ? "Online" : connectionState === "reconnecting" ? "Reconnecting..." : "Connecting..."}
                </span>
              </div>
              <ChevronsUpDownIcon className="ml-auto size-4 text-[var(--svx-color-text-disabled)]" />
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        {/* ── Core Navigation (DASH-27) ── */}
        <SidebarGroup>
          <SidebarGroupLabel>Core</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {CORE_NAV.map((item) => (
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

        {/* ── Upcoming Features ── */}
        <SidebarGroup>
          <SidebarGroupLabel>Upcoming</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {UPCOMING_NAV.map((item) => (
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
      </SidebarContent>

      {/* ── Footer (DASH-28) ── */}
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link to="/about" />}
              isActive={location.pathname === "/about"}
              tooltip="About"
              size="sm"
            >
              <InfoIcon />
              <span>About</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton size="sm">
              <StatusDot
                status={connectionState === "connected" ? "online" : connectionState === "reconnecting" ? "thinking" : "offline"}
                size="sm"
              />
              <span className="text-xs text-[var(--svx-color-text-tertiary)]">
                {connectionState === "reconnecting"
                  ? "Reconnecting..."
                  : status
                    ? `Up ${formatUptime(status.uptime_seconds)}`
                    : "Connecting..."}
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <div className="px-2 py-1">
              <span className="text-[10px] text-[var(--svx-color-text-disabled)]">
                Sovyx v{status?.version ?? "0.1.0"}
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
