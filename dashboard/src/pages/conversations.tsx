/**
 * Conversations page — split panel (list + detail).
 *
 * Desktop: list (w-80) + detail side by side.
 * Mobile: list-only → tap navigates to detail → back button returns.
 *
 * Data: GET /api/conversations (list), GET /api/conversations/:id (messages).
 * Detail response: {conversation_id, messages[]} — conversation metadata from list cache.
 *
 * Ref: Architecture §3.2
 */

import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { SearchIcon, MessageSquareIcon, ArrowLeftIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useDashboardStore } from "@/stores/dashboard";
import { api } from "@/lib/api";
import { formatTimeAgo } from "@/lib/format";
import { LetterAvatar } from "@/components/dashboard/letter-avatar";
import { ChannelBadge } from "@/components/dashboard/channel-badge";
import { ChatThread } from "@/components/dashboard/chat-thread";
import { StatusDot } from "@/components/dashboard/status-dot";
import type { Conversation, ConversationsResponse, Message } from "@/types/api";
import { EmptyState } from "@/components/empty-state";
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
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [hasMore, setHasMore] = useState(false);

  // Fetch conversation list
  const fetchConversations = useCallback(
    async (offset = 0) => {
      try {
        setLoading(true);
        const data = await api.get<ConversationsResponse>(
          `/api/conversations?limit=${PAGE_SIZE}&offset=${offset}`,
        );
        if (offset === 0) {
          setConversations(data.conversations);
        } else {
          setConversations([...conversations, ...data.conversations]);
        }
        setHasMore(data.conversations.length === PAGE_SIZE);
      } catch {
        // 401 handled by interceptor
      } finally {
        setLoading(false);
      }
    },
    [conversations, setConversations],
  );

  // Fetch messages when active conversation changes
  // Backend returns {conversation_id, messages[]} — NOT {conversation, messages[]}
  useEffect(() => {
    if (!activeId) {
      setActiveMessages([]);
      return;
    }

    const fetchMessages = async () => {
      setLoadingMessages(true);
      try {
        const data = await api.get<{ conversation_id: string; messages: Message[] }>(
          `/api/conversations/${activeId}`,
        );
        setActiveMessages(data.messages);
      } catch {
        setActiveMessages([]);
      } finally {
        setLoadingMessages(false);
      }
    };

    void fetchMessages();
  }, [activeId, setActiveMessages]);

  // Initial load
  useEffect(() => {
    void fetchConversations(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = search
    ? conversations.filter(
        (c) =>
          c.participant.toLowerCase().includes(search.toLowerCase()) ||
          c.channel.toLowerCase().includes(search.toLowerCase()),
      )
    : conversations;

  // Get conversation metadata from list cache (NOT from detail endpoint)
  const activeConv = conversations.find((c) => c.id === activeId);

  return (
    <div className="flex h-[calc(100vh-6rem)] gap-4">
      {/* ── Left: Conversation List (DASH-12) ── */}
      <div
        className={cn(
          "flex w-full shrink-0 flex-col rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] md:w-80",
          activeId && "hidden md:flex",
        )}
      >
        {/* List header */}
        <div className="shrink-0 space-y-3 p-4 pb-3">
          <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
            {t("title")}
          </h2>
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

        {/* List body */}
        <div className="flex-1 overflow-hidden">
          <ScrollArea className="h-full">
            {filtered.length === 0 && !loading ? (
              <EmptyState
                icon={<MessageSquareIcon className="size-8" />}
                title={t("list.empty")}
                description="Send a message via Telegram to get started."
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

            {hasMore && (
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

      {/* ── Right: Chat Detail (DASH-13 + DASH-14) ── */}
      <div
        className={cn(
          "flex flex-1 flex-col rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]",
          !activeId && "hidden md:flex",
        )}
      >
        {activeConv ? (
          <>
            {/* Detail header */}
            <div className="shrink-0 border-b border-[var(--svx-color-border-subtle)] p-4 pb-3">
              <div className="flex items-center gap-3">
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-8 md:hidden"
                  onClick={() => setActiveId(null)}
                  aria-label="Back to conversations"
                >
                  <ArrowLeftIcon className="size-4" />
                </Button>
                <LetterAvatar name={activeConv.participant || "?"} />
                <div>
                  <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
                    {activeConv.participant || "Unknown"}
                  </h2>
                  <div className="flex items-center gap-2 pt-0.5">
                    <ChannelBadge channel={activeConv.channel} />
                    <span className="text-[10px] text-[var(--svx-color-text-tertiary)]">
                      {activeConv.message_count} msgs
                    </span>
                  </div>
                </div>
              </div>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-hidden">
              <ChatThread
                messages={activeMessages}
                participantName={activeConv.participant || "Owner"}
                loading={loadingMessages}
              />
            </div>

            {/* Send placeholder */}
            <div className="shrink-0 border-t border-[var(--svx-color-border-subtle)] p-3">
              <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-input)] px-3 py-2 text-xs text-[var(--svx-color-text-disabled)]">
                <MessageSquareIcon className="size-3.5" />
                Send from dashboard coming in v1.0
              </div>
            </div>
          </>
        ) : (
          <div className="flex h-full items-center justify-center">
            <EmptyState
              icon={<MessageSquareIcon className="size-10" />}
              title="Select a conversation"
              description="Choose from the list to view messages."
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
      <LetterAvatar name={c.participant || "?"} size={28} />

      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-xs font-medium text-[var(--svx-color-text-primary)]">
            {c.participant || "Unknown"}
          </span>
          <span className="shrink-0 text-[10px] text-[var(--svx-color-text-tertiary)]">
            {formatTimeAgo(c.last_message_at)}
          </span>
        </div>
        <div className="flex items-center gap-1.5 pt-0.5">
          <ChannelBadge channel={c.channel} />
          {c.status === "active" && (
            <StatusDot status="online" size="sm" />
          )}
        </div>
      </div>
    </button>
  );
}
