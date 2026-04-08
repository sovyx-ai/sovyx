# Dashboard Smoke Test Checklist

> Manual verification for Sovyx Dashboard v0.5 — Dashboard Synergy Release

## Pre-requisites
- [ ] Python 3.12+
- [ ] Node.js 20+
- [ ] LLM API key (OpenAI, Anthropic, or compatible)

## Installation & Setup

- [ ] `pip install sovyx` installs cleanly
- [ ] `sovyx --version` shows `0.5.x`
- [ ] `sovyx init` creates `~/.sovyx/` directory
- [ ] `sovyx init` creates `system.yaml` and mind config

## Startup

- [ ] `sovyx start -f` launches the daemon
- [ ] Startup banner appears with:
  - [ ] Dashboard URL (`http://127.0.0.1:7777`)
  - [ ] Auth token (or "not generated" note)
  - [ ] `sovyx token` reference
- [ ] `sovyx token` displays the token
- [ ] `sovyx token --copy` copies to clipboard (if xclip/pbcopy available)

## Dashboard — Auth

- [ ] Browser opens `http://127.0.0.1:7777`
- [ ] Token entry modal appears
- [ ] Modal shows `sovyx token` as the command to get token
- [ ] Placeholder text: "Paste token here…"
- [ ] Invalid token shows error message
- [ ] Valid token → "Connected" feedback → modal closes
- [ ] Redirect to Overview page

## Dashboard — Overview

- [ ] Overview page loads with stat cards
- [ ] **Fresh engine**: Welcome banner appears with 3 steps
  - [ ] Step 1: "Configure your LLM key" → links to Settings
  - [ ] Step 2: "Send your first message" → links to Chat
  - [ ] Step 3: "Watch your mind grow"
- [ ] Channel status card shows:
  - [ ] Dashboard: ✅ Connected
  - [ ] Telegram: status correct (connected/not connected)
  - [ ] Signal: status correct (connected/not connected)
- [ ] Health grid loads
- [ ] Activity feed loads (empty for fresh engine)

## Dashboard — Chat

- [ ] `/chat` page loads from sidebar navigation
- [ ] Chat icon visible in sidebar (Core section)
- [ ] Empty state: "Start a conversation" message
- [ ] Input field with placeholder "Type a message..."
- [ ] Send button disabled when input is empty
- [ ] Type message → send button enables
- [ ] Click send → user message appears immediately (optimistic)
- [ ] Loading indicator: "Thinking..." with spinner
- [ ] AI response appears after processing
- [ ] Auto-scroll to latest message
- [ ] Enter key sends message
- [ ] Shift+Enter creates newline (no send)
- [ ] "New Chat" button appears after first message
- [ ] Click "New Chat" → clears messages, shows empty state
- [ ] Conversation continuity: second message uses same conversation_id

## Dashboard — Other Pages

- [ ] Conversations page loads
- [ ] Brain page loads
- [ ] Logs page loads
- [ ] Settings page loads
- [ ] About page loads
- [ ] 404 page shows for invalid routes

## CLI Commands

- [ ] `sovyx status` shows daemon status
- [ ] `sovyx dashboard` shows URL info
- [ ] `sovyx dashboard --token` reveals token
- [ ] `sovyx doctor` runs health checks
- [ ] `sovyx stop` stops the daemon

## Edge Cases

- [ ] Refresh page → re-authenticates (or cached token)
- [ ] Rapid messages → no state corruption
- [ ] Very long message → sends and displays correctly
- [ ] Unicode/emoji → renders correctly
- [ ] Network disconnect → error message shown
- [ ] Engine restart → dashboard reconnects

---

**Tested by:** _______________
**Date:** _______________
**Version:** 0.5.0
**Result:** ☐ PASS / ☐ FAIL
**Notes:**
