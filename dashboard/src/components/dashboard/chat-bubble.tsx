import type { Message } from "@/types/api";
import { LetterAvatar, MindAvatar } from "./letter-avatar";
import { formatTimeShort } from "@/lib/format";
import { cn } from "@/lib/utils";

interface ChatBubbleProps {
  message: Message;
  participantName: string;
}

export function ChatBubble({ message, participantName }: ChatBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div
      className={cn(
        "flex gap-3 px-4 py-2",
        isUser ? "flex-row" : "flex-row-reverse",
      )}
    >
      {/* Avatar */}
      <div className="shrink-0 pt-1">
        {isUser ? (
          <LetterAvatar name={participantName} />
        ) : (
          <MindAvatar />
        )}
      </div>

      {/* Bubble */}
      <div
        className={cn(
          "max-w-[75%] space-y-1",
          isUser ? "items-start" : "items-end",
        )}
      >
        <div
          className={cn(
            "rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
            isUser
              ? "rounded-tl-sm bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-primary)]"
              : "rounded-tr-sm bg-[var(--svx-color-brand-subtle)] text-[var(--svx-color-text-primary)]",
          )}
        >
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
        </div>
        <span className="block text-[10px] text-[var(--svx-color-text-secondary)] px-1">
          {formatTimeShort(message.timestamp)}
        </span>
      </div>
    </div>
  );
}
