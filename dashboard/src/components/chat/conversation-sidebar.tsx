import { memo, useCallback, useEffect, useState } from "react";
import { PlusIcon, SearchIcon, MessageSquareIcon } from "lucide-react";
import { api } from "@/lib/api";
import { formatTimeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Conversation } from "@/types/api";

interface ConversationSidebarProps {
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  className?: string;
}

function ConversationSidebarImpl({
  activeId,
  onSelect,
  onNew,
  className,
}: ConversationSidebarProps) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  const fetchConversations = useCallback(async () => {
    try {
      const res = await api.get<{ conversations: Conversation[] }>(
        "/api/conversations?limit=50",
      );
      setConversations(res.conversations);
    } catch {
      // Graceful
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchConversations();
    const interval = setInterval(() => void fetchConversations(), 15_000);
    return () => clearInterval(interval);
  }, [fetchConversations]);

  const filtered = search
    ? conversations.filter(
        (c) =>
          (c.participant_name ?? "").toLowerCase().includes(search.toLowerCase()) ||
          c.channel.toLowerCase().includes(search.toLowerCase()),
      )
    : conversations;

  return (
    <div
      className={cn(
        "flex h-full flex-col border-r border-[var(--svx-color-border-subtle)] bg-[var(--svx-color-bg-surface)]",
        className,
      )}
    >
      {/* Header */}
      <div className="space-y-2 border-b border-[var(--svx-color-border-subtle)] p-3">
        <button
          type="button"
          onClick={onNew}
          className="flex w-full items-center gap-2 rounded-[var(--svx-radius-md)] border border-dashed border-[var(--svx-color-border-default)] px-3 py-2 text-xs text-[var(--svx-color-text-secondary)] transition-colors hover:border-[var(--svx-color-brand-primary)]/40 hover:text-[var(--svx-color-brand-primary)]"
        >
          <PlusIcon className="size-3.5" />
          New conversation
        </button>
        <div className="relative">
          <SearchIcon className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--svx-color-text-disabled)]" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search..."
            className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] py-1.5 pl-8 pr-3 text-xs text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)]"
          />
        </div>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="space-y-2 p-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-12 animate-pulse rounded-[var(--svx-radius-md)] bg-[var(--svx-color-bg-elevated)]" />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-6 text-center">
            <MessageSquareIcon className="size-6 text-[var(--svx-color-text-disabled)]" />
            <p className="text-xs text-[var(--svx-color-text-tertiary)]">
              {search ? "No matches" : "No conversations yet"}
            </p>
          </div>
        ) : (
          <div className="space-y-0.5 p-1.5">
            {filtered.map((conv) => (
              <button
                key={conv.id}
                type="button"
                onClick={() => onSelect(conv.id)}
                className={cn(
                  "flex w-full items-start gap-2.5 rounded-[var(--svx-radius-md)] px-3 py-2.5 text-left transition-colors",
                  activeId === conv.id
                    ? "bg-[var(--svx-color-brand-primary)]/10 text-[var(--svx-color-brand-primary)]"
                    : "hover:bg-[var(--svx-color-bg-hover)] text-[var(--svx-color-text-primary)]",
                )}
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium">
                    {conv.participant_name || conv.channel}
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-[10px] text-[var(--svx-color-text-tertiary)]">
                    <span>{conv.message_count} msgs</span>
                    <span>{formatTimeAgo(conv.last_message_at)}</span>
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Footer */}
      {conversations.length > 0 && (
        <div className="border-t border-[var(--svx-color-border-subtle)] px-3 py-2 text-[10px] text-[var(--svx-color-text-disabled)]">
          {conversations.length} conversations
        </div>
      )}
    </div>
  );
}

export const ConversationSidebar = memo(ConversationSidebarImpl);
