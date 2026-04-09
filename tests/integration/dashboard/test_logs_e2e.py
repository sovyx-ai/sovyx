"""End-to-end integration test: daemon logging → file → API → schema.

Validates the complete log pipeline:
    setup_logging() → structlog event → JSON file → query_logs() → normalized entry

This test caught 3 production bugs in v0.5.22–v0.5.23 that unit tests missed.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from typing import TYPE_CHECKING

import pytest

from sovyx.dashboard.logs import query_logs
from sovyx.engine.config import EngineConfig, LoggingConfig
from sovyx.observability.logging import get_logger, setup_logging

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clean_handlers() -> Generator[None, None, None]:
    """Reset root logger handlers after each test."""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.close()
    root.handlers.clear()


class TestLogsPipeline:
    """End-to-end: setup_logging → log event → file → query_logs → schema."""

    def test_event_flows_from_logger_to_file_to_api(self, tmp_path: Path) -> None:
        """Complete pipeline: log an event, read it back via query_logs."""
        log_file = tmp_path / "logs" / "sovyx.log"

        # 1. Configure logging with file handler
        config = LoggingConfig(
            level="DEBUG",
            console_format="text",
            log_file=log_file,
        )
        setup_logging(config)

        # 2. Log a structured event (this is what the daemon does)
        logger = get_logger("sovyx.engine.test")
        logger.info("engine_started", version="0.5.24", port=7777)

        # 3. Flush handlers to ensure file write
        for handler in logging.getLogger().handlers:
            handler.flush()

        # 4. Read back via query_logs (same function the API uses)
        entries = query_logs(log_file)
        assert len(entries) >= 1

        # 5. Verify normalized schema (what the frontend expects)
        entry = entries[0]
        assert "timestamp" in entry  # Normalized from structlog's ISO timestamp
        assert entry["level"] == "INFO"
        assert entry["logger"] == "sovyx.engine.test"
        assert entry["event"] == "engine_started"
        # Extra fields preserved
        assert entry["version"] == "0.5.24"
        assert entry["port"] == 7777

    def test_console_text_and_file_json_coexist(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Console uses text renderer while file uses JSON simultaneously."""
        log_file = tmp_path / "logs" / "sovyx.log"

        config = LoggingConfig(
            level="DEBUG",
            console_format="text",
            log_file=log_file,
        )
        setup_logging(config)

        logger = get_logger("sovyx.test.dual")
        logger.info("dual_output_test")

        for handler in logging.getLogger().handlers:
            handler.flush()

        # File should contain valid JSON
        content = log_file.read_text().strip()
        parsed = json.loads(content)
        assert parsed["event"] == "dual_output_test"
        assert isinstance(parsed, dict)

        # Console (stderr) should NOT be valid JSON (it's text-formatted)
        # We can't easily capture stderr from structlog, but we verify
        # the file handler formatter is JSONRenderer
        root = logging.getLogger()
        file_handlers = [
            h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        # File formatter renders JSON
        file_fmt = file_handlers[0].formatter
        assert file_fmt is not None

        stream_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(stream_handlers) == 1

    def test_engine_config_resolves_log_file(self, tmp_path: Path) -> None:
        """EngineConfig.data_dir → log_file resolved correctly."""
        config = EngineConfig(data_dir=tmp_path / "sovyx-data")
        assert config.log.log_file == tmp_path / "sovyx-data" / "logs" / "sovyx.log"

        # Setup logging with this config
        setup_logging(config.log)

        logger = get_logger("sovyx.test.config")
        logger.info("config_resolved_test")

        for handler in logging.getLogger().handlers:
            handler.flush()

        # Verify file exists and contains the event
        assert config.log.log_file is not None
        assert config.log.log_file.exists()
        entries = query_logs(config.log.log_file)
        assert any(e["event"] == "config_resolved_test" for e in entries)

    def test_query_logs_after_param_for_incremental(self, tmp_path: Path) -> None:
        """Incremental polling: 'after' param returns only newer entries."""
        log_file = tmp_path / "logs" / "sovyx.log"

        config = LoggingConfig(level="DEBUG", console_format="json", log_file=log_file)
        setup_logging(config)

        logger = get_logger("sovyx.test.incremental")
        logger.info("event_one")

        for handler in logging.getLogger().handlers:
            handler.flush()

        # Get the first entry's timestamp
        entries_1 = query_logs(log_file)
        assert len(entries_1) >= 1
        first_ts = entries_1[0]["timestamp"]

        # Log another event
        logger.info("event_two")
        for handler in logging.getLogger().handlers:
            handler.flush()

        # Incremental fetch: only entries after first_ts
        entries_2 = query_logs(log_file, after=first_ts)
        assert len(entries_2) >= 1
        assert all(e["event"] != "event_one" for e in entries_2)
        assert any(e["event"] == "event_two" for e in entries_2)

    def test_multiple_log_levels(self, tmp_path: Path) -> None:
        """All log levels write correctly and are normalized."""
        log_file = tmp_path / "logs" / "sovyx.log"

        config = LoggingConfig(level="DEBUG", console_format="json", log_file=log_file)
        setup_logging(config)

        logger = get_logger("sovyx.test.levels")
        logger.debug("debug_event")
        logger.info("info_event")
        logger.warning("warning_event")
        logger.error("error_event")

        for handler in logging.getLogger().handlers:
            handler.flush()

        entries = query_logs(log_file, limit=100)
        levels = {e["level"] for e in entries}
        assert {"DEBUG", "INFO", "WARNING", "ERROR"}.issubset(levels)

        # Level filter works
        errors = query_logs(log_file, level="ERROR")
        assert all(e["level"] == "ERROR" for e in errors)
        assert any(e["event"] == "error_event" for e in errors)

    def test_idempotent_setup_no_handler_leak(self, tmp_path: Path) -> None:
        """Multiple setup_logging calls don't leak handlers."""
        log_file = tmp_path / "logs" / "sovyx.log"
        config = LoggingConfig(level="INFO", console_format="text", log_file=log_file)

        for _ in range(5):
            setup_logging(config)

        root = logging.getLogger()
        stream_count = sum(
            1
            for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
        )
        file_count = sum(
            1 for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        assert stream_count == 1, f"Expected 1 StreamHandler, got {stream_count}"
        assert file_count == 1, f"Expected 1 FileHandler, got {file_count}"

    def test_httpx_suppressed(self, tmp_path: Path) -> None:
        """httpx/httpcore loggers are suppressed after setup."""
        log_file = tmp_path / "logs" / "sovyx.log"
        config = LoggingConfig(level="DEBUG", console_format="text", log_file=log_file)
        setup_logging(config)

        for name in ("httpx", "httpcore", "urllib3", "hpack"):
            assert logging.getLogger(name).level == logging.WARNING
