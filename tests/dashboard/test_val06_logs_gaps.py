"""VAL-06: Coverage gaps for dashboard/logs.py.

Covers:
- query_logs exception path (corrupt file mid-read)
- _tail_lines large file seek (>1MB)
- _tail_lines max_lines truncation
- _tail_lines OSError
- _parse_line blank lines
- search in nested dict/list values
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from sovyx.dashboard.logs import _tail_lines, query_logs

if TYPE_CHECKING:
    from pathlib import Path


class TestQueryLogsException:
    def test_read_error_returns_empty(self, tmp_path: Path) -> None:
        """When _read_and_filter raises, query_logs returns [] gracefully."""
        log_file = tmp_path / "sovyx.log"
        log_file.write_text('{"event": "test", "level": "INFO"}\n')

        with patch(
            "sovyx.dashboard.logs._read_and_filter",
            side_effect=RuntimeError("parse crash"),
        ):
            result = query_logs(log_file)
        assert result == []


class TestTailLinesLargeFile:
    def test_large_file_seeks_from_end(self, tmp_path: Path) -> None:
        """Files >1MB: seek to last 1MB and read from there."""
        log_file = tmp_path / "large.log"

        padding_line = json.dumps({"event": "pad", "level": "DEBUG"}) + "\n"
        padding_size = 1024 * 1024 + 100  # Just over 1MB

        with log_file.open("w") as f:
            written = 0
            while written < padding_size:
                f.write(padding_line)
                written += len(padding_line)
            for i in range(5):
                f.write(json.dumps({"event": f"recent-{i}", "level": "INFO"}) + "\n")

        lines = _tail_lines(log_file, max_lines=10)
        assert len(lines) > 0
        last_entries = [json.loads(ln) for ln in lines[-5:] if ln.strip()]
        events = [e["event"] for e in last_entries]
        assert "recent-4" in events

    def test_max_lines_truncation(self, tmp_path: Path) -> None:
        """When file has more lines than max_lines, only last N are returned."""
        log_file = tmp_path / "many.log"

        with log_file.open("w") as f:
            for i in range(50):
                f.write(json.dumps({"event": f"line-{i}", "level": "INFO"}) + "\n")

        lines = _tail_lines(log_file, max_lines=10)
        assert len(lines) == 10
        last_entry = json.loads(lines[-1])
        assert last_entry["event"] == "line-49"

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        """OSError during file open returns empty list."""
        dir_path = tmp_path / "fakelog"
        dir_path.mkdir()

        lines = _tail_lines(dir_path)
        assert lines == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        """Empty file (0 bytes) returns empty list."""
        log_file = tmp_path / "empty.log"
        log_file.write_text("")

        lines = _tail_lines(log_file)
        assert lines == []


class TestParseLineEdges:
    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        """Blank and whitespace-only lines are skipped."""
        log_file = tmp_path / "blanks.log"
        log_file.write_text(
            "\n"
            "   \n"
            '{"event": "real", "level": "INFO", "timestamp": "2026-01-01T00:00:00"}\n'
            "\t\n"
            '{"event": "also-real", "level": "DEBUG", "timestamp": "2026-01-01T00:00:01"}\n'
            "\n"
        )
        result = query_logs(log_file)
        assert len(result) == 2
        assert result[0]["event"] == "also-real"  # reversed order
        assert result[1]["event"] == "real"


class TestSearchNestedValues:
    def test_search_in_nested_dict(self, tmp_path: Path) -> None:
        """Search finds text inside nested dict values."""
        log_file = tmp_path / "nested.log"
        entry = {
            "event": "request",
            "level": "INFO",
            "data": {"url": "/api/secret-endpoint", "method": "POST"},
            "timestamp": "2026-01-01T00:00:00",
        }
        log_file.write_text(json.dumps(entry) + "\n")

        result = query_logs(log_file, search="secret-endpoint")
        assert len(result) == 1
        assert result[0]["event"] == "request"

    def test_search_in_nested_list(self, tmp_path: Path) -> None:
        """Search finds text inside nested list values."""
        log_file = tmp_path / "nested.log"
        entry = {
            "event": "batch",
            "level": "INFO",
            "items": ["alpha", "beta-target", "gamma"],
            "timestamp": "2026-01-01T00:00:00",
        }
        log_file.write_text(json.dumps(entry) + "\n")

        result = query_logs(log_file, search="beta-target")
        assert len(result) == 1

    def test_search_no_match_in_nested(self, tmp_path: Path) -> None:
        """Search returns empty when nested values don't match."""
        log_file = tmp_path / "nested.log"
        entry = {
            "event": "batch",
            "level": "INFO",
            "data": {"key": "value"},
            "timestamp": "2026-01-01T00:00:00",
        }
        log_file.write_text(json.dumps(entry) + "\n")

        result = query_logs(log_file, search="nonexistent-term")
        assert len(result) == 0
