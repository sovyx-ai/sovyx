"""Tests for sovyx.cli._chat_commands — slash-command parsing + dispatch.

Each test mocks ``DaemonClient.call`` so we never touch a real socket.
The dispatcher returns Rich renderables; we assert on the slash
``SlashResult`` shape (flags + presence of expected text in the
rendered Group/Panel/Table) rather than scraping ANSI output.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from sovyx.cli import _chat_commands
from sovyx.cli._chat_commands import SlashResult, dispatch, known_commands, parse


def _render(renderable: Any) -> str:
    """Render a Rich object to a string for substring-matching."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=120).print(renderable)
    return buf.getvalue()


# ── parse() ──────────────────────────────────────────────────────────


class TestParse:
    def test_returns_none_for_chat_input(self) -> None:
        assert parse("hello there") is None
        assert parse("  hello  ") is None
        assert parse("") is None

    def test_extracts_command_and_argv(self) -> None:
        assert parse("/help") == ("/help", [])
        assert parse("/status now") == ("/status", ["now"])
        assert parse("  /help  ") == ("/help", [])

    def test_command_lowercased(self) -> None:
        cmd, _ = parse("/STATUS") or ("", [])
        assert cmd == "/status"

    def test_argv_preserves_case(self) -> None:
        cmd, argv = parse("/foo BarBaz") or ("", [])
        assert cmd == "/foo"
        assert argv == ["BarBaz"]


# ── dispatch() basics ────────────────────────────────────────────────


class TestDispatchBasics:
    async def test_unknown_command_returns_friendly_error(self) -> None:
        client = AsyncMock()
        result = await dispatch(client, "/bogus", [])
        assert isinstance(result, SlashResult)
        assert not result.should_exit
        text = _render(result.rendered)
        assert "Unknown command" in text
        assert "/help" in text
        client.call.assert_not_awaited()

    async def test_known_commands_table_complete(self) -> None:
        cmds = known_commands()
        # MVP commands plus their aliases.
        for required in (
            "/help",
            "/?",
            "/exit",
            "/quit",
            "/new",
            "/clear",
            "/status",
            "/minds",
            "/config",
        ):
            assert required in cmds


# ── /help ────────────────────────────────────────────────────────────


class TestHelp:
    async def test_help_lists_every_command(self) -> None:
        client = AsyncMock()
        result = await dispatch(client, "/help", [])
        text = _render(result.rendered)
        for cmd in ("/help", "/status", "/minds", "/config", "/new", "/clear", "/exit"):
            assert cmd in text
        client.call.assert_not_awaited()

    async def test_help_alias_question_mark(self) -> None:
        client = AsyncMock()
        result = await dispatch(client, "/?", [])
        assert "REPL commands" in _render(result.rendered)


# ── /exit, /quit ─────────────────────────────────────────────────────


class TestExit:
    @pytest.mark.parametrize("cmd", ["/exit", "/quit"])
    async def test_exit_flags_should_exit(self, cmd: str) -> None:
        client = AsyncMock()
        result = await dispatch(client, cmd, [])
        assert result.should_exit
        assert not result.new_conversation
        client.call.assert_not_awaited()


# ── /new ─────────────────────────────────────────────────────────────


class TestNew:
    async def test_new_flags_new_conversation(self) -> None:
        client = AsyncMock()
        result = await dispatch(client, "/new", [])
        assert result.new_conversation
        assert not result.should_exit
        client.call.assert_not_awaited()


# ── /clear ───────────────────────────────────────────────────────────


class TestClear:
    async def test_clear_flags_clear_and_new_conversation(self) -> None:
        """``/clear`` wipes screen *and* rotates conversation_id."""
        client = AsyncMock()
        result = await dispatch(client, "/clear", [])
        assert result.clear_screen
        assert result.new_conversation
        assert not result.should_exit


# ── /status ──────────────────────────────────────────────────────────


class TestStatus:
    async def test_renders_status_dict_into_table(self) -> None:
        client = AsyncMock()
        client.call = AsyncMock(return_value={"version": "0.11.6", "status": "running"})
        result = await dispatch(client, "/status", [])

        client.call.assert_awaited_once_with("status")
        text = _render(result.rendered)
        assert "version" in text
        assert "0.11.6" in text
        assert "running" in text

    async def test_handles_non_dict_response(self) -> None:
        client = AsyncMock()
        client.call = AsyncMock(return_value="just a string")
        result = await dispatch(client, "/status", [])
        # Non-dict shape collapses into an empty table; we still get a panel.
        assert "no fields returned" in _render(result.rendered)


# ── /minds ───────────────────────────────────────────────────────────


class TestMinds:
    async def test_lists_minds_and_marks_active(self) -> None:
        client = AsyncMock()
        client.call = AsyncMock(return_value={"minds": ["aria", "luna"], "active": "aria"})
        result = await dispatch(client, "/minds", [])

        client.call.assert_awaited_once_with("mind.list")
        text = _render(result.rendered)
        assert "aria" in text
        assert "luna" in text
        # Active marker appears at least once.
        assert "●" in text

    async def test_empty_minds_returns_warning(self) -> None:
        client = AsyncMock()
        client.call = AsyncMock(return_value={"minds": [], "active": None})
        result = await dispatch(client, "/minds", [])
        text = _render(result.rendered)
        assert "No active minds" in text


# ── /config ──────────────────────────────────────────────────────────


class TestConfig:
    async def test_renders_three_sections(self) -> None:
        client = AsyncMock()
        client.call = AsyncMock(
            return_value={
                "available": True,
                "mind_id": "aria",
                "name": "Aria",
                "language": "en",
                "timezone": "UTC",
                "template": "assistant",
                "llm": {
                    "default_provider": "anthropic",
                    "default_model": "claude-sonnet-4-6",
                    "fast_model": "claude-haiku-4-5-20251001",
                    "temperature": 0.7,
                    "budget_daily_usd": 5.0,
                },
                "brain": {
                    "consolidation_interval_hours": 6,
                    "dream_time": "02:00",
                    "dream_lookback_hours": 24,
                    "dream_max_patterns": 5,
                    "max_concepts": 50000,
                    "forgetting_enabled": True,
                    "decay_rate": 0.1,
                },
            }
        )
        result = await dispatch(client, "/config", [])
        text = _render(result.rendered)
        for needle in (
            "Mind",
            "LLM",
            "Brain",
            "Aria",
            "anthropic",
            "claude-sonnet-4-6",
            "dream_time",
            "02:00",
        ):
            assert needle in text

    async def test_config_unavailable_returns_warning(self) -> None:
        client = AsyncMock()
        client.call = AsyncMock(return_value={"available": False, "mind_id": None})
        result = await dispatch(client, "/config", [])
        text = _render(result.rendered)
        assert "not available" in text


# ── Smoke: every handler returns a SlashResult ───────────────────────


class TestHandlerContract:
    @pytest.mark.parametrize(
        "command,response",
        [
            ("/help", None),
            ("/exit", None),
            ("/new", None),
            ("/clear", None),
            ("/status", {}),
            ("/minds", {"minds": []}),
            ("/config", {"available": False}),
        ],
    )
    async def test_returns_slash_result(self, command: str, response: Any) -> None:
        client = AsyncMock()
        client.call = AsyncMock(return_value=response)
        result = await _chat_commands.dispatch(client, command, [])
        assert isinstance(result, SlashResult)
