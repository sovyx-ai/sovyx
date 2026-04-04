"""Tests for sovyx.dashboard.logs — log file query module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from sovyx.dashboard.logs import query_logs

if TYPE_CHECKING:
    from pathlib import Path


def _entry(event: str, level: str, logger: str, ts: str) -> dict[str, str]:
    return {"event": event, "level": level, "logger": logger, "ts": ts}


@pytest.fixture()
def log_file(tmp_path: Path) -> Path:
    """Create a temp JSON log file with sample entries."""
    entries = [
        _entry("engine_started", "info", "sovyx.engine", "2026-04-04T10:00:00"),
        _entry("message_received", "debug", "sovyx.bridge", "2026-04-04T10:00:01"),
        _entry("llm_call", "info", "sovyx.cognitive", "2026-04-04T10:00:02"),
        _entry("db_error", "error", "sovyx.persistence", "2026-04-04T10:00:03"),
        _entry("concept_created", "info", "sovyx.brain.service", "2026-04-04T10:00:04"),
    ]
    f = tmp_path / "sovyx.log"
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return f


class TestQueryLogs:
    def test_returns_all_entries(self, log_file: Path) -> None:
        result = query_logs(log_file, limit=100)
        assert len(result) == 5
        # Most recent first
        assert result[0]["event"] == "concept_created"
        assert result[-1]["event"] == "engine_started"

    def test_filter_by_level(self, log_file: Path) -> None:
        result = query_logs(log_file, level="error")
        assert len(result) == 1
        assert result[0]["event"] == "db_error"

    def test_filter_by_level_case_insensitive(self, log_file: Path) -> None:
        result = query_logs(log_file, level="INFO")
        assert len(result) == 3

    def test_filter_by_module(self, log_file: Path) -> None:
        result = query_logs(log_file, module="sovyx.brain")
        assert len(result) == 1
        assert result[0]["event"] == "concept_created"

    def test_filter_by_module_prefix(self, log_file: Path) -> None:
        result = query_logs(log_file, module="sovyx")
        assert len(result) == 5

    def test_filter_by_search(self, log_file: Path) -> None:
        result = query_logs(log_file, search="llm")
        assert len(result) == 1
        assert result[0]["event"] == "llm_call"

    def test_combined_filters(self, log_file: Path) -> None:
        result = query_logs(log_file, level="info", module="sovyx.brain")
        assert len(result) == 1

    def test_limit(self, log_file: Path) -> None:
        result = query_logs(log_file, limit=2)
        assert len(result) == 2
        assert result[0]["event"] == "concept_created"

    def test_none_log_file(self) -> None:
        result = query_logs(None)
        assert result == []

    def test_missing_file(self, tmp_path: Path) -> None:
        result = query_logs(tmp_path / "nonexistent.log")
        assert result == []

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.log"
        f.write_text("")
        result = query_logs(f)
        assert result == []

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.log"
        f.write_text(
            'not json\n'
            '{"event": "good", "level": "info", "logger": "test"}\n'
            '{broken\n'
        )
        result = query_logs(f)
        assert len(result) == 1
        assert result[0]["event"] == "good"

    def test_search_in_full_entry(self, tmp_path: Path) -> None:
        """Search matches keys/values beyond event field."""
        f = tmp_path / "meta.log"
        content = json.dumps({"event": "call", "level": "info", "model": "gpt-4o"})
        f.write_text(content + "\n")
        result = query_logs(f, search="gpt-4o")
        assert len(result) == 1
