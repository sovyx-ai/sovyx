/**
 * Chat slice — state for the /chat direct conversation page.
 *
 * Manages message thread, loading state, error, and conversation continuity.
 *
 * Ref: DASH-03, DASH-04
 */
import type { StateCreator } from "zustand";
import type { ChatMessage } from "@/types/api";
import type { DashboardState } from "../dashboard";

export interface ChatSlice {
  chatMessages: ChatMessage[];
  chatLoading: boolean;
  chatConversationId: string | null;
  chatError: string | null;
  addChatMessage: (msg: ChatMessage) => void;
  setChatLoading: (loading: boolean) => void;
  setChatConversationId: (id: string | null) => void;
  setChatError: (error: string | null) => void;
  clearChat: () => void;
}

export const createChatSlice: StateCreator<
  DashboardState,
  [],
  [],
  ChatSlice
> = (set) => ({
  chatMessages: [],
  chatLoading: false,
  chatConversationId: null,
  chatError: null,
  addChatMessage: (msg) =>
    set((state) => ({ chatMessages: [...state.chatMessages, msg] })),
  setChatLoading: (loading) => set({ chatLoading: loading }),
  setChatConversationId: (id) => set({ chatConversationId: id }),
  setChatError: (error) => set({ chatError: error }),
  clearChat: () =>
    set({
      chatMessages: [],
      chatConversationId: null,
      chatLoading: false,
      chatError: null,
    }),
});
