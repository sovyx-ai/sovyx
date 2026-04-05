import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { SearchIcon, MessageSquareIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useDashboardStore } from "@/stores/dashboard";
import { api } from "@/lib/api";
import { formatTimeAgo } from "@/lib/format";
import { LetterAvatar } from "@/components/dashboard/letter-avatar";
import { ChannelBadge } from "@/components/dashboard/channel-badge";
import type { Conversation, ConversationsResponse } from "@/types/api";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;

export default function ConversationsPage() {
  const { t } = useTranslation(["conversations", "common"]);
  const conversations = useDashboardStore((s) => s.conversations);
  const setConversations = useDashboardStore((s) => s.setConversations);
  const activeId = useDashboardStore((s) => s.activeConversationId);
  const setActiveId = useDashboardStore((s) => s.setActiveConversationId);

  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);

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

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold">{t("title")}</h1>
      </div>

      {/* Search */}
      <div className="relative">
        <SearchIcon className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder={t("search")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="pl-9"
        />
      </div>

      {/* Conversation List */}
      <Card>
        <CardContent className="p-0">
          <ScrollArea className="h-[calc(100vh-16rem)]">
            {filtered.length === 0 && !loading ? (
              <div className="flex flex-col items-center justify-center gap-2 py-16 text-muted-foreground">
                <MessageSquareIcon className="size-8 opacity-50" />
                <p className="text-sm">{t("list.empty")}</p>
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
              <div className="p-4 text-center">
                <Button
                  variant="ghost"
                  size="sm"
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
        "flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-secondary/50",
        active && "border-l-2 border-primary bg-secondary/30",
      )}
    >
      <LetterAvatar name={c.participant || "?"} />

      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-medium">
            {c.participant || "Unknown"}
          </span>
          <span className="shrink-0 text-[10px] text-muted-foreground">
            {formatTimeAgo(c.last_message_at)}
          </span>
        </div>
        <div className="flex items-center gap-2 pt-0.5">
          <ChannelBadge channel={c.channel} />
          <span className="text-[10px] text-muted-foreground">
            {c.message_count} msgs
          </span>
          {c.status === "active" && (
            <span className="status-dot-green" title="Active" />
          )}
        </div>
      </div>
    </button>
  );
}
