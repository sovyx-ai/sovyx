"""Tests for sovyx.observability.saga — saga/span scopes + trace_saga decorator."""

from __future__ import annotations

import re
from collections.abc import Generator
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from structlog.contextvars import bind_contextvars, clear_contextvars

from sovyx.observability import saga as saga_mod
from sovyx.observability.saga import (
    SagaHandle,
    _build_saga_binds,
    _new_id,
    async_saga_scope,
    async_span_scope,
    begin_saga,
    current_event_id,
    current_saga_id,
    current_span_id,
    end_saga,
    saga_scope,
    span_scope,
    trace_saga,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_HEX16 = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture(autouse=True)
def _clean_contextvars() -> Generator[None, None, None]:
    """Each test starts with an empty structlog contextvar bag."""
    clear_contextvars()
    yield
    clear_contextvars()


@pytest.fixture()
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``saga_mod.logger`` so tests can inspect every emission."""
    mock = MagicMock()
    monkeypatch.setattr(saga_mod, "logger", mock)
    return mock


def _events(fake_logger: MagicMock, level: str) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(event_name, kwargs)`` for every call on ``fake_logger.<level>``."""
    method = getattr(fake_logger, level)
    return [(call.args[0], call.kwargs) for call in method.call_args_list]


def _all_events(fake_logger: MagicMock) -> list[str]:
    """Return the ordered list of event names emitted across info+error."""
    info = [c.args[0] for c in fake_logger.info.call_args_list]
    err = [c.args[0] for c in fake_logger.error.call_args_list]
    # Calls were interleaved; reconstruct in mock_calls order to preserve sequence.
    sequence: list[str] = []
    for call in fake_logger.mock_calls:
        if call[0] in {"info", "error"} and call.args:
            sequence.append(call.args[0])
    # Sanity: every emitted name should appear in info+err lists.
    assert sorted(sequence) == sorted(info + err)
    return sequence


class TestIdHelpers:
    """``_new_id`` and the three ``current_*`` accessors."""

    def test_new_id_is_16_hex_chars(self) -> None:
        for _ in range(8):
            assert _HEX16.match(_new_id())

    def test_new_id_unique_across_calls(self) -> None:
        ids = {_new_id() for _ in range(64)}
        assert len(ids) == 64

    def test_current_saga_id_none_outside_scope(self) -> None:
        assert current_saga_id() is None
        assert current_event_id() is None
        assert current_span_id() is None

    def test_current_saga_id_reads_contextvar(self) -> None:
        bind_contextvars(saga_id="hardcoded-saga")
        assert current_saga_id() == "hardcoded-saga"

    def test_current_event_id_reads_contextvar(self) -> None:
        bind_contextvars(event_id="hardcoded-event")
        assert current_event_id() == "hardcoded-event"

    def test_current_span_id_reads_contextvar(self) -> None:
        bind_contextvars(span_id="hardcoded-span")
        assert current_span_id() == "hardcoded-span"

    def test_current_accessors_ignore_non_string_values(self) -> None:
        bind_contextvars(saga_id=12345)
        assert current_saga_id() is None


class TestBuildSagaBinds:
    """``_build_saga_binds`` enforces that ``saga_id`` is reserved."""

    def test_returns_only_saga_id_when_no_extra_binds(self) -> None:
        result = _build_saga_binds("s-1", None)
        assert result == {"saga_id": "s-1"}

    def test_merges_extra_binds(self) -> None:
        result = _build_saga_binds("s-1", {"channel_id": "tg-42", "mind_id": "m-9"})
        assert result == {"saga_id": "s-1", "channel_id": "tg-42", "mind_id": "m-9"}

    def test_caller_cannot_overwrite_saga_id(self) -> None:
        result = _build_saga_binds("s-real", {"saga_id": "s-attacker", "k": "v"})
        assert result["saga_id"] == "s-real"
        assert result["k"] == "v"


class TestSagaScopeSync:
    """Synchronous ``saga_scope`` lifecycle + contextvar bookkeeping."""

    def test_yields_saga_id_and_binds_contextvar(self, fake_logger: MagicMock) -> None:
        with saga_scope("test.saga") as saga_id:
            assert _HEX16.match(saga_id)
            assert current_saga_id() == saga_id

    def test_emits_started_then_completed_on_success(self, fake_logger: MagicMock) -> None:
        with saga_scope("cog.turn", kind="cognitive"):
            pass
        info = _events(fake_logger, "info")
        names = [name for name, _ in info]
        assert names == ["saga.started", "saga.completed"]
        assert info[0][1]["saga_name"] == "cog.turn"
        assert info[0][1]["kind"] == "cognitive"
        assert info[1][1]["saga_name"] == "cog.turn"
        assert "duration_ms" in info[1][1]
        assert info[1][1]["duration_ms"] >= 0

    def test_emits_failed_on_exception_and_reraises(self, fake_logger: MagicMock) -> None:
        with pytest.raises(RuntimeError, match="boom"), saga_scope("bad.saga"):
            msg = "boom"
            raise RuntimeError(msg)
        info_names = [c.args[0] for c in fake_logger.info.call_args_list]
        err_calls = fake_logger.error.call_args_list
        assert info_names == ["saga.started"]
        assert len(err_calls) == 1
        assert err_calls[0].args[0] == "saga.failed"
        kwargs = err_calls[0].kwargs
        assert kwargs["saga_name"] == "bad.saga"
        assert kwargs["exc_type"] == "RuntimeError"
        assert kwargs["exc_msg"] == "boom"
        assert "duration_ms" in kwargs

    def test_emits_failed_on_base_exception(self, fake_logger: MagicMock) -> None:
        with pytest.raises(KeyboardInterrupt), saga_scope("interrupted"):
            raise KeyboardInterrupt
        err_calls = fake_logger.error.call_args_list
        assert len(err_calls) == 1
        assert err_calls[0].kwargs["exc_type"] == "KeyboardInterrupt"

    def test_restores_parent_contextvars_on_exit(self, fake_logger: MagicMock) -> None:
        bind_contextvars(saga_id="parent-saga")
        with saga_scope("inner"):
            assert current_saga_id() != "parent-saga"
        assert current_saga_id() == "parent-saga"

    def test_clears_saga_id_when_no_parent(self, fake_logger: MagicMock) -> None:
        with saga_scope("only"):
            assert current_saga_id() is not None
        assert current_saga_id() is None

    def test_nested_sagas_have_distinct_ids(self, fake_logger: MagicMock) -> None:
        with saga_scope("outer") as outer:
            with saga_scope("inner") as inner:
                assert outer != inner
                assert current_saga_id() == inner
            assert current_saga_id() == outer

    def test_binds_extra_kwargs_into_contextvars(self, fake_logger: MagicMock) -> None:
        with saga_scope("bridge.in", binds={"channel_id": "tg-7"}):
            from structlog.contextvars import get_contextvars

            assert get_contextvars().get("channel_id") == "tg-7"
        from structlog.contextvars import get_contextvars

        assert "channel_id" not in get_contextvars()


class TestSagaScopeAsync:
    """Async ``async_saga_scope`` mirrors the sync contract."""

    @pytest.mark.asyncio
    async def test_yields_saga_id_and_binds_contextvar(self, fake_logger: MagicMock) -> None:
        async with async_saga_scope("a.saga") as saga_id:
            assert _HEX16.match(saga_id)
            assert current_saga_id() == saga_id

    @pytest.mark.asyncio
    async def test_emits_started_completed_on_success(self, fake_logger: MagicMock) -> None:
        async with async_saga_scope("voice.turn", kind="voice"):
            pass
        names = [c.args[0] for c in fake_logger.info.call_args_list]
        assert names == ["saga.started", "saga.completed"]

    @pytest.mark.asyncio
    async def test_emits_failed_on_exception_and_reraises(self, fake_logger: MagicMock) -> None:
        with pytest.raises(ValueError, match="async-boom"):
            async with async_saga_scope("bad"):
                msg = "async-boom"
                raise ValueError(msg)
        err_calls = fake_logger.error.call_args_list
        assert len(err_calls) == 1
        assert err_calls[0].args[0] == "saga.failed"
        assert err_calls[0].kwargs["exc_type"] == "ValueError"
        assert err_calls[0].kwargs["exc_msg"] == "async-boom"

    @pytest.mark.asyncio
    async def test_restores_parent_contextvars_on_exit(self, fake_logger: MagicMock) -> None:
        bind_contextvars(saga_id="parent-async")
        async with async_saga_scope("child"):
            assert current_saga_id() != "parent-async"
        assert current_saga_id() == "parent-async"


class TestSpanScopeSync:
    """``span_scope`` lifecycle + cause_id inheritance."""

    def test_yields_span_id_and_binds_contextvar(self, fake_logger: MagicMock) -> None:
        with span_scope("llm.call") as span_id:
            assert _HEX16.match(span_id)
            assert current_span_id() == span_id

    def test_emits_started_completed_on_success(self, fake_logger: MagicMock) -> None:
        with span_scope("plugin.invoke"):
            pass
        names = [c.args[0] for c in fake_logger.info.call_args_list]
        assert names == ["span.started", "span.completed"]

    def test_emits_failed_on_exception_and_reraises(self, fake_logger: MagicMock) -> None:
        with pytest.raises(RuntimeError), span_scope("bad.span"):
            raise RuntimeError
        err = fake_logger.error.call_args_list
        assert len(err) == 1
        assert err[0].args[0] == "span.failed"
        assert err[0].kwargs["span_name"] == "bad.span"

    def test_inherits_cause_id_from_event_id_when_not_supplied(
        self, fake_logger: MagicMock
    ) -> None:
        bind_contextvars(event_id="evt-parent")
        with span_scope("inherited"):
            from structlog.contextvars import get_contextvars

            assert get_contextvars().get("cause_id") == "evt-parent"

    def test_explicit_cause_id_wins_over_event_id(self, fake_logger: MagicMock) -> None:
        bind_contextvars(event_id="evt-parent")
        with span_scope("explicit", cause_id="explicit-cause"):
            from structlog.contextvars import get_contextvars

            assert get_contextvars().get("cause_id") == "explicit-cause"

    def test_no_cause_id_bound_when_neither_supplied_nor_inherited(
        self, fake_logger: MagicMock
    ) -> None:
        with span_scope("naked"):
            from structlog.contextvars import get_contextvars

            assert "cause_id" not in get_contextvars()

    def test_restores_parent_span_id_on_exit(self, fake_logger: MagicMock) -> None:
        with span_scope("outer") as outer:
            with span_scope("inner") as inner:
                assert outer != inner
                assert current_span_id() == inner
            assert current_span_id() == outer


class TestSpanScopeAsync:
    """``async_span_scope`` mirrors the sync contract."""

    @pytest.mark.asyncio
    async def test_yields_span_id_and_binds_contextvar(self, fake_logger: MagicMock) -> None:
        async with async_span_scope("a.span") as span_id:
            assert _HEX16.match(span_id)
            assert current_span_id() == span_id

    @pytest.mark.asyncio
    async def test_inherits_cause_id_from_event_id(self, fake_logger: MagicMock) -> None:
        bind_contextvars(event_id="evt-async-parent")
        async with async_span_scope("inherited"):
            from structlog.contextvars import get_contextvars

            assert get_contextvars().get("cause_id") == "evt-async-parent"

    @pytest.mark.asyncio
    async def test_explicit_cause_id_wins(self, fake_logger: MagicMock) -> None:
        bind_contextvars(event_id="evt-async-parent")
        async with async_span_scope("explicit", cause_id="manual-cause"):
            from structlog.contextvars import get_contextvars

            assert get_contextvars().get("cause_id") == "manual-cause"

    @pytest.mark.asyncio
    async def test_emits_failed_on_exception_and_reraises(self, fake_logger: MagicMock) -> None:
        with pytest.raises(RuntimeError, match="async-span-boom"):
            async with async_span_scope("bad"):
                msg = "async-span-boom"
                raise RuntimeError(msg)
        err = fake_logger.error.call_args_list
        assert len(err) == 1
        assert err[0].args[0] == "span.failed"


class TestTraceSagaDecorator:
    """``@trace_saga`` dispatches sync vs async via iscoroutinefunction."""

    def test_sync_function_runs_inside_saga_scope(self, fake_logger: MagicMock) -> None:
        captured: dict[str, str | None] = {}

        @trace_saga("decorated.sync", kind="dec")
        def fn(x: int) -> int:
            captured["saga_id"] = current_saga_id()
            return x * 2

        assert fn(21) == 42
        assert captured["saga_id"] is not None
        assert _HEX16.match(captured["saga_id"] or "")
        names = [c.args[0] for c in fake_logger.info.call_args_list]
        assert names == ["saga.started", "saga.completed"]
        assert fake_logger.info.call_args_list[0].kwargs["kind"] == "dec"

    def test_sync_function_propagates_exception(self, fake_logger: MagicMock) -> None:
        @trace_saga("decorated.bad")
        def boom() -> None:
            msg = "kaboom"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="kaboom"):
            boom()
        assert fake_logger.error.call_args_list[0].args[0] == "saga.failed"

    def test_sync_decorator_preserves_metadata(self, fake_logger: MagicMock) -> None:
        @trace_saga("meta.preserve")
        def my_function(arg: str) -> str:
            """My docstring."""
            return arg

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."

    @pytest.mark.asyncio
    async def test_async_function_runs_inside_async_saga_scope(
        self, fake_logger: MagicMock
    ) -> None:
        captured: dict[str, str | None] = {}

        @trace_saga("decorated.async", kind="acog")
        async def fn() -> str:
            captured["saga_id"] = current_saga_id()
            return "ok"

        result = await fn()
        assert result == "ok"
        assert captured["saga_id"] is not None
        names = [c.args[0] for c in fake_logger.info.call_args_list]
        assert names == ["saga.started", "saga.completed"]

    @pytest.mark.asyncio
    async def test_async_function_propagates_exception(self, fake_logger: MagicMock) -> None:
        @trace_saga("decorated.async.bad")
        async def boom() -> None:
            msg = "async-kaboom"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="async-kaboom"):
            await boom()
        assert fake_logger.error.call_args_list[0].args[0] == "saga.failed"

    @pytest.mark.asyncio
    async def test_async_decorator_preserves_metadata(self, fake_logger: MagicMock) -> None:
        @trace_saga("meta.async")
        async def my_async_function() -> None:
            """Async docstring."""

        assert my_async_function.__name__ == "my_async_function"
        assert my_async_function.__doc__ == "Async docstring."

    def test_dispatch_picks_sync_for_def(self, fake_logger: MagicMock) -> None:
        # Wrapping a plain ``def`` returns a *sync* callable, not a coroutine.
        @trace_saga("dispatch.sync")
        def fn() -> int:
            return 1

        result = fn()
        assert result == 1  # not awaitable


