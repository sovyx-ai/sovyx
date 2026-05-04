/**
 * i18n configuration — i18next + react-i18next
 *
 * FE-00f: Architecture setup. Bundled resources (no lazy loading);
 * locales added via `MISSION-claude-autonomous-batch-2026-05-03` §Phase 3:
 *   en, pt-BR, es
 *
 * Namespaces:
 *   common        — shared UI: nav, buttons, status, errors, time
 *   overview      — overview page: stat cards, health, feed
 *   conversations — conversation list + detail
 *   brain         — brain explorer: graph, categories, detail
 *   logs          — log viewer: filters, levels, table
 *   settings      — settings tabs, forms, about
 *   voice         — voice pipeline page (largest namespace)
 *   plugins, chat, about — page-specific
 *
 * Persistence: ``localStorage["sovyx_locale"]`` holds the operator's
 * choice (NOT auth-token-grade — locale is not a credential). The
 * detection layer in ``i18n-detect.ts`` handles first-visit
 * navigator.language sniffing + the toast-undo UX.
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import common from "@/locales/en/common.json";
import overview from "@/locales/en/overview.json";
import conversations from "@/locales/en/conversations.json";
import brain from "@/locales/en/brain.json";
import logs from "@/locales/en/logs.json";
import settings from "@/locales/en/settings.json";
import voice from "@/locales/en/voice.json";
import about from "@/locales/en/about.json";
import chat from "@/locales/en/chat.json";
import plugins from "@/locales/en/plugins.json";

import commonPtBR from "@/locales/pt-BR/common.json";
import overviewPtBR from "@/locales/pt-BR/overview.json";
import conversationsPtBR from "@/locales/pt-BR/conversations.json";
import brainPtBR from "@/locales/pt-BR/brain.json";
import logsPtBR from "@/locales/pt-BR/logs.json";
import settingsPtBR from "@/locales/pt-BR/settings.json";
import voicePtBR from "@/locales/pt-BR/voice.json";
import aboutPtBR from "@/locales/pt-BR/about.json";
import chatPtBR from "@/locales/pt-BR/chat.json";
import pluginsPtBR from "@/locales/pt-BR/plugins.json";

import commonEs from "@/locales/es/common.json";
import overviewEs from "@/locales/es/overview.json";
import conversationsEs from "@/locales/es/conversations.json";
import brainEs from "@/locales/es/brain.json";
import logsEs from "@/locales/es/logs.json";
import settingsEs from "@/locales/es/settings.json";
import voiceEs from "@/locales/es/voice.json";
import aboutEs from "@/locales/es/about.json";
import chatEs from "@/locales/es/chat.json";
import pluginsEs from "@/locales/es/plugins.json";

/** Supported locales — keep in sync with locale-switcher dropdown + completeness gate. */
export const SUPPORTED_LOCALES = ["en", "pt-BR", "es"] as const;
export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

void i18n.use(initReactI18next).init({
  resources: {
    en: {
      common,
      overview,
      conversations,
      brain,
      logs,
      settings,
      voice,
      about,
      chat,
      plugins,
    },
    "pt-BR": {
      common: commonPtBR,
      overview: overviewPtBR,
      conversations: conversationsPtBR,
      brain: brainPtBR,
      logs: logsPtBR,
      settings: settingsPtBR,
      voice: voicePtBR,
      about: aboutPtBR,
      chat: chatPtBR,
      plugins: pluginsPtBR,
    },
    es: {
      common: commonEs,
      overview: overviewEs,
      conversations: conversationsEs,
      brain: brainEs,
      logs: logsEs,
      settings: settingsEs,
      voice: voiceEs,
      about: aboutEs,
      chat: chatEs,
      plugins: pluginsEs,
    },
  },
  lng: "en",
  fallbackLng: "en",
  defaultNS: "common",
  ns: ["common", "overview", "conversations", "brain", "logs", "settings", "voice", "about", "chat", "plugins"],
  interpolation: {
    escapeValue: false, // React already escapes
  },
  react: {
    useSuspense: false, // Bundled resources — no async loading
  },
});

export default i18n;
