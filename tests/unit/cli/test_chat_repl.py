"""Tests for sovyx.cli.chat — REPL loop, slash + chat handlers.

The async loop is exercised with a fake ``session`` object whose
``prompt_async`` yields a queue of pre-recorded inputs. ``DaemonClient``
is mocked so no socket touches the filesystem. Together this drives
the loop deterministically without prompt_toolkit or asyncio
gymnastics.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from sovyx.cli import chat as repl
from sovyx.cli.chat import _handle_chat, _handle_slash, _loop, _ReplState, run_repl
from sovyx.engine.errors import ChannelConnectionError

# ── Helpers ──────────────────────────────────────────────────────────


class _FakeSession:
    """Mimics PromptSession.prompt_async — pulls from a fixed queue."""

    def __init__(self, lines: Iterable[str]) -> None:
        self._lines = iter(lines)

    async def prompt_async(self, *_a: Any, **_kw: Any) -> str:
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise EOFError from exc


def _capture_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=120, no_color=True), buf


# ── _handle_slash ────────────────────────────────────────────────────


class TestHandleSlash:
    async def test_help_prints_and_keeps_loop(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        state = _ReplState()
        keep_going = await _handle_slash(cli, client, "/help", [], state)
        assert keep_going is True
        assert "REPL commands" in buf.getvalue()

    async def test_exit_returns_false(self) -> None:
        cli, _ = _capture_console()
        client = AsyncMock()
        state = _ReplState()
        keep_going = await _handle_slash(cli, client, "/exit", [], state)
        assert keep_going is False

    async def test_new_resets_conversation_id(self) -> None:
        cli, _ = _capture_console()
        client = AsyncMock()
        state = _ReplState()
        state.conversation_id = "abc-123"
        await _handle_slash(cli, client, "/new", [], state)
        assert state.conversation_id is None

    async def test_clear_resets_conversation_id_and_clears_screen(self) -> None:
        cli = MagicMock(spec=Console)
        client = AsyncMock()
        state = _ReplState()
        state.conversation_id = "abc-123"
        await _handle_slash(cli, client, "/clear", [], state)
        assert state.conversation_id is None
        cli.clear.assert_called_once()

    async def test_daemon_error_renders_inline_and_continues(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(side_effect=ChannelConnectionError("socket gone"))
        state = _ReplState()
        keep_going = await _handle_slash(cli, client, "/status", [], state)
        assert keep_going is True
        assert "Daemon error" in buf.getvalue()
        assert "socket gone" in buf.getvalue()

    async def test_unexpected_exception_does_not_break_loop(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(side_effect=RuntimeError("boom"))
        state = _ReplState()
        keep_going = await _handle_slash(cli, client, "/status", [], state)
        assert keep_going is True
        assert "/status failed" in buf.getvalue()
        assert "boom" in buf.getvalue()


# ── _handle_chat ─────────────────────────────────────────────────────


class TestHandleChat:
    async def test_sends_message_and_persists_conversation_id(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(
            return_value={
                "response": "Hi there.",
                "conversation_id": "conv-42",
                "mind_id": "aria",
                "tags": ["brain"],
            }
        )
        state = _ReplState()
        await _handle_chat(cli, client, "hello", state)

        # First arg = method, second = params dict containing message.
        client.call.assert_awaited_once()
        method, params = client.call.await_args.args[0], client.call.await_args.args[1]
        assert method == "chat"
        assert params["message"] == "hello"
        assert "conversation_id" not in params  # fresh session

        assert state.conversation_id == "conv-42"
        out = buf.getvalue()
        assert "Hi there." in out
        assert "assistant" in out

    async def test_passes_existing_conversation_id_on_followup(self) -> None:
        cli, _ = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(return_value={"response": "ok", "conversation_id": "conv-1"})
        state = _ReplState()
        state.conversation_id = "conv-1"
        await _handle_chat(cli, client, "follow-up", state)

        params = client.call.await_args.args[1]
        assert params["conversation_id"] == "conv-1"

    async def test_empty_response_renders_placeholder(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(return_value={"response": "", "conversation_id": "c"})
        state = _ReplState()
        await _handle_chat(cli, client, "say nothing", state)
        assert "(empty response)" in buf.getvalue()

    async def test_daemon_error_renders_inline(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(side_effect=ChannelConnectionError("conn refused"))
        state = _ReplState()
        await _handle_chat(cli, client, "ping", state)
        assert "Daemon error" in buf.getvalue()
        # State must not be polluted on failure.
        assert state.conversation_id is None

    async def test_unexpected_exception_does_not_propagate(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(side_effect=RuntimeError("kaboom"))
        state = _ReplState()
        await _handle_chat(cli, client, "msg", state)
        assert "Chat failed" in buf.getvalue()
        assert "kaboom" in buf.getvalue()

    async def test_tags_rendered_as_dim_suffix(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(
            return_value={
                "response": "answer",
                "conversation_id": "c",
                "tags": ["brain", "weather"],
            }
        )
        state = _ReplState()
        await _handle_chat(cli, client, "what's the weather?", state)
        out = buf.getvalue()
        # Tags appear in the assistant header line.
        assert "brain" in out
        assert "weather" in out


# ── _loop ────────────────────────────────────────────────────────────


class TestLoop:
    async def test_blank_input_skipped(self) -> None:
        cli, _ = _capture_console()
        client = AsyncMock()
        client.call = AsyncMock(return_value={"response": "x", "conversation_id": "c"})
        session = _FakeSession(["", "  ", "hi"])
        state = _ReplState()
        await _loop(cli, client, session, state)

        # Only one chat call (the "hi"); blanks were silently skipped.
        assert client.call.await_count == 1
        method, params = client.call.await_args.args[0], client.call.await_args.args[1]
        assert method == "chat"
        assert params["message"] == "hi"

    async def test_slash_then_chat_then_exit(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()

        async def fake_call(method: str, params: dict[str, Any] | None = None, **_kw: Any) -> Any:
            if method == "status":
                return {"version": "0.11.6"}
            if method == "chat":
                return {"response": "hello back", "conversation_id": "c-1"}
            return {}

        client.call = AsyncMock(side_effect=fake_call)
        session = _FakeSession(["/status", "hello", "/exit"])
        state = _ReplState()
        await _loop(cli, client, session, state)

        out = buf.getvalue()
        assert "0.11.6" in out
        assert "hello back" in out
        assert state.conversation_id == "c-1"
        assert client.call.await_count == 2  # /status + chat (no call for /exit)

    async def test_eof_breaks_loop_cleanly(self) -> None:
        cli, buf = _capture_console()
        client = AsyncMock()
        # Empty session → first prompt_async raises EOFError.
        session = _FakeSession([])
        state = _ReplState()
        await _loop(cli, client, session, state)
        assert "Goodbye." in buf.getvalue()


# ── run_repl entry-point ─────────────────────────────────────────────


class TestRunReplDaemonDown:
    def test_returns_1_when_daemon_not_running(self, tmp_path: Path) -> None:
        cli, buf = _capture_console()
        # Socket path under tmp_path → guaranteed missing.
        rc = run_repl(
            socket_path=tmp_path / "missing.sock",
            history_path=tmp_path / "history",
            console=cli,
        )
        assert rc == 1
        assert "daemon is not running" in buf.getvalue()


class TestRunReplFullSession:
    def test_drives_session_until_exit(self, tmp_path: Path) -> None:
        """Patch ``_build_session`` + ``DaemonClient`` to drive the REPL end-to-end."""
        cli, buf = _capture_console()

        # Make is_daemon_running return True without touching the FS.
        fake_client = MagicMock()
        fake_client.is_daemon_running = MagicMock(return_value=True)
        fake_client.call = AsyncMock(return_value={"version": "0.11.6"})
        fake_session = _FakeSession(["/status", "/exit"])

        with (
            patch.object(repl, "DaemonClient", return_value=fake_client),
            patch.object(repl, "_build_session", return_value=fake_session),
        ):
            rc = run_repl(
                socket_path=tmp_path / "sovyx.sock",
                history_path=tmp_path / "history",
                console=cli,
            )

        assert rc == 0
        # Status was fetched + rendered before /exit broke the loop.
        fake_client.call.assert_awaited_with("status")
        out = buf.getvalue()
        assert "0.11.6" in out
        assert "Goodbye." in out

    def test_history_file_created_with_secure_permissions(self, tmp_path: Path) -> None:
        cli, _ = _capture_console()
        history = tmp_path / "history"

        fake_client = MagicMock()
        fake_client.is_daemon_running = MagicMock(return_value=True)
        fake_session = _FakeSession(["/exit"])

        with (
            patch.object(repl, "DaemonClient", return_value=fake_client),
            patch.object(repl, "_build_session", return_value=fake_session),
        ):
            run_repl(
                socket_path=tmp_path / "sovyx.sock",
                history_path=history,
                console=cli,
            )

        assert history.exists()
        # Permission check is POSIX-only — skip on Windows where chmod
        # mode bits don't reflect the same model.
        import sys

        if sys.platform != "win32":
            mode = history.stat().st_mode & 0o777
            assert mode == 0o600


# ── Test for the RPC handler module too — registers expected methods ─


class TestRpcHandlerRegistration:
    def test_register_cli_handlers_adds_expected_methods(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer

        rpc = DaemonRPCServer()
        registry = MagicMock()
        register_cli_handlers(rpc, registry)

        # Direct attribute access — it's a private dict, but the
        # handler set is a public contract: chat, mind.list,
        # mind.forget (T8.21 step 4), mind.retention.prune (T8.21
        # step 6), config.get.
        registered = set(rpc._methods.keys())  # noqa: SLF001
        assert {
            "chat",
            "mind.list",
            "mind.forget",
            "mind.retention.prune",
            "config.get",
        } <= registered


@pytest.mark.asyncio
class TestRpcHandlerBehavior:
    async def test_mind_list_returns_minds_and_active(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer

        rpc = DaemonRPCServer()
        registry = MagicMock()
        mgr = MagicMock()
        mgr.get_active_minds = MagicMock(return_value=["aria", "luna"])
        registry.resolve = AsyncMock(return_value=mgr)

        register_cli_handlers(rpc, registry)
        result = await rpc._methods["mind.list"]()  # noqa: SLF001
        assert result == {"minds": ["aria", "luna"], "active": "aria"}

    async def test_mind_list_no_active(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer

        rpc = DaemonRPCServer()
        registry = MagicMock()
        mgr = MagicMock()
        mgr.get_active_minds = MagicMock(return_value=[])
        registry.resolve = AsyncMock(return_value=mgr)

        register_cli_handlers(rpc, registry)
        result = await rpc._methods["mind.list"]()  # noqa: SLF001
        assert result == {"minds": [], "active": None}

    async def test_config_get_handles_unregistered_personality(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer

        rpc = DaemonRPCServer()
        registry = MagicMock()
        mgr = MagicMock()
        mgr.get_active_minds = MagicMock(return_value=["aria"])
        registry.resolve = AsyncMock(return_value=mgr)
        registry.is_registered = MagicMock(return_value=False)

        register_cli_handlers(rpc, registry)
        result = await rpc._methods["config.get"]()  # noqa: SLF001
        assert result == {"mind_id": "aria", "available": False}

    async def test_mind_retention_prune_returns_serialisable_report(
        self,
        tmp_path: Path,
    ) -> None:
        """``mind.retention.prune`` returns a JSON-serialisable dict
        with every report field. Phase 8 / T8.21 step 6."""
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.config import EngineConfig
        from sovyx.engine.rpc_server import DaemonRPCServer
        from sovyx.persistence.manager import DatabaseManager
        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.brain import get_brain_migrations
        from sovyx.persistence.schemas.conversations import get_conversation_migrations
        from sovyx.persistence.schemas.system import get_system_migrations

        brain = DatabasePool(
            db_path=tmp_path / "brain.db",
            read_pool_size=1,
            load_extensions=["vec0"],
        )
        await brain.initialize()
        runner = MigrationRunner(brain)
        await runner.initialize()
        await runner.run_migrations(
            get_brain_migrations(has_sqlite_vec=brain.has_sqlite_vec),
        )
        conv = DatabasePool(db_path=tmp_path / "conv.db", read_pool_size=1)
        await conv.initialize()
        crunner = MigrationRunner(conv)
        await crunner.initialize()
        await crunner.run_migrations(get_conversation_migrations())
        system = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
        await system.initialize()
        srunner = MigrationRunner(system)
        await srunner.initialize()
        await srunner.run_migrations(get_system_migrations())

        try:
            db_manager = MagicMock(spec=DatabaseManager)
            db_manager.get_brain_pool = MagicMock(return_value=brain)
            db_manager.get_conversation_pool = MagicMock(return_value=conv)
            db_manager.get_system_pool = MagicMock(return_value=system)

            from sovyx.engine.config import DatabaseConfig

            config = EngineConfig(
                data_dir=tmp_path,
                database=DatabaseConfig(data_dir=tmp_path),
            )

            async def _resolve(svc):  # noqa: ANN001
                if svc is EngineConfig:
                    return config
                if svc is DatabaseManager:
                    return db_manager
                msg = f"unexpected resolve target: {svc}"
                raise AssertionError(msg)

            registry = MagicMock()
            registry.resolve = AsyncMock(side_effect=_resolve)
            registry.is_registered = MagicMock(return_value=False)

            rpc = DaemonRPCServer()
            register_cli_handlers(rpc, registry)

            result = await rpc._methods["mind.retention.prune"](  # noqa: SLF001
                mind_id="aria",
                dry_run=True,
            )
            assert isinstance(result, dict)
            assert result["mind_id"] == "aria"
            assert result["dry_run"] is True
            for field in (
                "cutoff_utc",
                "episodes_purged",
                "conversations_purged",
                "conversation_turns_purged",
                "daily_stats_purged",
                "consolidation_log_purged",
                "consent_ledger_purged",
                "effective_horizons",
                "total_brain_rows_purged",
                "total_conversations_rows_purged",
                "total_system_rows_purged",
                "total_rows_purged",
            ):
                assert field in result, f"missing report field: {field}"
            assert isinstance(result["effective_horizons"], dict)
            # Default horizons from RetentionTuningConfig defaults.
            assert result["effective_horizons"]["episodes"] == 30  # noqa: PLR2004
        finally:
            await brain.close()
            await conv.close()
            await system.close()

    async def test_mind_forget_returns_serialisable_report(
        self,
        tmp_path: Path,
    ) -> None:
        """Daemon-side ``mind.forget`` resolves the live pools +
        ledger from the registry, runs the wipe, and returns a
        JSON-serialisable dict with every report field. Phase 8 /
        T8.21 step 4."""
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.config import EngineConfig
        from sovyx.engine.rpc_server import DaemonRPCServer
        from sovyx.persistence.manager import DatabaseManager

        # Real pools + schemas — the brain pool needs vec0 and migrations
        # to mirror the daemon environment.
        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.brain import get_brain_migrations
        from sovyx.persistence.schemas.conversations import get_conversation_migrations
        from sovyx.persistence.schemas.system import get_system_migrations

        brain = DatabasePool(
            db_path=tmp_path / "brain.db",
            read_pool_size=1,
            load_extensions=["vec0"],
        )
        await brain.initialize()
        runner = MigrationRunner(brain)
        await runner.initialize()
        await runner.run_migrations(
            get_brain_migrations(has_sqlite_vec=brain.has_sqlite_vec),
        )

        conv = DatabasePool(db_path=tmp_path / "conversations.db", read_pool_size=1)
        await conv.initialize()
        crunner = MigrationRunner(conv)
        await crunner.initialize()
        await crunner.run_migrations(get_conversation_migrations())

        system = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
        await system.initialize()
        srunner = MigrationRunner(system)
        await srunner.initialize()
        await srunner.run_migrations(get_system_migrations())

        try:
            db_manager = MagicMock(spec=DatabaseManager)
            db_manager.get_brain_pool = MagicMock(return_value=brain)
            db_manager.get_conversation_pool = MagicMock(return_value=conv)
            db_manager.get_system_pool = MagicMock(return_value=system)

            config = MagicMock(spec=EngineConfig)
            config.data_dir = tmp_path

            async def _resolve(svc):  # noqa: ANN001 — registry mock
                if svc is EngineConfig:
                    return config
                if svc is DatabaseManager:
                    return db_manager
                msg = f"unexpected resolve target: {svc}"
                raise AssertionError(msg)

            registry = MagicMock()
            registry.resolve = AsyncMock(side_effect=_resolve)

            rpc = DaemonRPCServer()
            register_cli_handlers(rpc, registry)

            # Empty mind, but the handler still returns a well-formed
            # zero-count report — pins the JSON shape contract.
            result = await rpc._methods["mind.forget"](mind_id="aria", dry_run=True)  # noqa: SLF001

            assert isinstance(result, dict)
            assert result["mind_id"] == "aria"
            assert result["dry_run"] is True
            for field in (
                "concepts_purged",
                "relations_purged",
                "episodes_purged",
                "concept_embeddings_purged",
                "episode_embeddings_purged",
                "conversation_imports_purged",
                "consolidation_log_purged",
                "conversations_purged",
                "conversation_turns_purged",
                "daily_stats_purged",
                "consent_ledger_purged",
                "total_brain_rows_purged",
                "total_conversations_rows_purged",
                "total_system_rows_purged",
                "total_rows_purged",
            ):
                assert field in result, f"missing report field: {field}"
                assert isinstance(result[field], int)
        finally:
            await brain.close()
            await conv.close()
            await system.close()
