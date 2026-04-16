import { useCallback, useRef, useState } from "react";
import { SendIcon, LoaderIcon, SparklesIcon } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Message {
  role: "assistant" | "user";
  content: string;
}

interface FirstChatStepProps {
  mindName: string;
  provider: string;
  model: string;
  onComplete: () => void;
}

export function FirstChatStep({ mindName, provider, model, onComplete }: FirstChatStepProps) {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      content:
        `Hi! I'm ${mindName}. I run entirely on your hardware \u2014 everything we discuss stays between us. Over time, I'll learn your preferences and get better at helping you.\n\nWhat's on your mind?`,
    },
  ]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [hasReplied, setHasReplied] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;

    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    setSending(true);

    try {
      const resp = await api.post<{ response: string }>("/api/chat", {
        message: text,
        user_name: "User",
      });
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: resp.response || "..." },
      ]);
      setHasReplied(true);
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: "I couldn't respond right now. You can try again or explore the dashboard.",
        },
      ]);
      setHasReplied(true);
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }, [input, sending]);

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
          Say Hello
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          This is a real conversation powered by {provider} ({model}).
          Everything stays on this machine.
        </p>
      </div>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]">
        <div className="max-h-[320px] min-h-[200px] overflow-y-auto p-4 space-y-3">
          {messages.map((msg, i) => (
            <div
              key={i}
              className={cn(
                "max-w-[85%] rounded-[var(--svx-radius-lg)] px-3.5 py-2.5 text-sm leading-relaxed",
                msg.role === "assistant"
                  ? "bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-primary)]"
                  : "ml-auto bg-[var(--svx-color-brand-primary)] text-white",
              )}
            >
              {msg.role === "assistant" && (
                <div className="mb-1 flex items-center gap-1 text-[10px] font-medium text-[var(--svx-color-text-tertiary)]">
                  <SparklesIcon className="size-3" />
                  {mindName}
                </div>
              )}
              <p className="whitespace-pre-wrap">{msg.content}</p>
            </div>
          ))}
          {sending && (
            <div className="flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]">
              <LoaderIcon className="size-3.5 animate-spin" />
              {mindName} is thinking...
            </div>
          )}
        </div>

        <div className="border-t border-[var(--svx-color-border-default)] p-3">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleSend();
            }}
            className="flex items-center gap-2"
          >
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Type your first message..."
              disabled={sending}
              className="flex-1 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 text-sm text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)] disabled:opacity-50"
              autoFocus
            />
            <Button
              type="submit"
              size="icon"
              disabled={!input.trim() || sending}
            >
              <SendIcon className="size-4" />
            </Button>
          </form>
        </div>
      </div>

      <div className="flex items-center justify-end">
        <Button onClick={onComplete} variant={hasReplied ? "default" : "outline"}>
          {hasReplied ? "Explore Dashboard" : "Skip to Dashboard"}
        </Button>
      </div>
    </div>
  );
}
