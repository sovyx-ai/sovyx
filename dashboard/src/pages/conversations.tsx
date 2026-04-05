import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { SearchIcon, MessageSquareIcon, ArrowLeftIcon } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useDashboardStore } from "@/stores/dashboard";
import { api } from "@/lib/api";
import { formatTimeAgo } from "@/lib/format";
import { LetterAvatar } from "@/components/dashboard/letter-avatar";
import { ChannelBadge } from "@/components/dashboard/channel-badge";
import { ChatThread } from "@/components/dashboard/chat-thread";
import type { Conversation, ConversationsResponse, Message } from "@/types/api";
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

  const activeConv = conversations.find((c) => c.id === activeId);

  return (
    <div className="flex h-[calc(100vh-6rem)] gap-4">
      {/* Left: Conversation List */}
      <Card
        className={cn(
          "flex w-80 shrink-0 flex-col",
          activeId && "hidden md:flex",
        )}
      >
        <CardHeader className="shrink-0 space-y-3 pb-3">
          <CardTitle className="text-sm font-medium">{t("title")}</CardTitle>
          <div className="relative">
            <SearchIcon className="absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder={t("search")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 pl-8 text-xs"
            />
          </div>
        </CardHeader>
        <CardContent className="flex-1 overflow-hidden p-0">
          <ScrollArea className="h-full">
            {filtered.length === 0 && !loading ? (
              <div className="flex flex-col items-center justify-center gap-2 py-12 text-muted-foreground">
                <MessageSquareIcon className="size-6 opacity-50" />
                <p className="text-xs">{t("list.empty")}</p>
              </div>
            ) : (
              <div className="divide-y divide-border/50">
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
        </CardContent>
      </Card>

      {/* Right: Chat Detail */}
      <Card className="flex flex-1 flex-col">
        {activeConv ? (
          <>
            {/* Detail Header */}
            <CardHeader className="shrink-0 border-b border-border/50 pb-3">
              <div className="flex items-center gap-3">
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-8 md:hidden"
                  onClick={() => setActiveId(null)}
                >
                  <ArrowLeftIcon className="size-4" />
                </Button>
                <LetterAvatar name={activeConv.participant || "?"} />
                <div>
                  <CardTitle className="text-sm font-medium">
                    {activeConv.participant || "Unknown"}
                  </CardTitle>
                  <div className="flex items-center gap-2 pt-0.5">
                    <ChannelBadge channel={activeConv.channel} />
                    <span className="text-[10px] text-muted-foreground">
                      {activeConv.message_count} msgs
                    </span>
                  </div>
                </div>
              </div>
            </CardHeader>

            {/* Messages */}
            <CardContent className="flex-1 overflow-hidden p-0">
              <ChatThread
                messages={activeMessages}
                participantName={activeConv.participant || "User"}
                loading={loadingMessages}
              />
            </CardContent>
          </>
        ) : (
          <CardContent className="flex h-full items-center justify-center">
            <div className="text-center text-muted-foreground">
              <MessageSquareIcon className="mx-auto size-10 opacity-30" />
              <p className="mt-3 text-sm">Select a conversation</p>
            </div>
          </CardContent>
        )}
      </Card>
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
        "flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-secondary/50",
        active && "border-l-2 border-primary bg-secondary/30",
      )}
    >
      <LetterAvatar name={c.participant || "?"} className="size-7" />

      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-xs font-medium">
            {c.participant || "Unknown"}
          </span>
          <span className="shrink-0 text-[10px] text-muted-foreground">
            {formatTimeAgo(c.last_message_at)}
          </span>
        </div>
        <div className="flex items-center gap-1.5 pt-0.5">
          <ChannelBadge channel={c.channel} />
          {c.status === "active" && (
            <span className="status-dot-green" title="Active" />
          )}
        </div>
      </div>
    </button>
  );
}
