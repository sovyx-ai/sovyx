import { memo } from "react";
import { MarkdownContent } from "./markdown-content";

interface StreamingMessageProps {
  text: string;
  complete: boolean;
}

function StreamingMessageImpl({ text, complete }: StreamingMessageProps) {
  if (!text) return null;

  return (
    <div className="relative">
      <MarkdownContent content={text} />
      {!complete && (
        <span className="ml-0.5 inline-block h-4 w-[2px] animate-[cursor-blink_1s_step-end_infinite] bg-[var(--svx-color-brand-primary)]" />
      )}
    </div>
  );
}

export const StreamingMessage = memo(StreamingMessageImpl);
