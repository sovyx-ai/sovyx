import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router";
import { useTranslation } from "react-i18next";
import {
  LayoutDashboardIcon,
  MessageSquareIcon,
  BrainIcon,
  FileTextIcon,
  SettingsIcon,
  InfoIcon,
  MicIcon,
  HeartIcon,
  ListTodoIcon,
  PuzzleIcon,
  HomeIcon,
  TrashIcon,
  RefreshCwIcon,
} from "lucide-react";
import {
  CommandDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandSeparator,
} from "@/components/ui/command";
import { useDashboardStore } from "@/stores/dashboard";

interface CommandAction {
  id: string;
  label: string;
  icon: React.ReactNode;
  action: () => void;
  group: "navigation" | "actions";
  keywords?: string[];
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const { t } = useTranslation("common");
  const clearLogs = useDashboardStore((s) => s.clearLogs);

  // Cmd+K / Ctrl+K to open
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  const run = useCallback(
    (fn: () => void) => {
      fn();
      setOpen(false);
    },
    [],
  );

  const actions: CommandAction[] = [
    {
      id: "nav-overview",
      label: t("nav.overview"),
      icon: <LayoutDashboardIcon className="size-4" />,
      action: () => run(() => navigate("/")),
      group: "navigation",
      keywords: ["home", "dashboard", "status"],
    },
    {
      id: "nav-conversations",
      label: t("nav.conversations"),
      icon: <MessageSquareIcon className="size-4" />,
      action: () => run(() => navigate("/conversations")),
      group: "navigation",
      keywords: ["chat", "messages"],
    },
    {
      id: "nav-brain",
      label: t("nav.brain"),
      icon: <BrainIcon className="size-4" />,
      action: () => run(() => navigate("/brain")),
      group: "navigation",
      keywords: ["graph", "knowledge", "memory", "concepts"],
    },
    {
      id: "nav-logs",
      label: t("nav.logs"),
      icon: <FileTextIcon className="size-4" />,
      action: () => run(() => navigate("/logs")),
      group: "navigation",
      keywords: ["debug", "errors"],
    },
    {
      id: "nav-settings",
      label: t("nav.settings"),
      icon: <SettingsIcon className="size-4" />,
      action: () => run(() => navigate("/settings")),
      group: "navigation",
      keywords: ["config", "preferences"],
    },
    {
      id: "nav-about",
      label: "About",
      icon: <InfoIcon className="size-4" />,
      action: () => run(() => navigate("/about")),
      group: "navigation",
      keywords: ["version", "license", "info"],
    },
    {
      id: "nav-voice",
      label: t("nav.voice"),
      icon: <MicIcon className="size-4" />,
      action: () => run(() => navigate("/voice")),
      group: "navigation",
      keywords: ["stt", "tts", "microphone", "speech"],
    },
    {
      id: "nav-emotions",
      label: t("nav.emotions"),
      icon: <HeartIcon className="size-4" />,
      action: () => run(() => navigate("/emotions")),
      group: "navigation",
      keywords: ["mood", "feelings", "pad", "emotional"],
    },
    {
      id: "nav-productivity",
      label: t("nav.productivity"),
      icon: <ListTodoIcon className="size-4" />,
      action: () => run(() => navigate("/productivity")),
      group: "navigation",
      keywords: ["tasks", "habits", "journal", "briefing"],
    },
    {
      id: "nav-plugins",
      label: t("nav.plugins"),
      icon: <PuzzleIcon className="size-4" />,
      action: () => run(() => navigate("/plugins")),
      group: "navigation",
      keywords: ["extensions", "marketplace", "install"],
    },
    {
      id: "nav-home",
      label: t("nav.home"),
      icon: <HomeIcon className="size-4" />,
      action: () => run(() => navigate("/home")),
      group: "navigation",
      keywords: ["smart home", "automation", "ha", "home assistant"],
    },
    {
      id: "action-clear-logs",
      label: "Clear Logs",
      icon: <TrashIcon className="size-4" />,
      action: () => run(() => clearLogs()),
      group: "actions",
      keywords: ["delete", "reset"],
    },
    {
      id: "action-refresh",
      label: "Refresh Data",
      icon: <RefreshCwIcon className="size-4" />,
      action: () => run(() => window.location.reload()),
      group: "actions",
      keywords: ["reload"],
    },
  ];

  const navActions = actions.filter((a) => a.group === "navigation");
  const quickActions = actions.filter((a) => a.group === "actions");

  return (
    <CommandDialog open={open} onOpenChange={setOpen}>
      <CommandInput placeholder="Type a command or search..." />
      <CommandList>
        <CommandEmpty>No results found.</CommandEmpty>
        <CommandGroup heading="Navigation">
          {navActions.map((item) => (
            <CommandItem
              key={item.id}
              value={[item.label, ...(item.keywords ?? [])].join(" ")}
              onSelect={item.action}
            >
              {item.icon}
              <span>{item.label}</span>
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Actions">
          {quickActions.map((item) => (
            <CommandItem
              key={item.id}
              value={[item.label, ...(item.keywords ?? [])].join(" ")}
              onSelect={item.action}
            >
              {item.icon}
              <span>{item.label}</span>
            </CommandItem>
          ))}
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
