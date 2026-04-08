/**
 * Global dashboard store — zustand v5 slices pattern
 *
 * FE-00c: Refactored from monolithic to 6 slices:
 *   connection, status, conversations, brain, logs, settings
 *
 * Each slice is in stores/slices/ for separation of concerns.
 * Combined into one bounded store with devtools middleware.
 */
import { create } from "zustand";
import { devtools } from "zustand/middleware";

import { createActivitySlice, type ActivitySlice } from "./slices/activity";
import { createAuthSlice, type AuthSlice } from "./slices/auth";
import {
  createConnectionSlice,
  type ConnectionSlice,
} from "./slices/connection";
import { createStatusSlice, type StatusSlice } from "./slices/status";
import {
  createConversationsSlice,
  type ConversationsSlice,
} from "./slices/conversations";
import { createBrainSlice, type BrainSlice } from "./slices/brain";
import { createLogsSlice, type LogsSlice } from "./slices/logs";
import { createSettingsSlice, type SettingsSlice } from "./slices/settings";
import { createChatSlice, type ChatSlice } from "./slices/chat";

export type DashboardState = ActivitySlice &
  AuthSlice &
  ConnectionSlice &
  StatusSlice &
  ConversationsSlice &
  BrainSlice &
  LogsSlice &
  SettingsSlice &
  ChatSlice;

export const useDashboardStore = create<DashboardState>()(
  devtools(
    (...a) => ({
      ...createActivitySlice(...a),
      ...createAuthSlice(...a),
      ...createConnectionSlice(...a),
      ...createStatusSlice(...a),
      ...createConversationsSlice(...a),
      ...createBrainSlice(...a),
      ...createLogsSlice(...a),
      ...createSettingsSlice(...a),
      ...createChatSlice(...a),
    }),
    { name: "sovyx-dashboard" },
  ),
);
