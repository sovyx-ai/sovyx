import type { StateCreator } from "zustand";
import type { Conversation, Message } from "@/types/api";
import type { DashboardState } from "../dashboard";

export interface ConversationsSlice {
  conversations: Conversation[];
  setConversations: (convs: Conversation[]) => void;
  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;
  activeMessages: Message[];
  setActiveMessages: (msgs: Message[]) => void;
}

export const createConversationsSlice: StateCreator<
  DashboardState,
  [],
  [],
  ConversationsSlice
> = (set) => ({
  conversations: [],
  setConversations: (convs) => set({ conversations: convs }),
  activeConversationId: null,
  setActiveConversationId: (id) => set({ activeConversationId: id }),
  activeMessages: [],
  setActiveMessages: (msgs) => set({ activeMessages: msgs }),
});
