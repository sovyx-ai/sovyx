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
      label: t("nav.about"),
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
      id: "action-clear-logs",
      label: t("command.clearLogs"),
      icon: <TrashIcon className="size-4" />,
      action: () => run(() => clearLogs()),
      group: "actions",
      keywords: ["delete", "reset"],
    },
    {
      id: "action-refresh",
      label: t("command.refreshData"),
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
      <CommandInput placeholder={t("command.placeholder")} />
      <CommandList>
        <CommandEmpty>{t("command.empty")}</CommandEmpty>
        <CommandGroup heading={t("command.navigation")}>
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
        <CommandGroup heading={t("command.actions")}>
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