class TestBeginEndSaga:
    """``begin_saga`` / ``end_saga`` — manual lifetime via SagaHandle."""

    def test_begin_returns_handle_and_binds_saga_id(self, fake_logger: MagicMock) -> None:
        handle = begin_saga("manual.saga", kind="manual")
        try:
            assert isinstance(handle, SagaHandle)
            assert _HEX16.match(handle.saga_id)
            assert handle.name == "manual.saga"
            assert handle.kind == "manual"
            assert current_saga_id() == handle.saga_id
            assert fake_logger.info.call_args_list[0].args[0] == "saga.started"
            assert fake_logger.info.call_args_list[0].kwargs["saga_name"] == "manual.saga"
        finally:
            end_saga(handle)

    def test_end_saga_emits_completed_and_resets_contextvars(self, fake_logger: MagicMock) -> None:
        handle = begin_saga("lifecycle")
        end_saga(handle)
        info_names = [c.args[0] for c in fake_logger.info.call_args_list]
        assert info_names == ["saga.started", "saga.completed"]
        assert current_saga_id() is None

    def test_end_saga_emits_failed_when_exception_provided(self, fake_logger: MagicMock) -> None:
        handle = begin_saga("fail.path")
        end_saga(handle, exc=RuntimeError("oops"))
        assert fake_logger.error.call_args_list[0].args[0] == "saga.failed"
        kwargs = fake_logger.error.call_args_list[0].kwargs
        assert kwargs["exc_type"] == "RuntimeError"
        assert kwargs["exc_msg"] == "oops"
        assert kwargs["saga_name"] == "fail.path"
        assert kwargs["kind"] == "default"

    def test_end_saga_restores_parent_saga_id(self, fake_logger: MagicMock) -> None:
        bind_contextvars(saga_id="outer-parent")
        handle = begin_saga("child")
        assert current_saga_id() == handle.saga_id
        end_saga(handle)
        assert current_saga_id() == "outer-parent"

    def test_saga_handle_is_frozen(self, fake_logger: MagicMock) -> None:
        handle = begin_saga("frozen")
        try:
            with pytest.raises(Exception) as exc_info:
                handle.saga_id = "tampered"  # type: ignore[misc]
            # FrozenInstanceError lives in dataclasses; xdist-safe class name check.
            assert type(exc_info.value).__name__ == "FrozenInstanceError"
        finally:
            end_saga(handle)


class TestEmitFailedHelpers:
    """Indirect coverage of ``_emit_failed`` via saga_scope when chained context."""

    def test_failed_payload_carries_duration_ms(self, fake_logger: MagicMock) -> None:
        with pytest.raises(ValueError), saga_scope("d.test"):
            msg = "x"
            raise ValueError(msg)
        kwargs = fake_logger.error.call_args_list[0].kwargs
        assert isinstance(kwargs["duration_ms"], float)
        assert kwargs["duration_ms"] >= 0

    def test_completed_payload_carries_duration_ms(self, fake_logger: MagicMock) -> None:
        with saga_scope("ok"):
            pass
        kwargs = fake_logger.info.call_args_list[1].kwargs
        assert isinstance(kwargs["duration_ms"], float)
        assert kwargs["duration_ms"] >= 0


class TestTraceSagaIsCallable:
    """Smoke check: ``trace_saga`` returns a decorator usable on bare callables."""

    def test_returns_callable_decorator(self) -> None:
        decorator: Callable[..., Any] = trace_saga("smoke")
        assert callable(decorator)
