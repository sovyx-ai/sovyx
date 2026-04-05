import { ListTodoIcon } from "lucide-react";
import { ComingSoon } from "@/components/coming-soon";

export default function ProductivityPage() {
  return (
    <ComingSoon
      icon={<ListTodoIcon className="size-8" />}
      title="Daily Productivity"
      description="Morning briefings, task management, habit tracking, and daily journal — all managed by your Mind."
      features={[
        "Morning briefing summary",
        "Task manager with priorities",
        "Habit tracker & streaks",
        "Daily journal entries",
        "Calendar sync",
      ]}
      version="v1.0"
    />
  );
}
