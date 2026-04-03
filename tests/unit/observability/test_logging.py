"""Tests for sovyx.observability.logging — structured logging."""

from __future__ import annotations

import asyncio
import logging

from sovyx.engine.config import LoggingConfig
from sovyx.observability.logging import (
    SecretMasker,
    get_correlation_id,
    get_logger,
    set_correlation_id,
    setup_logging,
)


class TestSecretMasker:
    """Secret masking processor."""

    def test_masks_token_field(self) -> None:
        masker = SecretMasker()
        event = {"event": "test", "token": "sk-1234567890abcdef"}
        result = masker(None, "info", event)
        assert result["token"] == "sk-...def"

    def test_masks_api_key_field(self) -> None:
        masker = SecretMasker()
        event = {"event": "test", "api_key": "key-abcdefghijklmnop"}
        result = masker(None, "info", event)
        assert result["api_key"] == "key...nop"

    def test_masks_password_field(self) -> None:
        masker = SecretMasker()
        event = {"event": "test", "password": "mysecretpassword"}
        result = masker(None, "info", event)
        assert result["password"] == "mys...ord"

    def test_masks_secret_field(self) -> None:
        masker = SecretMasker()
        event = {"event": "test", "client_secret": "abcdefghij"}
        result = masker(None, "info", event)
        assert result["client_secret"] == "abc...hij"

    def test_short_value_fully_masked(self) -> None:
        masker = SecretMasker()
        event = {"event": "test", "token": "short"}
        result = masker(None, "info", event)
        assert result["token"] == "***"

    def test_does_not_mask_normal_fields(self) -> None:
        masker = SecretMasker()
        event = {"event": "test", "user_name": "guipe", "count": 42}
        result = masker(None, "info", event)
        assert result["user_name"] == "guipe"
        assert result["count"] == 42

    def test_does_not_mask_non_string_sensitive(self) -> None:
        masker = SecretMasker()
        event = {"event": "test", "token": 12345}
        result = masker(None, "info", event)
        assert result["token"] == 12345  # non-string, not masked

    def test_case_insensitive(self) -> None:
        masker = SecretMasker()
        event = {"event": "test", "API_KEY": "abcdefghijklmnop"}
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
        assert SecretMasker._mask_value("short") == "***"

    def test_mask_value_boundary(self) -> None:
        # Exactly 8 chars
        assert SecretMasker._mask_value("12345678") == "123...678"
        # 7 chars — still short
        assert SecretMasker._mask_value("1234567") == "***"


class TestCorrelationId:
    """Correlation ID management via contextvars."""

    def test_default_is_empty(self) -> None:
        # Reset to default
        set_correlation_id("")
        assert get_correlation_id() == ""

    def test_set_and_get(self) -> None:
        set_correlation_id("req-123")
        assert get_correlation_id() == "req-123"
        set_correlation_id("")  # cleanup

    def test_async_isolation(self) -> None:
        """Different coroutines get different correlation IDs."""
        results: dict[str, str] = {}

        async def worker(name: str, cid: str) -> None:
            set_correlation_id(cid)
            await asyncio.sleep(0.01)
            results[name] = get_correlation_id()

        async def main() -> None:
            await asyncio.gather(
                worker("a", "cid-a"),
                worker("b", "cid-b"),
            )

        asyncio.run(main())
        assert results["a"] == "cid-a"
        assert results["b"] == "cid-b"


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

    def test_json_output_contains_required_fields(self, capsys: object) -> None:
        """JSON logs contain required structlog processors."""
        config = LoggingConfig(level="DEBUG", format="json")
        setup_logging(config)

        # Verify structlog processor chain is configured
        import structlog as _structlog

        cfg = _structlog.get_config()
        processors = cfg["processors"]
        assert len(processors) > 0

        # Verify JSON renderer is on the handler formatter
        root = logging.getLogger()
        handler = root.handlers[0]
        assert handler.formatter is not None

        # Verify actual log goes through without error
        logger = get_logger("test.module")
        logger.info("test event", extra_field="value")

    def test_correlation_id_in_json_output(self) -> None:
        """JSON output includes correlation_id when set."""
        config = LoggingConfig(level="DEBUG", format="json")
        setup_logging(config)
        set_correlation_id("test-corr-123")

        # Verify via processor directly
        from sovyx.observability.logging import _add_correlation_id

        event_dict: dict[str, object] = {"event": "test"}
        result = _add_correlation_id(None, "info", event_dict)
        assert result["correlation_id"] == "test-corr-123"
        set_correlation_id("")  # cleanup

    def test_correlation_id_absent_when_empty(self) -> None:
        """No correlation_id field when not set."""
        from sovyx.observability.logging import _add_correlation_id

        set_correlation_id("")
        event_dict: dict[str, object] = {"event": "test"}
        result = _add_correlation_id(None, "info", event_dict)
        assert "correlation_id" not in result


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
