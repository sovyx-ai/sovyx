"""T03 mission test — extract_signals.has_tool_use (Mission pre-wake-word T03).

Before T03, ``ComplexitySignals.has_tool_use`` was always ``False`` because
``extract_signals`` never set it. This caused the complexity-tier router to
miss the "tool-use mode" signal, potentially routing tool-using conversations
to providers that lack native tool support.

T03 fix: slide a 5-message window over message history, set
``has_tool_use=True`` if ANY of those messages carries:

* ``role == "tool"`` (a tool result message), OR
* a non-empty ``tool_calls`` list (assistant calling tools in this turn).

These tests pin the contract from D5 of the mission spec.
"""

from __future__ import annotations

from sovyx.llm.router import _TOOL_USE_WINDOW, extract_signals


def _user(content: str) -> dict[str, object]:
    return {"role": "user", "content": content}


def _assistant(content: str = "") -> dict[str, object]:
    return {"role": "assistant", "content": content}


def _assistant_with_tool_call(content: str = "") -> dict[str, object]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{}"},
            },
        ],
    }


def _tool_result(call_id: str = "call_1", content: str = "Sunny, 22°C") -> dict[str, object]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class TestHasToolUseFalseWhenNoTools:
    """has_tool_use stays False on plain user/assistant chats."""

    def test_empty_messages(self) -> None:
        signals = extract_signals([])
        assert signals.has_tool_use is False

    def test_single_user_message(self) -> None:
        signals = extract_signals([_user("hello")])
        assert signals.has_tool_use is False

    def test_user_assistant_chat_no_tools(self) -> None:
        signals = extract_signals(
            [
                _user("hi"),
                _assistant("hello back"),
                _user("how are you"),
                _assistant("fine"),
            ]
        )
        assert signals.has_tool_use is False


class TestHasToolUseTrueOnContinuation:
    """Path 1 — current/recent message carries tool_call_id metadata
    (continuation of an active ReAct turn)."""

    def test_tool_result_in_history(self) -> None:
        """A tool result message in the history → has_tool_use=True."""
        signals = extract_signals(
            [
                _user("what's the weather"),
                _assistant_with_tool_call(),
                _tool_result(),
            ]
        )
        assert signals.has_tool_use is True

    def test_tool_result_at_end(self) -> None:
        """The most recent message is a tool result — clearly tool-use mode."""
        signals = extract_signals(
            [
                _user("calculate 5+3"),
                _assistant_with_tool_call(),
                _tool_result(call_id="call_1", content="8"),
            ]
        )
        assert signals.has_tool_use is True


class TestHasToolUseTrueOnRecentToolCall:
    """Path 2 — recent assistant message had non-empty tool_calls."""

    def test_assistant_tool_call_in_history(self) -> None:
        signals = extract_signals(
            [
                _user("get weather"),
                _assistant_with_tool_call(),
                _tool_result(),
                _assistant("It's sunny."),
                _user("thanks"),
            ]
        )
        assert signals.has_tool_use is True

    def test_assistant_with_empty_tool_calls_list_is_not_tool_use(self) -> None:
        """Edge case: tool_calls=[] is falsy; should NOT trigger has_tool_use."""
        signals = extract_signals(
            [
                _user("hi"),
                {"role": "assistant", "content": "hello", "tool_calls": []},
            ]
        )
        assert signals.has_tool_use is False


class TestSlidingWindow:
    """The window is the last 5 messages; older tool activity falls off."""

    def test_old_tool_activity_outside_window_does_not_trigger(self) -> None:
        """If tool activity is OLDER than the window, has_tool_use=False."""
        # Build: [tool_call, tool_result] OUTSIDE the trailing 5-window
        # by padding with 6 plain user/assistant turns.
        messages = [
            _user("get weather"),  # 0
            _assistant_with_tool_call(),  # 1
            _tool_result(),  # 2
            _assistant("It's sunny."),  # 3
            _user("topic switch"),  # 4
            _assistant("ok"),  # 5
            _user("more"),  # 6
            _assistant("more"),  # 7
            _user("more2"),  # 8
        ]
        # _TOOL_USE_WINDOW = 5 → the trailing 5 are messages [4..8],
        # none of which are tool messages.
        signals = extract_signals(messages)
        assert signals.has_tool_use is False
        assert _TOOL_USE_WINDOW == 5  # contract pin

    def test_topic_switch_after_old_tool_use(self) -> None:
        """Same as above but spelled out: old ReAct cycle followed by
        enough non-tool turns to push it out of the window → False."""
        old_react = [
            _user("calc 2+2"),
            _assistant_with_tool_call(),
            _tool_result(content="4"),
            _assistant("4"),
        ]
        topic_switch = [_user(f"unrelated turn {i}") for i in range(10)]
        signals = extract_signals(old_react + topic_switch)
        assert signals.has_tool_use is False

    def test_recent_tool_use_within_window(self) -> None:
        """Tool use INSIDE the trailing 5-window → True."""
        messages = [
            _user("hi"),
            _assistant("hello"),
            _user("get weather"),  # 2
            _assistant_with_tool_call(),  # 3
            _tool_result(),  # 4
            _assistant("It's sunny."),  # 5 ← window covers indices 1..5
        ]
        signals = extract_signals(messages)
        assert signals.has_tool_use is True


class TestSignalsCompositionWithExistingFields:
    """Verify other ComplexitySignals fields still extract correctly after
    the T03 has_tool_use addition."""

    def test_message_length_with_tool_messages(self) -> None:
        msgs = [_user("hi"), _tool_result(content="some result text")]
        signals = extract_signals(msgs)
        assert signals.message_length == len("hi") + len("some result text")

    def test_turn_count_only_user_and_assistant(self) -> None:
        msgs = [
            _user("hi"),
            _assistant("hi back"),
            _tool_result(),
        ]
        signals = extract_signals(msgs)
        # turn_count counts user + assistant; tool messages don't count
        assert signals.turn_count == 2

    def test_non_string_content_handled_defensively(self) -> None:
        """Anthropic with tool-use sometimes carries content as a list of
        blocks instead of str. Should not crash extract_signals."""
        msg: dict[str, object] = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
        signals = extract_signals([msg])
        # Treats non-string content as empty for length/code purposes
        assert signals.message_length == 0
        assert signals.has_code is False
