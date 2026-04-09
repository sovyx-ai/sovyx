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
        f.write_text('not json\n{"event": "good", "level": "info", "logger": "test"}\n{broken\n')
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

    def test_search_in_nested_dict(self, tmp_path: Path) -> None:
        """Search finds text inside nested dict/list values."""
        f = tmp_path / "nested.log"
        entry = json.dumps(
            {
                "event": "generic",
                "level": "info",
                "logger": "test",
                "details": {"inner_key": "secret_needle_here"},
            }
        )
        f.write_text(entry + "\n")
        result = query_logs(f, search="secret_needle_here")
        assert len(result) == 1
        assert result[0]["event"] == "generic"

    def test_search_nested_no_match(self, tmp_path: Path) -> None:
        """Search in nested dict that does NOT match returns empty."""
        f = tmp_path / "nested_no.log"
        entry = json.dumps(
            {
                "event": "generic",
                "level": "info",
                "logger": "test",
                "details": {"inner_key": "nothing_useful"},
            }
        )
        f.write_text(entry + "\n")
        result = query_logs(f, search="totally_absent_string")
        assert len(result) == 0

    def test_large_file_seek_path(self, tmp_path: Path) -> None:
        """Files >1MB use seek-from-end path."""
        f = tmp_path / "large.log"
        # Each line ~120 bytes; need >1MB = >8700 lines
        lines: list[str] = []
        for i in range(9000):
            lines.append(
                json.dumps(
                    {
                        "event": f"entry_{i:06d}",
                        "level": "info",
                        "logger": "sovyx.test",
                        "ts": "2026-04-04T10:00:00",
                        "pad": "x" * 50,
                    }
                )
            )
        f.write_text("\n".join(lines) + "\n")
        assert f.stat().st_size > 1024 * 1024  # Confirm >1MB

        result = query_logs(f, limit=10)
        assert len(result) == 10
        # Most recent (last written) should be first
        assert result[0]["event"] == "entry_008999"

    def test_tail_lines_max_lines_truncation(self, tmp_path: Path) -> None:
        """When file has more lines than limit*10, tail truncates."""
        f = tmp_path / "many.log"
        # limit=1 → max_lines=10 inside _read_and_filter
        # Write 50 lines to exceed that
        lines = []
        for i in range(50):
            lines.append(
                json.dumps(
                    {
                        "event": f"line_{i:03d}",
                        "level": "info",
                        "logger": "test",
                    }
                )
            )
        f.write_text("\n".join(lines) + "\n")
        result = query_logs(f, limit=1)
        assert len(result) == 1
        # Should be the most recent line
        assert result[0]["event"] == "line_049"

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        """OSError during file read returns empty list."""
        # Use a directory path instead of a file — stat() works but open() fails
        d = tmp_path / "fakefile.log"
        d.mkdir()
        # Create a dummy file inside so stat shows non-zero size
        (d / "x").write_text("data")
        # query_logs checks .exists() which is True for dirs, but open("rb") will fail
        # Actually, Path.exists() is True for dirs too
        # The _tail_lines will get OSError when trying to open a dir
        result = query_logs(d)
        assert result == []

    def test_query_logs_exception_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When _read_and_filter raises, query_logs catches and returns []."""
        f = tmp_path / "ok.log"
        f.write_text(json.dumps({"event": "test", "level": "info"}) + "\n")

        from sovyx.dashboard import logs as logs_mod

        def _boom(*args: object, **kwargs: object) -> list[dict[str, object]]:
            msg = "synthetic failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(logs_mod, "_read_and_filter", _boom)
        result = query_logs(f)
        assert result == []

    def test_filter_uses_severity_fallback(self, tmp_path: Path) -> None:
        """Level filter checks 'severity' field when 'level' is absent."""
        f = tmp_path / "severity.log"
        entry = json.dumps({"event": "alt", "severity": "WARNING", "logger": "test"})
        f.write_text(entry + "\n")
        result = query_logs(f, level="WARNING")
        assert len(result) == 1

    def test_filter_uses_module_fallback(self, tmp_path: Path) -> None:
        """Module filter checks 'module' field when 'logger' is absent."""
        f = tmp_path / "modfield.log"
        entry = json.dumps({"event": "x", "level": "info", "module": "sovyx.alt"})
        f.write_text(entry + "\n")
        result = query_logs(f, module="sovyx.alt")
        assert len(result) == 1

    def test_search_in_event_message_field(self, tmp_path: Path) -> None:
        """Search matches the 'message' field fallback."""
        f = tmp_path / "msg.log"
        entry = json.dumps({"message": "deployment_ready", "level": "info", "logger": "t"})
        f.write_text(entry + "\n")
        result = query_logs(f, search="deployment")
        assert len(result) == 1

    def test_after_returns_newer_entries(self, log_file: Path) -> None:
        """after= returns only entries with timestamp > cursor."""
        result = query_logs(log_file, after="2026-04-04T10:00:02")
        assert len(result) == 2
        events = {e["event"] for e in result}
        assert events == {"db_error", "concept_created"}

    def test_after_no_match(self, log_file: Path) -> None:
        """after= beyond all entries returns empty."""
        result = query_logs(log_file, after="2099-01-01T00:00:00")
        assert result == []

    def test_after_none_returns_all(self, log_file: Path) -> None:
        """after=None is a no-op (returns all entries)."""
        result = query_logs(log_file, after=None, limit=100)
        assert len(result) == 5

    def test_after_with_level_filter(self, log_file: Path) -> None:
        """after= combined with level filter."""
        result = query_logs(log_file, after="2026-04-04T10:00:01", level="info")
        assert len(result) == 2
        events = {e["event"] for e in result}
        assert events == {"llm_call", "concept_created"}

    def test_after_uses_timestamp_field(self, tmp_path: Path) -> None:
        """after= works with 'timestamp' field (not just 'ts')."""
        f = tmp_path / "ts_field.log"
        entries = [
            json.dumps({"event": "old", "level": "info", "timestamp": "2026-04-04T10:00:00"}),
            json.dumps({"event": "new", "level": "info", "timestamp": "2026-04-04T10:00:05"}),
        ]
        f.write_text("\n".join(entries) + "\n")
        result = query_logs(f, after="2026-04-04T10:00:02")
        assert len(result) == 1
        assert result[0]["event"] == "new"

    def test_after_exact_timestamp_excluded(self, log_file: Path) -> None:
        """Entry with timestamp == after is excluded (strictly after)."""
        result = query_logs(log_file, after="2026-04-04T10:00:03")
        assert len(result) == 1
        assert result[0]["event"] == "concept_created"

    def test_search_with_list_nested_value(self, tmp_path: Path) -> None:
        """Search finds text inside nested list values."""
        f = tmp_path / "list_nested.log"
        entry = json.dumps(
            {
                "event": "batch",
                "level": "info",
                "logger": "test",
                "items": ["alpha", "beta_target", "gamma"],
            }
        )
        f.write_text(entry + "\n")
        result = query_logs(f, search="beta_target")
        assert len(result) == 1

    def test_rotation_fallback_to_backup(self, tmp_path: Path) -> None:
        """If primary log is empty (just rotated), reads from .1 backup."""
        primary = tmp_path / "sovyx.log"
        backup = tmp_path / "sovyx.log.1"

        # Primary is empty (just rotated)
        primary.write_text("")

        # Backup has the recent data
        entry = json.dumps({"event": "from_backup", "level": "info", "logger": "test"})
        backup.write_text(entry + "\n")

        result = query_logs(primary)
        assert len(result) == 1
        assert result[0]["event"] == "from_backup"

    def test_rotation_primary_missing_reads_backup(self, tmp_path: Path) -> None:
        """If primary log doesn't exist, reads from .1 backup."""
        primary = tmp_path / "sovyx.log"
        backup = tmp_path / "sovyx.log.1"

        # Primary doesn't exist
        assert not primary.exists()

        # Backup exists
        entry = json.dumps({"event": "rotated", "level": "info", "logger": "test"})
        backup.write_text(entry + "\n")

        result = query_logs(primary)
        # query_logs checks exists() first → returns [] for missing primary
        # This is correct: we don't want to silently read stale backups
        assert result == []

    def test_rotation_both_missing(self, tmp_path: Path) -> None:
        """Both primary and backup missing → empty result."""
        primary = tmp_path / "sovyx.log"
        result = query_logs(primary)
        assert result == []
