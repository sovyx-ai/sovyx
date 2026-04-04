"""Tests for sovyx.observability.logging — structured logging with request context."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest
import structlog

from sovyx.engine.config import LoggingConfig
from sovyx.observability.logging import (
    SecretMasker,
    bind_request_context,
    bound_request_context,
    clear_request_context,
    get_correlation_id,
    get_logger,
    get_request_context,
    set_correlation_id,
    setup_logging,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    """Ensure every test starts and ends with a clean structlog context."""
    clear_request_context()
    yield  # type: ignore[misc]
    clear_request_context()


@pytest.fixture()
def _json_logging() -> None:
    """Set up JSON logging for tests that need real output."""
    setup_logging(LoggingConfig(level="DEBUG", format="json"))


# ── SecretMasker ────────────────────────────────────────────────────────────


class TestSecretMasker:
    """Secret masking processor."""

    def test_masks_token_field(self) -> None:
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "token": "sk-1234567890abcdef"}
        result = masker(None, "info", event)
        assert result["token"] == "sk-...def"

    def test_masks_api_key_field(self) -> None:
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "api_key": "key-abcdefghijklmnop"}
        result = masker(None, "info", event)
        assert result["api_key"] == "key...nop"

    def test_masks_password_field(self) -> None:
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "password": "mysecretpassword"}
        result = masker(None, "info", event)
        assert result["password"] == "mys...ord"

    def test_masks_secret_field(self) -> None:
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "client_secret": "abcdefghij"}
        result = masker(None, "info", event)
        assert result["client_secret"] == "abc...hij"

    def test_short_value_fully_masked(self) -> None:
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "token": "short"}
        result = masker(None, "info", event)
        assert result["token"] == "***"

    def test_does_not_mask_normal_fields(self) -> None:
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "user_name": "guipe", "count": 42}
        result = masker(None, "info", event)
        assert result["user_name"] == "guipe"
        assert result["count"] == 42

    def test_does_not_mask_non_string_sensitive(self) -> None:
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "token": 12345}
        result = masker(None, "info", event)
        assert result["token"] == 12345  # non-string, not masked

    def test_case_insensitive(self) -> None:
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "API_KEY": "abcdefghijklmnop"}
        result = masker(None, "info", event)
        assert result["API_KEY"] == "abc...nop"

    def test_is_sensitive_detection(self) -> None:
        assert SecretMasker._is_sensitive("token") is True
        assert SecretMasker._is_sensitive("api_key") is True
        assert SecretMasker._is_sensitive("password") is True
        assert SecretMasker._is_sensitive("client_secret") is True
        assert SecretMasker._is_sensitive("user_name") is False
        assert SecretMasker._is_sensitive("count") is False

    def test_mask_value_long(self) -> None:
        assert SecretMasker._mask_value("abcdefghij") == "abc...hij"

    def test_mask_value_short(self) -> None:
        assert SecretMasker._mask_value("abc") == "***"

    def test_mask_value_boundary(self) -> None:
        # Exactly 8 chars: "12345678" → "123...678"
        assert SecretMasker._mask_value("12345678") == "123...678"

    def test_mask_value_7_chars(self) -> None:
        # 7 chars: fully masked
        assert SecretMasker._mask_value("1234567") == "***"

    def test_api_key_env_sensitive(self) -> None:
        """api_key_env is in the sensitive set."""
        masker = SecretMasker()
        event: dict[str, Any] = {"event": "test", "api_key_env": "OPENAI_KEY"}
        result = masker(None, "info", event)
        # "OPENAI_KEY" is 10 chars → masked as first3...last3
        assert result["api_key_env"] == "OPE...KEY"


# ── Request Context ─────────────────────────────────────────────────────────


class TestBindRequestContext:
    """bind_request_context / clear / get."""

    def test_bind_and_get(self) -> None:
        bind_request_context(mind_id="test-mind", conversation_id="conv-1")
        ctx = get_request_context()
        assert ctx["mind_id"] == "test-mind"
        assert ctx["conversation_id"] == "conv-1"
        assert "request_id" in ctx  # auto-generated
        assert len(ctx["request_id"]) == 12  # hex[:12]

    def test_bind_with_explicit_request_id(self) -> None:
        bind_request_context(mind_id="m", conversation_id="c", request_id="my-req-id")
        ctx = get_request_context()
        assert ctx["request_id"] == "my-req-id"

    def test_bind_with_correlation_id(self) -> None:
        bind_request_context(
            mind_id="m",
            conversation_id="c",
            correlation_id="trace-xyz",
        )
        ctx = get_request_context()
        assert ctx["correlation_id"] == "trace-xyz"

    def test_bind_without_correlation_id_omits_key(self) -> None:
        bind_request_context(mind_id="m", conversation_id="c")
        ctx = get_request_context()
        assert "correlation_id" not in ctx

    def test_bind_empty_mind_id_omits_key(self) -> None:
        bind_request_context(mind_id="", conversation_id="c")
        ctx = get_request_context()
        assert "mind_id" not in ctx

    def test_bind_empty_conversation_id_omits_key(self) -> None:
        bind_request_context(mind_id="m", conversation_id="")
        ctx = get_request_context()
        assert "conversation_id" not in ctx

    def test_bind_extra_kwargs(self) -> None:
        bind_request_context(mind_id="m", conversation_id="c", person_name="Guipe")
        ctx = get_request_context()
        assert ctx["person_name"] == "Guipe"

    def test_clear_removes_all(self) -> None:
        bind_request_context(mind_id="m", conversation_id="c")
        clear_request_context()
        assert get_request_context() == {}

    def test_auto_generated_request_id_is_unique(self) -> None:
        bind_request_context(mind_id="m", conversation_id="c")
        rid1 = get_request_context()["request_id"]
        clear_request_context()
        bind_request_context(mind_id="m", conversation_id="c")
        rid2 = get_request_context()["request_id"]
        assert rid1 != rid2


class TestBoundRequestContext:
    """bound_request_context context manager."""

    def test_binds_on_entry_resets_on_exit(self) -> None:
        with bound_request_context(mind_id="inner", conversation_id="c1"):
            ctx = get_request_context()
            assert ctx["mind_id"] == "inner"
        # After exit — context should be clean (no leaks)
        after = get_request_context()
        assert "mind_id" not in after or after.get("mind_id") == ""

    def test_restores_previous_context(self) -> None:
        bind_request_context(mind_id="outer", conversation_id="outer-c")
        with bound_request_context(
            mind_id="inner", conversation_id="inner-c", request_id="r-inner"
        ):
            assert get_request_context()["mind_id"] == "inner"
        # Outer restored
        ctx = get_request_context()
        assert ctx["mind_id"] == "outer"

    def test_fixed_request_id(self) -> None:
        with bound_request_context(mind_id="m", conversation_id="c", request_id="fixed-123"):
            assert get_request_context()["request_id"] == "fixed-123"

    def test_auto_request_id(self) -> None:
        with bound_request_context(mind_id="m", conversation_id="c"):
            rid = get_request_context()["request_id"]
            assert len(rid) == 12

    def test_with_correlation_id(self) -> None:
        with bound_request_context(
            mind_id="m",
            conversation_id="c",
            correlation_id="trace-1",
        ):
            assert get_request_context()["correlation_id"] == "trace-1"
        assert "correlation_id" not in get_request_context()

    def test_cleanup_on_exception(self) -> None:
        """Context is properly cleaned up even if body raises."""
        with (
            pytest.raises(ValueError, match="boom"),
            bound_request_context(mind_id="m", conversation_id="c"),
        ):
            raise ValueError("boom")
        # Must be clean
        assert "mind_id" not in get_request_context()


# ── Backward Compatibility ──────────────────────────────────────────────────


class TestCorrelationIdCompat:
    """set_correlation_id / get_correlation_id backward compat."""

    def test_set_and_get(self) -> None:
        set_correlation_id("corr-abc")
        assert get_correlation_id() == "corr-abc"

    def test_clear_with_empty_string(self) -> None:
        set_correlation_id("corr-abc")
        set_correlation_id("")
        assert get_correlation_id() == ""

    def test_appears_in_request_context(self) -> None:
        set_correlation_id("corr-xyz")
        ctx = get_request_context()
        assert ctx["correlation_id"] == "corr-xyz"

    def test_absent_when_empty(self) -> None:
        set_correlation_id("")
        ctx = get_request_context()
        assert "correlation_id" not in ctx


# ── Setup Logging ───────────────────────────────────────────────────────────


class TestSetupLogging:
    """Logging setup with JSON and console modes."""

    def test_setup_json_mode(self) -> None:
        config = LoggingConfig(level="DEBUG", format="json")
        setup_logging(config)

        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1

    def test_setup_text_mode(self) -> None:
        config = LoggingConfig(level="INFO", format="text")
        setup_logging(config)

        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_json_output_contains_required_fields(self) -> None:
        """JSON logs contain required structlog processors."""
        config = LoggingConfig(level="DEBUG", format="json")
        setup_logging(config)

        cfg = structlog.get_config()
        processors = cfg["processors"]
        assert len(processors) > 0

        root = logging.getLogger()
        handler = root.handlers[0]
        assert handler.formatter is not None

        logger = get_logger("test.module")
        logger.info("test event", extra_field="value")

    def test_processor_chain_includes_merge_contextvars(self) -> None:
        """merge_contextvars is first in the processor chain."""
        setup_logging(LoggingConfig(level="DEBUG", format="json"))
        cfg = structlog.get_config()
        processors = cfg["processors"]
        # First processor should be merge_contextvars
        assert processors[0] is structlog.contextvars.merge_contextvars

    def test_processor_chain_includes_secret_masker(self) -> None:
        """SecretMasker is in the processor chain."""
        setup_logging(LoggingConfig(level="DEBUG", format="json"))
        cfg = structlog.get_config()
        processors = cfg["processors"]
        masker_found = any(isinstance(p, SecretMasker) for p in processors)
        assert masker_found


# ── JSON Output Integration ─────────────────────────────────────────────────


class TestJSONOutput:
    """End-to-end JSON log output with structured context."""

    @pytest.fixture(autouse=True)
    def _setup_json(self) -> None:
        setup_logging(LoggingConfig(level="DEBUG", format="json"))

    def _capture_log(
        self,
        logger: structlog.stdlib.BoundLogger,
        level: str,
        event: str,
        **kw: str,
    ) -> dict[str, Any]:
        """Capture a single log line as parsed JSON."""
        buf = io.StringIO()
        root = logging.getLogger()
        handler = root.handlers[0]
        old_stream = handler.stream
        handler.stream = buf  # type: ignore[assignment]
        try:
            getattr(logger, level)(event, **kw)
        finally:
            handler.stream = old_stream  # type: ignore[assignment]
        line = buf.getvalue().strip()
        assert line, "No log output captured"
        return json.loads(line)

    def test_context_fields_in_json(self) -> None:
        bind_request_context(
            mind_id="nyx",
            conversation_id="conv-42",
            request_id="req-abc",
        )
        logger = get_logger("test.json")
        parsed = self._capture_log(logger, "info", "test_event", extra="val")
        assert parsed["mind_id"] == "nyx"
        assert parsed["conversation_id"] == "conv-42"
        assert parsed["request_id"] == "req-abc"
        assert parsed["event"] == "test_event"
        assert parsed["extra"] == "val"
        assert parsed["level"] == "info"
        assert "timestamp" in parsed

    def test_no_context_fields_when_unbound(self) -> None:
        logger = get_logger("test.json")
        parsed = self._capture_log(logger, "info", "bare_event")
        assert "mind_id" not in parsed
        assert "conversation_id" not in parsed
        # request_id should NOT be present when not bound
        assert "request_id" not in parsed

    def test_correlation_id_in_json(self) -> None:
        set_correlation_id("corr-json-test")
        logger = get_logger("test.json")
        parsed = self._capture_log(logger, "info", "with_corr")
        assert parsed["correlation_id"] == "corr-json-test"

    def test_secret_masking_in_json(self) -> None:
        logger = get_logger("test.json")
        parsed = self._capture_log(
            logger, "info", "secret_event", api_key="sk-very-long-secret-key"
        )
        assert parsed["api_key"] == "sk-...key"

    def test_bound_context_manager_in_json(self) -> None:
        logger = get_logger("test.json")
        with bound_request_context(mind_id="ctx-m", conversation_id="ctx-c", request_id="ctx-r"):
            parsed = self._capture_log(logger, "info", "in_ctx")
        assert parsed["mind_id"] == "ctx-m"
        assert parsed["request_id"] == "ctx-r"

    def test_timestamp_is_iso(self) -> None:
        logger = get_logger("test.json")
        parsed = self._capture_log(logger, "info", "ts_test")
        ts = parsed["timestamp"]
        # ISO format: YYYY-MM-DDTHH:MM:SS...
        assert "T" in ts
        assert ts.endswith("Z") or "+" in ts or len(ts) > 20


# ── GetLogger ───────────────────────────────────────────────────────────────


class TestGetLogger:
    """Logger factory."""

    def test_returns_bound_logger(self) -> None:
        setup_logging(LoggingConfig())
        logger = get_logger("sovyx.brain")
        assert logger is not None

    def test_logger_has_standard_methods(self) -> None:
        setup_logging(LoggingConfig())
        logger = get_logger("sovyx.test")
        assert hasattr(logger, "info")
        assert hasattr(logger, "debug")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "error")
        assert hasattr(logger, "exception")
