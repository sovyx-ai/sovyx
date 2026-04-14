/**
 * Chat page — direct conversation with the mind via POST /api/chat.
 *
 * Features:
 * - Message input with Enter to send
 * - Scrollable message thread (user right, AI left)
 * - Loading indicator while AI processes
 * - Auto-scroll to latest message
 * - Conversation continuity via conversation_id
 * - New chat button to reset
 * - Error display with retry
 *
 * DASH-03: Core chat page for dashboard synergy mission.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  MessageSquareIcon,
  SendIcon,
  PlusIcon,
  Loader2Icon,
  AlertTriangleIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useDashboardStore } from "@/stores/dashboard";
import { api, isAbortError } from "@/lib/api";
import { ChatResponseSchema } from "@/types/schemas";
import { cn } from "@/lib/utils";
import { MarkdownContent } from "@/components/chat";
import { EmptyState } from "@/components/empty-state";
import { formatTimeShort } from "@/lib/format";
import { LetterAvatar, MindAvatar } from "@/components/dashboard/letter-avatar";
import type { ChatResponse } from "@/types/api";

/** Generate a local ID for optimistic UI messages. */
function localId(): string {
  return `local-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export default function ChatPage() {
  const { t } = useTranslation(["chat", "common"]);

  // ── Store state ──
  const messages = useDashboardStore((s) => s.chatMessages);
  const loading = useDashboardStore((s) => s.chatLoading);
  const conversationId = useDashboardStore((s) => s.chatConversationId);
  const error = useDashboardStore((s) => s.chatError);
  const addMessage = useDashboardStore((s) => s.addChatMessage);
  const setLoading = useDashboardStore((s) => s.setChatLoading);
  const setConversationId = useDashboardStore((s) => s.setChatConversationId);
  const setError = useDashboardStore((s) => s.setChatError);
  const clearChat = useDashboardStore((s) => s.clearChat);

  // ── Local state ──
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // ── Auto-scroll on new messages ──
  useEffect(() => {
    // scrollIntoView may not exist in test environments (jsdom)
    if (bottomRef.current && typeof bottomRef.current.scrollIntoView === "function") {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages.length, loading]);

  // ── Focus input on mount ──
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // ── Cleanup abort on unmount ──
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // ── Send message ──
  const sendMessage = useCallback(async () => {
    const text = input.trim();
    if (!text || loading) return;

    setInput("");
    setError(null);

    // Optimistic: add user message immediately
    const userMsg = {
      id: localId(),
      role: "user" as const,
      content: text,
      timestamp: new Date().toISOString(),
    };
    addMessage(userMsg);
    setLoading(true);

    // Cancel any in-flight request
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const resp = await api.post<ChatResponse>(
        "/api/chat",
        {
          message: text,
          user_name: "Dashboard",
          conversation_id: conversationId,
        },
        { signal: controller.signal, schema: ChatResponseSchema },
      );

      // Store conversation ID for continuity
      if (resp.conversation_id && resp.conversation_id !== conversationId) {
        setConversationId(resp.conversation_id);
      }

      // Add AI response
      const aiMsg = {
        id: localId(),
        role: "assistant" as const,
        content: resp.response,
        timestamp: resp.timestamp ?? new Date().toISOString(),
        mind_id: resp.mind_id,
      };
      addMessage(aiMsg);
    } catch (err) {
      if (isAbortError(err)) return;
      setError(t("error.loadFailed"));
    } finally {
      setLoading(false);
      // Re-focus input after send
      inputRef.current?.focus();
    }
  }, [input, loading, conversationId, addMessage, setLoading, setConversationId, setError, t]);

  // ── Handle keyboard ──
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void sendMessage();
      }
    },
    [sendMessage],
  );

  // ── Auto-resize textarea ──
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInput(e.target.value);
      // Auto-resize: reset then set to scrollHeight
      const el = e.target;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
    },
    [],
  );

  // ── New chat ──
  const handleNewChat = useCallback(() => {
    abortRef.current?.abort();
    clearChat();
    setInput("");
    inputRef.current?.focus();
  }, [clearChat]);

  return (
    <div className="flex h-full flex-col" data-testid="chat-page">
      {/* ── Header ── */}
      <div className="flex items-center justify-between border-b border-[var(--svx-color-border-subtle)] px-6 py-4">
        <div>
          <h1 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
            {t("title")}
          </h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">
            {t("subtitle")}
          </p>
        </div>
        {messages.length > 0 && (
          <Button
            variant="outline"
            size="sm"
            onClick={handleNewChat}
            className="gap-2"
          >
            <PlusIcon className="size-4" />
            {t("newChat")}
          </Button>
        )}
      </div>

      {/* ── Message Thread ── */}
      <ScrollArea className="flex-1">
        <div className="mx-auto max-w-3xl">
          {messages.length === 0 && !loading ? (
            <EmptyState
              icon={<MessageSquareIcon className="size-8" />}
              title={t("empty.title")}
              description={t("empty.description")}
              className="h-[60vh]"
            />
          ) : (
            <div className="space-y-1 py-4">
              {messages.map((msg) => (
                <div
                  key={msg.id}
                  className={cn(
                    "flex gap-3 px-4 py-2",
                    msg.role === "user" ? "flex-row-reverse" : "flex-row",
                  )}
                >
                  {/* Avatar */}
                  <div className="shrink-0 pt-1">
                    {msg.role === "user" ? (
                      <LetterAvatar name={t("you")} />
                    ) : (
                      <MindAvatar />
                    )}
                  </div>

                  {/* Bubble */}
                  <div
                    className={cn(
                      "max-w-[75%] space-y-1",
                      msg.role === "user" ? "items-end" : "items-start",
                    )}
                  >
                    <div
                      className={cn(
                        "rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
                        msg.role === "user"
                          ? "rounded-tr-sm bg-[var(--svx-color-brand-subtle)] text-[var(--svx-color-text-primary)]"
                          : "rounded-tl-sm bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-primary)]",
                      )}
                    >
                      {msg.role === "user" ? (
                        <p className="whitespace-pre-wrap break-words">{msg.content}</p>
                      ) : (
                        <MarkdownContent content={msg.content} />
                      )}
                    </div>
                    <span className="block px-1 text-[10px] text-[var(--svx-color-text-secondary)]">
                      {formatTimeShort(msg.timestamp)}
                    </span>
                  </div>
                </div>
              ))}

              {/* Loading indicator */}
              {loading && (
                <div className="flex gap-3 px-4 py-2" data-testid="chat-loading">
                  <div className="shrink-0 pt-1">
                    <MindAvatar />
                  </div>
                  <div className="flex items-center gap-2 rounded-2xl rounded-tl-sm bg-[var(--svx-color-bg-elevated)] px-4 py-2.5">
                    <Loader2Icon className="size-4 animate-spin text-[var(--svx-color-brand-primary)]" />
                    <span className="text-sm text-[var(--svx-color-text-secondary)]">
                      {t("sending")}
                    </span>
                  </div>
                </div>
              )}

              <div ref={bottomRef} />
            </div>
          )}
        </div>
      </ScrollArea>

      {/* ── Error Banner ── */}
      {error && (
        <div className="mx-auto flex max-w-3xl items-center gap-2 px-6 py-2 text-sm text-red-400">
          <AlertTriangleIcon className="size-4 shrink-0" />
          <span>{error}</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setError(null)}
            className="ml-auto text-xs"
          >
            {t("common:actions.dismiss")}
          </Button>
        </div>
      )}

      {/* ── Input Area ── */}
      <div className="border-t border-[var(--svx-color-border-subtle)] px-6 py-4">
        <div className="mx-auto flex max-w-3xl items-end gap-3">
          <textarea
            ref={inputRef}
            value={input}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            placeholder={t("placeholder")}
            disabled={loading}
            rows={1}
            className={cn(
              "flex-1 resize-none rounded-xl border border-[var(--svx-color-border-default)]",
              "bg-[var(--svx-color-bg-elevated)] px-4 py-3 text-sm",
              "text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)]",
              "focus:border-[var(--svx-color-brand-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--svx-color-brand-primary)]",
              "disabled:opacity-50",
              "transition-colors",
            )}
            data-testid="chat-input"
          />
          <Button
            onClick={() => void sendMessage()}
            disabled={!input.trim() || loading}
            size="icon"
            className={cn(
              "size-11 shrink-0 rounded-xl",
              "bg-[var(--svx-color-brand-primary)] hover:bg-[var(--svx-color-brand-hover)]",
              "text-[var(--svx-color-text-inverse)]",
              "disabled:opacity-50",
            )}
            data-testid="chat-send"
          >
            {loading ? (
              <Loader2Icon className="size-5 animate-spin" />
            ) : (
              <SendIcon className="size-5" />
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
