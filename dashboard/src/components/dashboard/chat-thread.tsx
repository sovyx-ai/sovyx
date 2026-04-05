import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { MessageSquareIcon } from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { EmptyState } from "@/components/empty-state";
import { ChatBubble } from "./chat-bubble";
import type { Message } from "@/types/api";

interface ChatThreadProps {
  messages: Message[];
  participantName: string;
  loading?: boolean;
}

export function ChatThread({ messages, participantName, loading }: ChatThreadProps) {
  const { t } = useTranslation("conversations");
  const bottomRef = useRef<HTMLDivElement>(null);

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <EmptyState
        icon={<MessageSquareIcon className="size-8" />}
        title={t("list.empty")}
        description="No messages in this conversation yet."
        className="h-full"
      />
    );
  }

  return (
    <ScrollArea className="h-full">
      <div className="space-y-1 py-4">
        {messages.map((msg) => (
          <ChatBubble
            key={msg.id}
            message={msg}
            participantName={participantName}
          />
        ))}
        <div ref={bottomRef} />
      </div>
    </ScrollArea>
  );
}
