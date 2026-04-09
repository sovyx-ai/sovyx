/**
 * Conversations page — split panel (list + detail).
 *
 * POLISH-01: AbortController on both list and detail fetches.
 * POLISH-02: Error states with retry.
 * POLISH-04: Graceful fallback when list cache is empty.
 *
 * Ref: Architecture §3.2
 */

import { useEffect, useState, useCallback, useRef } from "react";
import { useTranslation } from "react-i18next";
import { SearchIcon, MessageSquareIcon, ArrowLeftIcon, AlertTriangleIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useDashboardStore } from "@/stores/dashboard";
import { api, isAbortError } from "@/lib/api";
import { formatTimeAgo } from "@/lib/format";
import { LetterAvatar } from "@/components/dashboard/letter-avatar";
import { ChannelBadge } from "@/components/dashboard/channel-badge";
import { ChatThread } from "@/components/dashboard/chat-thread";
import { StatusDot } from "@/components/dashboard/status-dot";
import type { Conversation, ConversationsResponse, Message } from "@/types/api";
import { EmptyState } from "@/components/empty-state";
import { ConversationSelectAnimation } from "@/components/empty-state-animations";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;

export default function ConversationsPage() {
  const { t } = useTranslation(["conversations", "common"]);
  const conversations = useDashboardStore((s) => s.conversations);
  const setConversations = useDashboardStore((s) => s.setConversations);
  const activeId = useDashboardStore((s) => s.activeConversationId);
  const setActiveId = useDashboardStore((s) => s.setActiveConversationId);
  const activeMessages = useDashboardStore((s) => s.activeMessages);
  const setActiveMessages = useDashboardStore((s) => s.setActiveMessages);

  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [messageError, setMessageError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);

  // Abort ref for list fetches
  const listAbortRef = useRef<AbortController | null>(null);

  // Fetch conversation list with AbortController
  const fetchConversations = useCallback(
    async (offset = 0, signal?: AbortSignal) => {
      try {
        setLoading(true);
        setError(null);
        const data = await api.get<ConversationsResponse>(
          `/api/conversations?limit=${PAGE_SIZE}&offset=${offset}`,
          { signal },
        );
        if (offset === 0) {
          setConversations(data.conversations);
        } else {
          setConversations([...conversations, ...data.conversations]);
        }
        setHasMore(data.conversations.length === PAGE_SIZE);
      } catch (err) {
        if (isAbortError(err)) return;
        setError(t("error.loadFailed"));
      } finally {
        setLoading(false);
      }
    },
    [conversations, setConversations],
  );

  // Fetch messages when active conversation changes
  useEffect(() => {
    if (!activeId) {
      setActiveMessages([]);
      setMessageError(null);
      return;
    }

    const controller = new AbortController();
    const fetchMessages = async () => {
      setLoadingMessages(true);
      setMessageError(null);
      try {
        const data = await api.get<{ conversation_id: string; messages: Message[] }>(
          `/api/conversations/${activeId}`,
          { signal: controller.signal },
        );
        setActiveMessages(data.messages);
      } catch (err) {
        if (isAbortError(err)) return;
        setMessageError(t("error.messagesFailed"));
        setActiveMessages([]);
      } finally {
        setLoadingMessages(false);
      }
    };

    void fetchMessages();
    return () => controller.abort();
  }, [activeId, setActiveMessages]);

  // Initial load with AbortController — inline to avoid exhaustive-deps on fetchConversations
  useEffect(() => {
    const controller = new AbortController();
    listAbortRef.current = controller;

    (async () => {
      try {
        setLoading(true);
        setError(null);
        const data = await api.get<ConversationsResponse>(
          `/api/conversations?limit=${PAGE_SIZE}&offset=0`,
          { signal: controller.signal },
        );
        setConversations(data.conversations);
        setHasMore(data.conversations.length === PAGE_SIZE);
      } catch (err) {
        if (isAbortError(err)) return;
        setError(t("error.loadFailed"));
      } finally {
        setLoading(false);
      }
    })();

    return () => controller.abort();
  }, [setConversations, t]);

  const filtered = search
    ? conversations.filter(
        (c) =>
          (c.participant_name ?? c.participant).toLowerCase().includes(search.toLowerCase()) ||
          c.channel.toLowerCase().includes(search.toLowerCase()),
      )
    : conversations;

  // Get conversation metadata from list cache, with fallback (POLISH-04)
  const activeConv = conversations.find((c) => c.id === activeId);
  const activeLabel = activeConv?.participant_name || activeConv?.participant?.slice(0, 8) || t("unknownParticipant");
  const activeChannel = activeConv?.channel || "unknown";

  return (
    <div className="flex h-[calc(100vh-6rem)] gap-4">
      {/* ── Left: Conversation List ── */}
      <div
        className={cn(
          "flex w-full shrink-0 flex-col rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] md:w-80",
          activeId && "hidden md:flex",
        )}
      >
        <div className="shrink-0 space-y-3 p-4 pb-3">
          <h1 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
            {t("title")}
          </h1>
          <div className="relative">
            <SearchIcon className="absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-[var(--svx-color-text-tertiary)]" />
            <Input
              placeholder={t("search")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 pl-8 text-xs"
            />
          </div>
        </div>

        <div className="flex-1 overflow-hidden">
          <ScrollArea className="h-full">
            {error ? (
              <EmptyState
                icon={<AlertTriangleIcon className="size-8" />}
                title={error}
                action={{ label: t("common:actions.retry"), onClick: () => void fetchConversations(0) }}
                className="py-12"
              />
            ) : filtered.length === 0 && !loading ? (
              <EmptyState
                icon={<MessageSquareIcon className="size-8" />}
                title={t("list.empty")}
                description={t("list.emptyHint")}
                className="py-12"
              />
            ) : (
              <div className="divide-y divide-[var(--svx-color-border-subtle)]">
                {filtered.map((conv) => (
                  <ConversationRow
                    key={conv.id}
                    conversation={conv}
                    active={conv.id === activeId}
                    onClick={() => setActiveId(conv.id)}
                  />
                ))}
              </div>
            )}

            {hasMore && !error && (
              <div className="p-3 text-center">
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-xs"
                  onClick={() => void fetchConversations(conversations.length)}
                  disabled={loading}
                >
                  {t("common:actions.loadMore")}
                </Button>
              </div>
            )}
          </ScrollArea>
        </div>
      </div>

      {/* ── Right: Chat Detail ── */}
      <div
        className={cn(
          "flex flex-1 flex-col rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]",
          !activeId && "hidden md:flex",
        )}
      >
        {activeId ? (
          <>
            <div className="shrink-0 border-b border-[var(--svx-color-border-subtle)] p-4 pb-3">
              <div className="flex items-center gap-3">
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-8 md:hidden"
                  onClick={() => setActiveId(null)}
                  aria-label={t("detail.backLabel")}
                >
                  <ArrowLeftIcon className="size-4" />
                </Button>
                <LetterAvatar name={activeLabel} />
                <div>
                  <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
                    {activeLabel}
                  </h2>
                  <div className="flex items-center gap-2 pt-0.5">
                    <ChannelBadge channel={activeChannel} />
                    {activeConv && (
                      <span className="text-[10px] text-[var(--svx-color-text-tertiary)]">
                        {t("detail.messageCount", { count: activeConv.message_count })}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </div>

            <div className="flex-1 overflow-hidden">
              {messageError ? (
                <EmptyState
                  icon={<AlertTriangleIcon className="size-8" />}
                  title={messageError}
                  action={{ label: t("common:actions.retry"), onClick: () => setActiveId(activeId) }}
                  className="h-full"
                />
              ) : (
                <ChatThread
                  messages={activeMessages}
                  participantName={activeLabel}
                  loading={loadingMessages}
                />
              )}
            </div>

            <div className="shrink-0 border-t border-[var(--svx-color-border-subtle)] p-3">
              <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-input)] px-3 py-2 text-xs text-[var(--svx-color-text-disabled)]">
                <MessageSquareIcon className="size-3.5" />
                {t("detail.sendPlaceholder")}
              </div>
            </div>
          </>
        ) : (
          <div className="flex h-full items-center justify-center">
            <EmptyState
              icon={<MessageSquareIcon className="size-10" />}
              animation={<ConversationSelectAnimation />}
              title={t("detail.selectTitle")}
              description={t("detail.selectHint")}
            />
          </div>
        )}
      </div>
    </div>
  );
}

// ── Conversation Row ──

interface ConversationRowProps {
  conversation: Conversation;
  active: boolean;
  onClick: () => void;
}

function ConversationRow({ conversation, active, onClick }: ConversationRowProps) {
  const { t } = useTranslation("conversations");
  const c = conversation;
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors",
        "hover:bg-[var(--svx-color-bg-hover)]",
        active && "border-l-2 border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-bg-active)]",
      )}
    >
      <LetterAvatar name={c.participant_name || c.participant || "?"} size={28} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-xs font-medium text-[var(--svx-color-text-primary)]">
            {c.participant_name || c.participant?.slice(0, 8) || t("unknownParticipant")}
          </span>
          <span className="shrink-0 text-[10px] text-[var(--svx-color-text-tertiary)]">
            {formatTimeAgo(c.last_message_at)}
          </span>
        </div>
        <div className="flex items-center gap-1.5 pt-0.5">
          <ChannelBadge channel={c.channel} />
          {c.status === "active" && <StatusDot status="online" size="sm" />}
        </div>
      </div>
    </button>
  );
}
