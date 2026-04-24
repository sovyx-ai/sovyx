/**
 * Chat page — streaming conversation with cognitive transparency.
 *
 * Uses SSE (POST /api/chat/stream) for token-by-token rendering with
 * cognitive phase indicators. Falls back to batch POST /api/chat on
 * SSE failure.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  MessageSquareIcon,
  SendIcon,
  PlusIcon,
  Loader2Icon,
  AlertTriangleIcon,
  ArrowDownIcon,
  PanelLeftIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useDashboardStore } from "@/stores/dashboard";
import { api, apiFetch, ApiError, clearToken, isAbortError } from "@/lib/api";
import { ChatResponseSchema } from "@/types/schemas";
import { cn } from "@/lib/utils";
import { MarkdownContent, MessageTags, MessageMeta, CognitiveProgress, StreamingMessage } from "@/components/chat";
import { ConversationSidebar } from "@/components/chat/conversation-sidebar";
import { EmptyState } from "@/components/empty-state";
import { formatTimeShort } from "@/lib/format";
import { LetterAvatar, MindAvatar } from "@/components/dashboard/letter-avatar";
import { ErrorBoundary } from "@/components/error-boundary";
import type { ChatResponse, ChatMessage } from "@/types/api";

function localId(): string {
  return `local-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export default function ChatPage() {
  const { t } = useTranslation(["chat", "common"]);

  const messages = useDashboardStore((s) => s.chatMessages);
  const loading = useDashboardStore((s) => s.chatLoading);
  const conversationId = useDashboardStore((s) => s.chatConversationId);
  const error = useDashboardStore((s) => s.chatError);
  const addMessage = useDashboardStore((s) => s.addChatMessage);
  const setLoading = useDashboardStore((s) => s.setChatLoading);
  const setConversationId = useDashboardStore((s) => s.setChatConversationId);
  const setError = useDashboardStore((s) => s.setChatError);
  const clearChat = useDashboardStore((s) => s.clearChat);

  const [input, setInput] = useState("");
  const [lastMessage, setLastMessage] = useState("");
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [cogPhase, setCogPhase] = useState<{ phase: string; detail: string } | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [mood, setMood] = useState<{ label: string; quadrant: string } | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const userScrolledRef = useRef(false);

  // ── Smart auto-scroll ──
  useEffect(() => {
    if (!userScrolledRef.current && bottomRef.current && typeof bottomRef.current.scrollIntoView === "function") {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages.length, loading, streamingText]);

  useEffect(() => {
    const el = scrollAreaRef.current?.querySelector("[data-radix-scroll-area-viewport]");
    if (!el) return;
    const handleScroll = () => {
      const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      userScrolledRef.current = !near;
      setShowScrollBtn(!near);
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, []);

  const scrollToBottom = useCallback(() => {
    userScrolledRef.current = false;
    setShowScrollBtn(false);
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  useEffect(() => {
    api
      .get<{ label: string; quadrant: string; episode_count: number }>("/api/emotions/current")
      .then((r) => { if (r.episode_count > 0) setMood({ label: r.label, quadrant: r.quadrant }); })
      .catch(() => {});
  }, []);

  // ── Send message (SSE streaming with batch fallback) ──
  const sendMessageText = useCallback(async (text: string) => {
    if (!text || loading) return;

    setInput("");
    setError(null);
    setLastMessage(text);
    setStreamingText("");
    setCogPhase(null);

    addMessage({
      id: localId(),
      role: "user",
      content: text,
      timestamp: new Date().toISOString(),
    });
    setLoading(true);
    userScrolledRef.current = false;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      // Try SSE streaming first. Routed through apiFetch so the auth header
      // is injected by a single code path (anti-pattern #18) — we can't use
      // api.post() because SSE needs the raw Response body reader, not the
      // parsed JSON. 401 is handled explicitly below to mirror request()'s
      // clear-token-and-show-modal behavior.
      const resp = await apiFetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          user_name: "Dashboard",
          conversation_id: conversationId,
        }),
        signal: controller.signal,
      });

      if (resp.status === 401) {
        clearToken();
        useDashboardStore.getState().setAuthenticated(false);
        useDashboardStore.getState().setShowTokenModal(true);
        throw new ApiError(401, "Unauthorized");
      }

      if (!resp.ok || !resp.body) {
        throw new Error("SSE not available");
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let accumulated = "";
      let doneData: Record<string, unknown> | null = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        let currentEventType = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) {
            currentEventType = line.slice(7).trim();
          } else if (line.startsWith("data: ") && currentEventType) {
            const data = line.slice(6);
            try {
              const parsed = JSON.parse(data) as Record<string, unknown>;
              if (currentEventType === "token") {
                const chunk = String(parsed.text ?? "");
                accumulated += chunk;
                setStreamingText(accumulated);
              } else if (currentEventType === "phase") {
                setCogPhase({
                  phase: String(parsed.phase ?? ""),
                  detail: String(parsed.detail ?? ""),
                });
              } else if (currentEventType === "done") {
                doneData = parsed;
              } else if (currentEventType === "error") {
                throw new Error(String(parsed.error ?? "Stream error"));
              }
            } catch (e) {
              if (e instanceof Error && e.message !== "Stream error") {
                // JSON parse error — skip malformed event
              } else {
                throw e;
              }
            }
            currentEventType = "";
          } else if (line === "") {
            currentEventType = "";
          }
        }
      }

      // Finalize
      setCogPhase(null);
      setStreamingText("");

      const finalResponse = String(doneData?.response ?? accumulated);
      const finalConvId = String(doneData?.conversation_id ?? conversationId ?? "");
      const finalTags = (doneData?.tags as string[] | undefined) ?? ["brain"];
      if (finalConvId && finalConvId !== conversationId) {
        setConversationId(finalConvId);
      }

      const aiMsg: ChatMessage = {
        id: localId(),
        role: "assistant",
        content: finalResponse,
        timestamp: (doneData?.timestamp as string) ?? new Date().toISOString(),
        tags: finalTags,
        mind_id: doneData?.mind_id as string | undefined,
        model: doneData?.model as string | undefined,
        tokens_in: doneData?.tokens_in as number | undefined,
        tokens_out: doneData?.tokens_out as number | undefined,
        cost_usd: doneData?.cost_usd as number | undefined,
        latency_ms: doneData?.latency_ms as number | undefined,
        provider: doneData?.provider as string | undefined,
      };
      addMessage(aiMsg);

    } catch (err) {
      if (isAbortError(err)) return;
      // 401 already cleared the token and opened the modal — don't
      // retry via the batch endpoint (would trigger a second modal).
      if (err instanceof ApiError && err.status === 401) {
        setLoading(false);
        setStreamingText("");
        setCogPhase(null);
        return;
      }

      // Fallback to batch endpoint
      try {
        const resp = await api.post<ChatResponse>(
          "/api/chat",
          { message: text, user_name: "Dashboard", conversation_id: conversationId },
          { signal: controller.signal, schema: ChatResponseSchema },
        );
        if (resp.conversation_id && resp.conversation_id !== conversationId) {
          setConversationId(resp.conversation_id);
        }
        addMessage({
          id: localId(),
          role: "assistant",
          content: resp.response,
          timestamp: resp.timestamp ?? new Date().toISOString(),
          mind_id: resp.mind_id,
          tags: resp.tags,
          model: resp.model,
          tokens_in: resp.tokens_in,
          tokens_out: resp.tokens_out,
          cost_usd: resp.cost_usd,
          latency_ms: resp.latency_ms,
          provider: resp.provider,
        });
      } catch (fallbackErr) {
        if (isAbortError(fallbackErr)) return;
        setError(t("error.loadFailed"));
      }
    } finally {
      setLoading(false);
      setStreamingText("");
      setCogPhase(null);
      inputRef.current?.focus();
    }
  }, [loading, conversationId, addMessage, setLoading, setConversationId, setError, t]);

  const sendMessage = useCallback(() => {
    void sendMessageText(input.trim());
  }, [input, sendMessageText]);

  const retryLastMessage = useCallback(() => {
    if (lastMessage) {
      setError(null);
      void sendMessageText(lastMessage);
    }
  }, [lastMessage, sendMessageText]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    },
    [sendMessage],
  );

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInput(e.target.value);
      const el = e.target;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
    },
    [],
  );

  const handleNewChat = useCallback(() => {
    abortRef.current?.abort();
    clearChat();
    setInput("");
    setLastMessage("");
    setStreamingText("");
    setCogPhase(null);
    inputRef.current?.focus();
  }, [clearChat]);

  const handleSelectConversation = useCallback(
    async (id: string) => {
      try {
        const res = await api.get<{ messages: Array<{ id: string; role: string; content: string; timestamp: string; tags?: string[] }> }>(
          `/api/conversations/${id}`,
        );
        clearChat();
        setConversationId(id);
        for (const msg of res.messages ?? []) {
          addMessage({
            id: msg.id,
            role: msg.role as "user" | "assistant",
            content: msg.content,
            timestamp: msg.timestamp,
            tags: msg.tags,
          });
        }
      } catch {
        // Graceful
      }
      setSidebarOpen(false);
    },
    [clearChat, setConversationId, addMessage],
  );

  return (
    <div className="flex h-full" data-testid="chat-page">
      {/* Conversation Sidebar */}
      {sidebarOpen && (
        <ConversationSidebar
          activeId={conversationId}
          onSelect={(id) => void handleSelectConversation(id)}
          onNew={() => {
            handleNewChat();
            setSidebarOpen(false);
          }}
          className="w-64 shrink-0"
        />
      )}

      <div className="flex flex-1 flex-col">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-[var(--svx-color-border-subtle)] px-6 py-4">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="rounded-[var(--svx-radius-md)] p-1.5 text-[var(--svx-color-text-tertiary)] transition-colors hover:bg-[var(--svx-color-bg-hover)] hover:text-[var(--svx-color-text-secondary)]"
              aria-label="Toggle conversations"
            >
              <PanelLeftIcon className="size-4" />
            </button>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">{t("title")}</h1>
                {mood && (
                  <span className="flex items-center gap-1.5 rounded-full bg-[var(--svx-color-bg-elevated)] px-2 py-0.5 text-[10px] text-[var(--svx-color-text-tertiary)]">
                    <span
                      className="size-1.5 rounded-full"
                      style={{
                        backgroundColor:
                          mood.quadrant === "positive_active" ? "#f59e0b"
                            : mood.quadrant === "positive_passive" ? "#14b8a6"
                            : mood.quadrant === "negative_active" ? "#f87171"
                            : mood.quadrant === "negative_passive" ? "#818cf8"
                            : "#94a3b8",
                      }}
                    />
                    {mood.label}
                  </span>
                )}
              </div>
              <p className="text-sm text-[var(--svx-color-text-secondary)]">{t("subtitle")}</p>
            </div>
          </div>
          {messages.length > 0 && (
            <Button variant="outline" size="sm" onClick={handleNewChat} className="gap-2">
              <PlusIcon className="size-4" />
              {t("newChat")}
            </Button>
          )}
        </div>

      {/* Message Thread */}
      <ErrorBoundary name="section.chat.thread" variant="section">
        <div className="relative flex-1" ref={scrollAreaRef}>
          <ScrollArea className="h-full">
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
                      <div className="shrink-0 pt-1">
                        {msg.role === "user" ? <LetterAvatar name={t("you")} /> : <MindAvatar />}
                      </div>
                      <div className={cn("max-w-[75%] space-y-1", msg.role === "user" ? "items-end" : "items-start")}>
                        {msg.role === "assistant" && msg.tags && msg.tags.length > 0 && (
                          <MessageTags tags={msg.tags} />
                        )}
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
                        <div className="flex items-center gap-2 px-1">
                          <span className="text-[10px] text-[var(--svx-color-text-secondary)]">
                            {formatTimeShort(msg.timestamp)}
                          </span>
                          {msg.role === "assistant" && (
                            <MessageMeta
                              model={msg.model}
                              tokensIn={msg.tokens_in}
                              tokensOut={msg.tokens_out}
                              costUsd={msg.cost_usd}
                              latencyMs={msg.latency_ms}
                              provider={msg.provider}
                            />
                          )}
                        </div>
                      </div>
                    </div>
                  ))}

                  {/* Streaming: cognitive progress + live text */}
                  {loading && (
                    <div className="flex gap-3 px-4 py-2" data-testid="chat-loading">
                      <div className="shrink-0 pt-1">
                        <MindAvatar />
                      </div>
                      <div className="max-w-[75%] space-y-1">
                        {cogPhase && !streamingText && (
                          <CognitiveProgress phase={cogPhase.phase} detail={cogPhase.detail} />
                        )}
                        {streamingText ? (
                          <div className="rounded-2xl rounded-tl-sm bg-[var(--svx-color-bg-elevated)] px-4 py-2.5 text-sm leading-relaxed text-[var(--svx-color-text-primary)]">
                            <StreamingMessage text={streamingText} complete={false} />
                          </div>
                        ) : !cogPhase ? (
                          <div className="flex items-center gap-2 rounded-2xl rounded-tl-sm bg-[var(--svx-color-bg-elevated)] px-4 py-2.5">
                            <Loader2Icon className="size-4 animate-spin text-[var(--svx-color-brand-primary)]" />
                            <span className="text-sm text-[var(--svx-color-text-secondary)]">{t("thinking")}</span>
                          </div>
                        ) : null}
                        {cogPhase && streamingText && (
                          <div className="px-1 text-[10px] text-[var(--svx-color-text-disabled)]">
                            {cogPhase.phase}
                            {cogPhase.detail && ` — ${cogPhase.detail}`}
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  <div ref={bottomRef} />
                </div>
              )}
            </div>
          </ScrollArea>

          {showScrollBtn && (
            <button
              type="button"
              onClick={scrollToBottom}
              className="absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded-full border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-2 shadow-lg transition-opacity hover:bg-[var(--svx-color-bg-elevated)]"
              aria-label="Scroll to bottom"
            >
              <ArrowDownIcon className="size-4 text-[var(--svx-color-text-secondary)]" />
            </button>
          )}
        </div>
      </ErrorBoundary>

      {/* Error Banner with Retry */}
      {error && (
        <div className="mx-auto flex max-w-3xl items-center gap-2 px-6 py-2 text-sm text-red-400">
          <AlertTriangleIcon className="size-4 shrink-0" />
          <span>{error}</span>
          {lastMessage && (
            <Button variant="ghost" size="sm" onClick={retryLastMessage} className="ml-2 text-xs">
              {t("common:actions.retry")}
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => setError(null)} className="ml-auto text-xs">
            {t("common:actions.dismiss")}
          </Button>
        </div>
      )}

      {/* Input Area */}
      <ErrorBoundary name="section.chat.input" variant="section">
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
                "disabled:opacity-50 transition-colors",
              )}
              data-testid="chat-input"
            />
            <Button
              onClick={sendMessage}
              disabled={!input.trim() || loading}
              size="icon"
              className={cn(
                "size-11 shrink-0 rounded-xl",
                "bg-[var(--svx-color-brand-primary)] hover:bg-[var(--svx-color-brand-hover)]",
                "text-[var(--svx-color-text-inverse)] disabled:opacity-50",
              )}
              data-testid="chat-send"
            >
              {loading ? <Loader2Icon className="size-5 animate-spin" /> : <SendIcon className="size-5" />}
            </Button>
          </div>
        </div>
      </ErrorBoundary>
      </div>
    </div>
  );
}
