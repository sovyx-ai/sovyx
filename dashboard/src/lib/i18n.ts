/**
 * i18n configuration — i18next + react-i18next
 *
 * FE-00f: Architecture setup. English only for v0.5.
 * All locale JSONs are bundled (no lazy loading yet).
 *
 * Namespaces:
 *   common        — shared UI: nav, buttons, status, errors, time
 *   overview      — overview page: stat cards, health, feed
 *   conversations — conversation list + detail
 *   brain         — brain explorer: graph, categories, detail
 *   logs          — log viewer: filters, levels, table
 *   settings      — settings tabs, forms, about
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
import emotions from "@/locales/en/emotions.json";
import productivity from "@/locales/en/productivity.json";
import plugins from "@/locales/en/plugins.json";
import home from "@/locales/en/home.json";
import about from "@/locales/en/about.json";
import chat from "@/locales/en/chat.json";

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
      emotions,
      productivity,
      plugins,
      home,
      about,
      chat,
    },
  },
  lng: "en",
  fallbackLng: "en",
  defaultNS: "common",
  ns: ["common", "overview", "conversations", "brain", "logs", "settings", "voice", "emotions", "productivity", "plugins", "home", "about", "chat"],
  interpolation: {
    escapeValue: false, // React already escapes
  },
  react: {
    useSuspense: false, // Bundled resources — no async loading
  },
});

export default i18n;
