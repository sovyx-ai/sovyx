import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { MessageSquareIcon } from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
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
  const parentRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);

  const virtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => parentRef.current,
    // Tuned to typical ChatBubble height (1–3 lines + avatar + timestamp).
    // Rows remeasure via `measureElement`, so inexact estimate is fine.
    estimateSize: () => 96,
    overscan: 6,
    getItemKey: (index) => messages[index]?.id ?? index,
  });

  // Auto-scroll to the newest message when the thread grows.
  useEffect(() => {
    if (messages.length > prevCountRef.current && messages.length > 0) {
      virtualizer.scrollToIndex(messages.length - 1, { align: "end" });
    }
    prevCountRef.current = messages.length;
  }, [messages.length, virtualizer]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-[var(--svx-color-brand-primary)] border-t-transparent" />
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <EmptyState
        icon={<MessageSquareIcon className="size-8" />}
        title={t("list.empty")}
        description={t("detail.emptyThread")}
        className="h-full"
      />
    );
  }

  return (
    <div
      ref={parentRef}
      className="h-full overflow-auto contain-strict"
      style={{ overflowAnchor: "none" }}
    >
      <div
        style={{
          height: virtualizer.getTotalSize(),
          width: "100%",
          position: "relative",
        }}
        className="py-4"
      >
        {virtualizer.getVirtualItems().map((virtualRow) => {
          const msg = messages[virtualRow.index];
          if (!msg) return null;
          return (
            <div
              key={virtualRow.key}
              data-index={virtualRow.index}
              ref={virtualizer.measureElement}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${virtualRow.start}px)`,
              }}
            >
              <ChatBubble message={msg} participantName={participantName} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
