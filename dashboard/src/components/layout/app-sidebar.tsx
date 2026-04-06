/**
 * AppSidebar — Main navigation sidebar.
 *
 * Uses shadcn Sidebar component with built-in mobile Sheet drawer.
 * 3 nav groups: Core (6 pages), Upcoming (5 placeholder pages), System (about).
 * Footer: connection dot + uptime + version.
 * Mind switcher: placeholder for multi-mind (v0.5).
 *
 * FINAL-08: Full i18n — zero hardcoded English strings.
 *
 * Ref: Architecture §4, DASH-26/27/28
 */

import { useLocation, Link } from "react-router";
import { useTranslation } from "react-i18next";
import { formatUptime } from "@/lib/format";
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
import type { LucideIcon } from "lucide-react";
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

interface NavItem {
  titleKey: string;
  icon: LucideIcon;
  path: string;
}

const CORE_NAV: NavItem[] = [
  { titleKey: "nav.overview", icon: LayoutDashboardIcon, path: "/" },
  { titleKey: "nav.conversations", icon: MessageSquareIcon, path: "/conversations" },
  { titleKey: "nav.brain", icon: BrainIcon, path: "/brain" },
  { titleKey: "nav.logs", icon: ScrollTextIcon, path: "/logs" },
  { titleKey: "nav.settings", icon: SettingsIcon, path: "/settings" },
];

const UPCOMING_NAV: NavItem[] = [
  { titleKey: "nav.voice", icon: MicIcon, path: "/voice" },
  { titleKey: "nav.emotions", icon: HeartIcon, path: "/emotions" },
  { titleKey: "nav.productivity", icon: ListTodoIcon, path: "/productivity" },
  { titleKey: "nav.plugins", icon: PuzzleIcon, path: "/plugins" },
  { titleKey: "nav.home", icon: HomeIcon, path: "/home" },
];

export function AppSidebar() {
  const { t } = useTranslation("common");
  const location = useLocation();
  const status = useDashboardStore((s) => s.status);
  const connectionState = useDashboardStore((s) => s.connectionState);

  const connectionLabel =
    connectionState === "connected"
      ? t("status.online")
      : connectionState === "reconnecting"
        ? t("status.reconnecting")
        : t("sidebar.connecting");

  const uptimeLabel =
    connectionState === "reconnecting"
      ? t("status.reconnecting")
      : status
        ? t("sidebar.uptime", { duration: formatUptime(status.uptime_seconds) })
        : t("sidebar.connecting");

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
                  {connectionLabel}
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
          <SidebarGroupLabel>{t("sidebar.core")}</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {CORE_NAV.map((item) => (
                <SidebarMenuItem key={item.path}>
                  <SidebarMenuButton
                    render={<Link to={item.path} />}
                    isActive={location.pathname === item.path}
                    tooltip={t(item.titleKey)}
                  >
                    <item.icon />
                    <span>{t(item.titleKey)}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {/* ── Upcoming Features ── */}
        <SidebarGroup>
          <SidebarGroupLabel>{t("sidebar.upcoming")}</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {UPCOMING_NAV.map((item) => (
                <SidebarMenuItem key={item.path}>
                  <SidebarMenuButton
                    render={<Link to={item.path} />}
                    isActive={location.pathname === item.path}
                    tooltip={t(item.titleKey)}
                  >
                    <item.icon />
                    <span>{t(item.titleKey)}</span>
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
              tooltip={t("nav.about")}
              size="sm"
            >
              <InfoIcon />
              <span>{t("nav.about")}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton size="sm">
              <StatusDot
                status={connectionState === "connected" ? "online" : connectionState === "reconnecting" ? "thinking" : "offline"}
                size="sm"
              />
              <span className="text-xs text-[var(--svx-color-text-tertiary)]">
                {uptimeLabel}
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <div className="px-2 py-1">
              <span className="text-[10px] text-[var(--svx-color-text-disabled)]">
                {t("app.name")} {t("app.version", { version: status?.version ?? "0.1.0" })}
              </span>
            </div>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}


